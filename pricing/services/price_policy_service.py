import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import Q

from documents.services.document_numbering_service import (
    parse_document_date,
)
from pricing.models import PricePolicy


MONEY_QUANT = Decimal("0.01")

DEFAULT_EXPIRATION_THRESHOLD_DAYS = 365
DEFAULT_EXPIRATION_DISCOUNT_RATE = Decimal("0.30")


def try_parse_date(value: Any):
    if not value:
        return None

    try:
        return parse_document_date(value)
    except Exception:
        return None


def get_hospital_order_date(order):
    """
    价格阶段判断日期 = 医院订单 Date de commande。
    """
    data = (
        getattr(order, "extracted_order_data", None)
        or {}
    )

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    raw_date = (
        data.get("header", {}).get("order_date")
        or data.get("summary", {}).get("order_date")
    )

    parsed = try_parse_date(raw_date)

    if parsed:
        return parsed, "hospital_order_date"

    for attr in [
        "document_date",
        "manual_document_date",
        "order_date",
        "selected_date",
    ]:
        value = getattr(order, attr, None)
        parsed = try_parse_date(value)

        if parsed:
            return parsed, attr

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

    def priority(policy):
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

        start_date_priority = (
            policy.start_date
            or date.min
        )

        return (
            factory_specificity,
            category_specificity,
            start_date_priority,
            policy.id or 0,
        )

    candidates.sort(
        key=priority,
        reverse=True,
    )

    policy = candidates[0]
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

    正式 Factory PO 必须显式传入 PO document date。
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

        resolved = resolve_price_policy_for_product(
            product=product,
            target_date=policy_date,
            order_factory=order.factory,
        )

        result["warnings"].extend(
            resolved["warnings"]
        )

        policy: Optional[PricePolicy] = (
            resolved["policy"]
        )

        if not policy:
            result["warnings"].append(
                f"OrderItem {item.product_code}: "
                f"no price policy found for "
                f"date {policy_date}. "
                "Existing price snapshot was kept."
            )
            continue

        item.hospital_unit_price = (
            policy.hospital_unit_price
        )
        item.factory_unit_price = (
            policy.factory_unit_price
        )
        item.expiration_discount_rate = (
            policy.expiration_discount_rate
        )
        item.expiration_threshold_days = (
            policy.expiration_threshold_days
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
                    "factory_unit_price",
                    "expiration_discount_rate",
                    "expiration_threshold_days",
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
                "factory_unit_price": str(
                    policy.factory_unit_price
                ),
                "expiration_discount_rate": str(
                    policy.expiration_discount_rate
                ),
                "expiration_threshold_days": (
                    policy.expiration_threshold_days
                ),
            }
        )

        result["updated_count"] += 1

    return result
