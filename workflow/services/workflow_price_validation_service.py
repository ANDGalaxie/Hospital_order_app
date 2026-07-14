from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from shipments.models import ShipmentBatchItem


def to_decimal(value):
    """
    安全转换价格或折扣字段。
    """
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def validate_workflow_price_snapshots(
    workflow_item,
) -> Dict[str, Any]:
    """
    验证当前 WorkflowItem 对应批次的价格快照。

    正式文件价格必须来自 OrderItem 快照，
    不允许依赖 Product 当前默认价格或硬编码价格。
    """
    errors: List[str] = []
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []

    order = workflow_item.order
    batch = workflow_item.shipment_batch

    if not order:
        errors.append(
            "WorkflowItem 没有关联医院订单。"
        )

        return {
            "is_valid": False,
            "can_generate_documents": False,
            "checked_product_count": 0,
            "rows": rows,
            "errors": errors,
            "warnings": warnings,
        }

    if not batch:
        errors.append(
            "WorkflowItem 没有关联 ShipmentBatch。"
        )

        return {
            "is_valid": False,
            "can_generate_documents": False,
            "checked_product_count": 0,
            "rows": rows,
            "errors": errors,
            "warnings": warnings,
        }

    order_items_by_code = {
        str(item.product_code or "").strip(): item
        for item in (
            order.items
            .select_related(
                "product",
                "product__factory",
                "price_policy",
            )
            .order_by("id")
        )
        if str(item.product_code or "").strip()
    }

    batch_items = list(
        ShipmentBatchItem.objects.filter(
            batch=batch
        )
        .select_related("product")
        .order_by("product_code", "id")
    )

    if not batch_items:
        errors.append(
            "当前 ShipmentBatch 没有产品明细，"
            "无法验证价格。"
        )

    checked_codes = set()

    for batch_item in batch_items:
        product_code = str(
            batch_item.product_code or ""
        ).strip()

        quantity = int(
            batch_item.shipped_quantity or 0
        )

        if not product_code:
            errors.append(
                f"ShipmentBatchItem {batch_item.id} "
                "缺少产品编号。"
            )
            continue

        if quantity <= 0:
            # 数量问题由 ShipmentBatch 验证负责。
            continue

        if product_code in checked_codes:
            continue

        checked_codes.add(product_code)

        order_item = order_items_by_code.get(
            product_code
        )

        row_errors = []
        row_warnings = []

        if order_item is None:
            row_errors.append(
                "医院订单中不存在对应 OrderItem。"
            )

            errors.append(
                f"产品 {product_code}："
                "医院订单中不存在对应 OrderItem。"
            )

            rows.append(
                {
                    "product_code": product_code,
                    "quantity": quantity,
                    "hospital_unit_price": None,
                    "factory_unit_price": None,
                    "price_policy_id": None,
                    "price_policy_name": "",
                    "price_policy_date": None,
                    "expiration_discount_rate": None,
                    "expiration_threshold_days": None,
                    "errors": row_errors,
                    "warnings": row_warnings,
                }
            )

            continue

        if not order_item.product_id:
            message = "没有匹配 Product。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if not order_item.price_policy_id:
            message = "没有命中 PricePolicy。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        hospital_price = to_decimal(
            order_item.hospital_unit_price
        )

        factory_price = to_decimal(
            order_item.factory_unit_price
        )

        discount_rate = to_decimal(
            order_item.expiration_discount_rate
        )

        threshold_days = (
            order_item.expiration_threshold_days
        )

        if (
            hospital_price is None
            or hospital_price <= 0
        ):
            message = "缺少有效医院销售单价。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if (
            factory_price is None
            or factory_price <= 0
        ):
            message = "缺少有效工厂采购单价。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if not order_item.price_policy_date:
            message = "缺少价格规则日期快照。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if discount_rate is None:
            message = "缺少临期折扣率快照。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        elif (
            discount_rate < Decimal("0")
            or discount_rate > Decimal("1")
        ):
            message = (
                "临期折扣率必须在 0 到 1 之间。"
            )
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if (
            threshold_days is None
            or int(threshold_days) <= 0
        ):
            message = "缺少有效临期门槛天数。"
            row_errors.append(message)
            errors.append(
                f"产品 {product_code}：{message}"
            )

        if (
            order.factory_id
            and order_item.product_id
            and order_item.product.factory_id
            and (
                order.factory_id
                != order_item.product.factory_id
            )
        ):
            message = (
                "产品所属工厂与订单工厂不一致。"
            )
            row_warnings.append(message)
            warnings.append(
                f"产品 {product_code}：{message}"
            )

        rows.append(
            {
                "product_code": product_code,
                "quantity": quantity,
                "hospital_unit_price": (
                    str(hospital_price)
                    if hospital_price is not None
                    else None
                ),
                "factory_unit_price": (
                    str(factory_price)
                    if factory_price is not None
                    else None
                ),
                "price_policy_id": (
                    order_item.price_policy_id
                ),
                "price_policy_name": (
                    order_item.price_policy.name
                    if order_item.price_policy
                    else ""
                ),
                "price_policy_date": (
                    order_item.price_policy_date.isoformat()
                    if order_item.price_policy_date
                    else None
                ),
                "expiration_discount_rate": (
                    str(discount_rate)
                    if discount_rate is not None
                    else None
                ),
                "expiration_threshold_days": (
                    threshold_days
                ),
                "errors": row_errors,
                "warnings": row_warnings,
            }
        )

    return {
        "is_valid": len(errors) == 0,
        "can_generate_documents": (
            len(errors) == 0
            and len(warnings) == 0
        ),
        "checked_product_count": len(
            checked_codes
        ),
        "rows": rows,
        "errors": errors,
        "warnings": warnings,
    }
