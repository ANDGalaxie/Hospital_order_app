import json
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from documents.services.document_numbering_service import parse_document_date
from pricing.models import PricePolicy


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
    价格规则判断日期 = 医院订单 Date de commande。

    优先从 extracted_order_data 中读取。
    如果以后你的 Order 模型里有手动日期字段，也可以在这里接入。
    """
    data = getattr(order, "extracted_order_data", None) or {}

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

    # 兼容：如果你们上传页面有手动日期字段，未来可以在这里接入。
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
    返回当前类别以及所有父类别。
    用于支持规则写在父类上。
    """
    result = []
    current = category

    while current is not None:
        result.append(current)
        current = current.parent

    return result


def category_depth(category):
    depth = 0
    current = category

    while current is not None:
        depth += 1
        current = current.parent

    return depth


def policy_contains_date(policy, target_date):
    if policy.start_date and target_date < policy.start_date:
        return False

    if policy.end_date and target_date > policy.end_date:
        return False

    return True


def find_price_policy_for_product(product, target_date, order_factory=None):
    """
    根据 product + 日期 找最合适的价格规则。

    优先级：
      1. 工厂匹配更具体
      2. 产品类别更具体
      3. start_date 更晚
    """
    if not product or not target_date:
        return None

    factory = getattr(product, "factory", None) or order_factory
    categories = category_path(getattr(product, "category", None))

    qs = PricePolicy.objects.filter(is_active=True)

    if factory:
        qs = qs.filter(factory__in=[factory])
    else:
        qs = qs.filter(factory__isnull=True)

    if categories:
        qs = qs.filter(category__in=categories)
    else:
        qs = qs.filter(category__isnull=True)

    candidates = [
        policy for policy in qs
        if policy_contains_date(policy, target_date)
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda p: (
            category_depth(p.category) if p.category else 0,
            p.start_date or parse_document_date("1900-01-01"),
        ),
        reverse=True,
    )

    return candidates[0]


@transaction.atomic
def apply_price_policy_to_order(order, save=True):
    """
    根据医院订单日期，把价格规则应用到 OrderItem 价格快照。

    写入：
      - OrderItem.hospital_unit_price
      - OrderItem.factory_unit_price
      - OrderItem.expiration_discount_rate
      - OrderItem.price_policy
      - OrderItem.price_policy_date
      - OrderItem.price_policy_message
    """
    result = {
        "order_id": order.id,
        "bon_de_commande": order.bon_de_commande,
        "price_policy_date": None,
        "date_source": "",
        "updated_count": 0,
        "warnings": [],
        "errors": [],
    }

    policy_date, date_source = get_hospital_order_date(order)

    if not policy_date:
        result["errors"].append(
            f"Order {order.bon_de_commande}: cannot find hospital order date. "
            "Price policy was not applied."
        )
        return result

    result["price_policy_date"] = policy_date.isoformat()
    result["date_source"] = date_source

    for item in order.items.select_related("product", "product__factory", "product__category"):
        product = item.product

        if not product:
            result["warnings"].append(
                f"OrderItem {item.product_code}: no product linked. Price policy skipped."
            )
            continue

        policy = find_price_policy_for_product(
            product=product,
            target_date=policy_date,
            order_factory=order.factory,
        )

        if not policy:
            result["warnings"].append(
                f"OrderItem {item.product_code}: no price policy found for date {policy_date}. "
                "Existing price snapshot was kept."
            )
            continue

        item.hospital_unit_price = policy.hospital_unit_price
        item.factory_unit_price = policy.factory_unit_price
        item.expiration_discount_rate = policy.expiration_discount_rate
        item.price_policy = policy
        item.price_policy_date = policy_date
        item.price_policy_message = (
            f"Applied price policy {policy.id} on {policy_date} "
            f"from {date_source}."
        )

        if save:
            item.save(
                update_fields=[
                    "hospital_unit_price",
                    "factory_unit_price",
                    "expiration_discount_rate",
                    "price_policy",
                    "price_policy_date",
                    "price_policy_message",
                    "updated_at",
                ]
            )

        result["updated_count"] += 1

    return result
