from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import Q

from pricing.models import PricePolicy


MONEY_QUANT = Decimal("0.01")

def get_hospital_order_date(order):
    """
    医院价格基准日期只能来自正式 Order.order_date。
    """
    order_date = getattr(
        order,
        "order_date",
        None,
    )

    if order_date:
        return order_date, "order.order_date"

    return None, ""


def category_path(category):
    """
    返回当前分类以及所有父节点。
    """
    result = []
    current = category
    visited_ids = set()

    while current is not None:
        if current.id in visited_ids:
            break

        visited_ids.add(current.id)
        result.append(current)
        current = current.parent

    return result


def category_depth(category):
    depth = 0
    current = category
    visited_ids = set()

    while current is not None:
        if current.id in visited_ids:
            break

        visited_ids.add(current.id)
        depth += 1
        current = current.parent

    return depth


def policy_contains_date(policy, target_date):
    if (
        policy.start_date
        and target_date < policy.start_date
    ):
        return False

    if (
        policy.end_date
        and target_date > policy.end_date
    ):
        return False

    return True


def describe_policy_scope(policy):
    if policy.factory_id and policy.category_id:
        return "factory_category"

    if policy.factory_id:
        return "factory"

    return "global"


def resolve_price_policy_for_product(
    product,
    target_date,
    order_factory=None,
) -> Dict[str, Any]:
    """
    返回价格规则及完整匹配说明。

    优先级：
        1. 具体工厂优于全局工厂
        2. 更具体的分类优于上级或通用分类
        3. start_date 更晚者优先
        4. ID 更大者仅作为最终确定性排序
    """
    result = {
        "policy": None,
        "scope": "",
        "message": "",
        "warnings": [],
        "errors": [],
        "is_ambiguous": False,
        "target_date": (
            target_date.isoformat()
            if target_date
            else None
        ),
    }

    if not product:
        result["message"] = "No product was provided."
        return result

    if not target_date:
        result["message"] = (
            "No target date was provided."
        )
        return result

    product_factory = getattr(
        product,
        "factory",
        None,
    )

    effective_factory = (
        product_factory
        or order_factory
    )

    if (
        product_factory
        and order_factory
        and product_factory.id != order_factory.id
    ):
        result["warnings"].append(
            "Order factory and product factory do not match. "
            "The product factory was used for price matching."
        )

    categories = category_path(
        getattr(product, "category", None)
    )

    policies = (
        PricePolicy.objects.filter(
            is_active=True,
        )
        .select_related(
            "factory",
            "category",
            "category__parent",
        )
    )

    if effective_factory:
        policies = policies.filter(
            Q(factory=effective_factory)
            | Q(factory__isnull=True)
        )
    else:
        policies = policies.filter(
            factory__isnull=True
        )

    if categories:
        policies = policies.filter(
            Q(category__in=categories)
            | Q(category__isnull=True)
        )
    else:
        policies = policies.filter(
            category__isnull=True
        )

    candidates = [
        policy
        for policy in policies
        if policy_contains_date(
            policy,
            target_date,
        )
    ]

    if not candidates:
        result["message"] = (
            f"No active price policy matched "
            f"product={product} "
            f"date={target_date}."
        )
        return result

    def specificity(policy):
        factory_specificity = (
            1
            if (
                effective_factory
                and policy.factory_id
                == effective_factory.id
            )
            else 0
        )

        category_specificity = (
            category_depth(policy.category)
            if policy.category_id
            else 0
        )

        return (
            factory_specificity,
            category_specificity,
        )

    top_specificity = max(
        specificity(policy)
        for policy in candidates
    )
    top_candidates = [
        policy
        for policy in candidates
        if specificity(policy) == top_specificity
    ]

    if len(top_candidates) != 1:
        policy_ids = ", ".join(
            str(policy.id)
            for policy in top_candidates
        )
        result["is_ambiguous"] = True
        result["errors"].append(
            "同一有效价格作用域存在多条 PricePolicy："
            f"product={product}, date={target_date}, "
            f"policy_ids={policy_ids}。"
        )
        result["message"] = result["errors"][0]
        return result

    policy = top_candidates[0]
    scope = describe_policy_scope(policy)

    result["policy"] = policy
    result["scope"] = scope
    result["message"] = (
        f"Applied PricePolicy #{policy.id} "
        f"({policy.name or 'unnamed'}) "
        f"for date {target_date.isoformat()}. "
        f"Scope={scope}."
    )

    return result


def resolve_hospital_unit_price(
    product,
    hospital,
    reference_date,
    order_factory=None,
) -> Dict[str, Any]:
    """
    解析医院订单日期有效的销售价。

    当前 PricePolicy 没有 hospital scope；hospital 仅作为
    定价依据元数据保留，作用域仍由产品、工厂与分类决定。
    """
    result = resolve_price_policy_for_product(
        product=product,
        target_date=reference_date,
        order_factory=order_factory,
    )
    result = dict(result)
    result["price_basis_type"] = (
        "hospital_order_date"
    )
    result["reference_date"] = (
        reference_date.isoformat()
        if reference_date
        else None
    )
    result["hospital_id"] = (
        getattr(hospital, "id", None)
    )

    policy = result.get("policy")
    price = (
        policy.hospital_unit_price
        if policy
        else None
    )

    if policy and (
        price is None
        or Decimal(str(price)) <= 0
    ):
        result["errors"].append(
            f"PricePolicy #{policy.id} "
            "缺少有效医院销售单价。"
        )

    result["unit_price"] = price
    return result


def resolve_factory_unit_price(
    product,
    factory,
    reference_date,
) -> Dict[str, Any]:
    """
    解析本次工厂实际发货日期有效的采购价和折扣规则。
    """
    result = resolve_price_policy_for_product(
        product=product,
        target_date=reference_date,
        order_factory=factory,
    )
    result = dict(result)
    result["price_basis_type"] = (
        "factory_shipping_date"
    )
    result["reference_date"] = (
        reference_date.isoformat()
        if reference_date
        else None
    )
    result["factory_id"] = (
        getattr(factory, "id", None)
    )

    product_factory_id = getattr(
        product,
        "factory_id",
        None,
    )
    factory_id = getattr(
        factory,
        "id",
        None,
    )

    if (
        product_factory_id
        and factory_id
        and product_factory_id != factory_id
    ):
        result["errors"].append(
            "产品所属工厂与本批 "
            "FactoryConfirmation 工厂不一致。"
        )

    policy = result.get("policy")
    price = (
        policy.factory_unit_price
        if policy
        else None
    )

    if policy and (
        price is None
        or Decimal(str(price)) <= 0
    ):
        result["errors"].append(
            f"PricePolicy #{policy.id} "
            "缺少有效工厂采购单价。"
        )

    if policy and (
        policy.expiration_discount_rate is None
        or policy.expiration_threshold_days is None
        or policy.expiration_threshold_days <= 0
    ):
        result["errors"].append(
            f"PricePolicy #{policy.id} "
            "临期折扣参数不完整。"
        )

    result["unit_price"] = price
    result["expiration_discount_rate"] = (
        policy.expiration_discount_rate
        if policy
        else None
    )
    result["expiration_threshold_days"] = (
        policy.expiration_threshold_days
        if policy
        else None
    )
    return result


def find_price_policy_for_product(
    product,
    target_date,
    order_factory=None,
):
    """
    保留旧函数接口，避免其他代码立即失效。
    """
    result = resolve_price_policy_for_product(
        product=product,
        target_date=target_date,
        order_factory=order_factory,
    )

    return result["policy"]


def calculate_expiration_pricing(
    factory_unit_price,
    expiration_date,
    reference_date,
    expiration_threshold_days,
    expiration_discount_rate,
) -> Dict[str, Any]:
    """
    根据 serial expiration date 计算采购价。

    严格边界：
        expiration_date
        <
        reference_date + threshold_days

    正式 Factory PO 必须显式传入本批 FactoryConfirmation.shipping_date。
    """
    base_price = Decimal(
        str(factory_unit_price or 0)
    )

    discount_rate = Decimal(
        str(expiration_discount_rate or 0)
    )

    threshold_days = int(
        expiration_threshold_days or 0
    )

    result = {
        "base_price": base_price.quantize(
            MONEY_QUANT,
            rounding=ROUND_HALF_UP,
        ),
        "discount_rate": discount_rate,
        "threshold_days": threshold_days,
        "threshold_date": None,
        "discount_applied": False,
        "discount_amount": Decimal("0.00"),
        "final_price": base_price.quantize(
            MONEY_QUANT,
            rounding=ROUND_HALF_UP,
        ),
    }

    if (
        expiration_date is None
        or reference_date is None
        or threshold_days <= 0
        or discount_rate <= 0
    ):
        return result

    if discount_rate > Decimal("1.00"):
        raise ValueError(
            "expiration_discount_rate cannot exceed 1."
        )

    threshold_date = (
        reference_date
        + timedelta(days=threshold_days)
    )

    discount_applied = (
        expiration_date < threshold_date
    )

    if discount_applied:
        final_price = (
            base_price
            * (Decimal("1.00") - discount_rate)
        )
    else:
        final_price = base_price

    final_price = final_price.quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )

    discount_amount = (
        base_price - final_price
    ).quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )

    result.update(
        {
            "threshold_date": threshold_date,
            "discount_applied": discount_applied,
            "discount_amount": discount_amount,
            "final_price": final_price,
        }
    )

    return result


@transaction.atomic
def apply_price_policy_to_order(
    order,
    save=True,
):
    """
    根据医院订单日期，将价格规则写入 OrderItem 快照。
    """
    result = {
        "order_id": order.id,
        "bon_de_commande": (
            order.bon_de_commande
        ),
        "price_policy_date": None,
        "date_source": "",
        "updated_count": 0,
        "matched_items": [],
        "warnings": [],
        "errors": [],
    }

    policy_date, date_source = (
        get_hospital_order_date(order)
    )

    if not policy_date:
        result["errors"].append(
            f"Order {order.bon_de_commande}: "
            "cannot find hospital order date. "
            "Price policy was not applied."
        )
        return result

    result["price_policy_date"] = (
        policy_date.isoformat()
    )
    result["date_source"] = date_source

    items = order.items.select_related(
        "product",
        "product__factory",
        "product__category",
        "product__category__parent",
    )

    for item in items:
        product = item.product

        if not product:
            result["warnings"].append(
                f"OrderItem {item.product_code}: "
                "no product linked. "
                "Price policy skipped."
            )
            continue

        resolved = resolve_hospital_unit_price(
            product=product,
            hospital=order.hospital,
            reference_date=policy_date,
            order_factory=order.factory,
        )

        result["warnings"].extend(
            resolved["warnings"]
        )
        result["errors"].extend(
            resolved["errors"]
        )

        policy: Optional[PricePolicy] = (
            resolved["policy"]
        )

        if not policy or resolved["errors"]:
            if not resolved["errors"]:
                result["errors"].append(
                    f"OrderItem {item.product_code}: "
                    f"no unique hospital price found for "
                    f"date {policy_date}."
                )
            continue

        item.hospital_unit_price = (
            resolved["unit_price"]
        )
        item.price_policy = policy
        item.price_policy_date = policy_date
        item.price_policy_message = (
            resolved["message"]
        )

        if save:
            item.save(
                update_fields=[
                    "hospital_unit_price",
                    "price_policy",
                    "price_policy_date",
                    "price_policy_message",
                    "updated_at",
                ]
            )

        result["matched_items"].append(
            {
                "order_item_id": item.id,
                "product_code": item.product_code,
                "price_policy_id": policy.id,
                "scope": resolved["scope"],
                "hospital_unit_price": str(
                    policy.hospital_unit_price
                ),
            }
        )

        result["updated_count"] += 1

    return result
