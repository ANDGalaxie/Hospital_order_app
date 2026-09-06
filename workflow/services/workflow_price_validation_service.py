from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from pricing.services.price_policy_service import (
    resolve_factory_unit_price,
    resolve_hospital_unit_price,
)
from shipments.models import ShipmentBatchItem


def to_decimal(value):
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return None


def add_row_error(
    errors,
    row_errors,
    product_code,
    message,
):
    row_errors.append(message)
    errors.append(
        f"产品 {product_code}：{message}"
    )


def validate_workflow_price_snapshots(
    workflow_item,
) -> Dict[str, Any]:
    """
    验证 Invoice 医院价快照，以及 Factory PO 的批次价格日期。

    Invoice:
      Order.order_date -> OrderItem.hospital_unit_price

    Factory PO:
      FactoryConfirmation.shipping_date -> 当前批次的
      factory price / expiration discount policy
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

    order_date = order.order_date

    if not order_date:
        errors.append(
            "Order.order_date 为空，"
            "不能解析或验证医院销售价。"
        )

    confirmation = (
        batch.factory_confirmation
        if batch.factory_confirmation_id
        else None
    )
    factory_shipping_date = (
        confirmation.shipping_date
        if confirmation
        else None
    )

    if confirmation:
        if not factory_shipping_date:
            errors.append(
                "FactoryConfirmation.shipping_date "
                "为空，不能解析 Factory PO 价格。"
            )

        if not batch.batch_date:
            errors.append(
                "ShipmentBatch.batch_date 为空，"
                "不能确认 Factory PO 价格日期。"
            )

        elif (
            factory_shipping_date
            and batch.batch_date
            != factory_shipping_date
        ):
            errors.append(
                "ShipmentBatch.batch_date "
                f"({batch.batch_date}) 与 "
                "FactoryConfirmation.shipping_date "
                f"({factory_shipping_date}) 不一致。"
            )

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
            continue

        if product_code in checked_codes:
            continue

        checked_codes.add(product_code)
        order_item = order_items_by_code.get(
            product_code
        )
        row_errors = []
        row_warnings = []

        row = {
            "product_code": product_code,
            "quantity": quantity,
            "batch_quantity": quantity,
            "hospital_unit_price": None,
            "hospital_order_date": (
                order_date.isoformat()
                if order_date
                else None
            ),
            "hospital_price_basis_type": (
                "hospital_order_date"
            ),
            "hospital_price_policy_id": None,
            "factory_base_unit_price": None,
            "factory_shipping_date": (
                factory_shipping_date.isoformat()
                if factory_shipping_date
                else None
            ),
            "factory_price_basis_type": (
                "factory_shipping_date"
                if confirmation
                else None
            ),
            "factory_price_policy_id": None,
            "expiration_discount_rate": None,
            "expiration_threshold_days": None,
            "errors": row_errors,
            "warnings": row_warnings,
        }

        if order_item is None:
            add_row_error(
                errors,
                row_errors,
                product_code,
                "医院订单中不存在对应 OrderItem。",
            )
            rows.append(row)
            continue

        if not order_item.product_id:
            add_row_error(
                errors,
                row_errors,
                product_code,
                "没有匹配 Product。",
            )

        hospital_price = to_decimal(
            order_item.hospital_unit_price
        )
        row["hospital_unit_price"] = (
            str(hospital_price)
            if hospital_price is not None
            else None
        )
        row["hospital_price_policy_id"] = (
            order_item.price_policy_id
        )

        if (
            hospital_price is None
            or hospital_price <= 0
        ):
            add_row_error(
                errors,
                row_errors,
                product_code,
                "缺少有效医院销售单价快照。",
            )

        if not order_item.price_policy_id:
            add_row_error(
                errors,
                row_errors,
                product_code,
                "没有命中医院 PricePolicy。",
            )

        if not order_item.price_policy_date:
            add_row_error(
                errors,
                row_errors,
                product_code,
                "缺少医院价格规则日期快照。",
            )

        elif (
            order_date
            and order_item.price_policy_date
            != order_date
        ):
            add_row_error(
                errors,
                row_errors,
                product_code,
                "医院价格快照日期与 "
                "Order.order_date 不一致。",
            )

        if order_date and order_item.product_id:
            hospital_resolution = (
                resolve_hospital_unit_price(
                    product=order_item.product,
                    hospital=order.hospital,
                    reference_date=order_date,
                    order_factory=order.factory,
                )
            )

            for resolution_error in (
                hospital_resolution["errors"]
            ):
                add_row_error(
                    errors,
                    row_errors,
                    product_code,
                    resolution_error,
                )

            hospital_policy = (
                hospital_resolution["policy"]
            )

            if hospital_policy is None:
                add_row_error(
                    errors,
                    row_errors,
                    product_code,
                    "医院订单日期没有唯一有效价格。",
                )

            elif not hospital_resolution["errors"]:
                resolved_price = to_decimal(
                    hospital_resolution[
                        "unit_price"
                    ]
                )

                if (
                    hospital_price is not None
                    and resolved_price
                    != hospital_price
                ):
                    add_row_error(
                        errors,
                        row_errors,
                        product_code,
                        "医院售价快照与订单日期"
                        "有效价格不一致，需要人工复核或"
                        "明确重新计价。",
                    )

        if (
            confirmation
            and factory_shipping_date
            and order_item.product_id
        ):
            factory_resolution = (
                resolve_factory_unit_price(
                    product=order_item.product,
                    factory=(
                        confirmation.factory
                        or order.factory
                    ),
                    reference_date=(
                        factory_shipping_date
                    ),
                )
            )

            for resolution_error in (
                factory_resolution["errors"]
            ):
                add_row_error(
                    errors,
                    row_errors,
                    product_code,
                    resolution_error,
                )

            factory_policy = (
                factory_resolution["policy"]
            )

            if factory_policy is None:
                add_row_error(
                    errors,
                    row_errors,
                    product_code,
                    "本批发货日期没有唯一有效"
                    "工厂价格。",
                )
            elif not factory_resolution["errors"]:
                factory_price = to_decimal(
                    factory_resolution[
                        "unit_price"
                    ]
                )
                discount_rate = to_decimal(
                    factory_resolution[
                        "expiration_discount_rate"
                    ]
                )
                threshold_days = (
                    factory_resolution[
                        "expiration_threshold_days"
                    ]
                )

                row[
                    "factory_base_unit_price"
                ] = str(factory_price)
                row[
                    "factory_price_policy_id"
                ] = factory_policy.id
                row[
                    "expiration_discount_rate"
                ] = str(discount_rate)
                row[
                    "expiration_threshold_days"
                ] = threshold_days

                if (
                    factory_price is None
                    or factory_price <= 0
                ):
                    add_row_error(
                        errors,
                        row_errors,
                        product_code,
                        "缺少有效工厂采购单价。",
                    )

                if (
                    discount_rate is None
                    or discount_rate < Decimal("0")
                    or discount_rate > Decimal("1")
                ):
                    add_row_error(
                        errors,
                        row_errors,
                        product_code,
                        "临期折扣率无法计算。",
                    )

                if (
                    threshold_days is None
                    or int(threshold_days) <= 0
                ):
                    add_row_error(
                        errors,
                        row_errors,
                        product_code,
                        "临期折扣门槛无法计算。",
                    )

        rows.append(row)

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
