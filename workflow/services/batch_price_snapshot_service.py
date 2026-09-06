from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from factory_confirmations.models import SerialItem
from pricing.services.price_policy_service import (
    calculate_expiration_pricing,
    resolve_factory_unit_price,
)
from shipments.models import ShipmentBatchItem


try:
    from backorders.models import InventoryItem
except Exception:
    InventoryItem = None


MONEY_QUANT = Decimal("0.01")
QUANTITY_QUANT = Decimal("0.01")


def to_decimal(
    value,
    default=None,
):
    if value is None or value == "":
        return default

    try:
        return Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return default


def get_serial_delivered_quantity(serial):
    """
    与旧文档生成逻辑保持一致。

    默认一个 SerialItem 代表 1 件。
    extraction raw_data 中存在 delivered_quantity 时，
    使用该数量。
    """
    raw_data = (
        getattr(serial, "raw_data", None)
        or {}
    )

    raw_quantity = (
        raw_data.get("delivered_quantity")
        if isinstance(raw_data, dict)
        else None
    )

    quantity = to_decimal(
        raw_quantity,
        Decimal("1.00"),
    )

    if quantity is None or quantity <= 0:
        return Decimal("1.00")

    return quantity


def get_batch_pricing_serial_rows(
    batch,
) -> List[Dict[str, Any]]:
    """
    读取当前 ShipmentBatch 的 Serial 级价格来源。

    支持：
    - FactoryConfirmation
    - InventoryAllocation
    """
    rows: List[Dict[str, Any]] = []

    if batch.factory_confirmation_id:
        serial_items = (
            SerialItem.objects.filter(
                order=batch.order,
                factory_confirmation=(
                    batch.factory_confirmation
                ),
            )
            .order_by(
                "product_code",
                "serial_number",
                "id",
            )
        )

        for serial in serial_items:
            rows.append(
                {
                    "source": (
                        "factory_confirmation"
                    ),
                    "source_id": serial.id,
                    "product_code": str(
                        serial.product_code or ""
                    ).strip(),
                    "serial_number": str(
                        serial.serial_number or ""
                    ).strip(),
                    "expiration_date": (
                        serial.expiration_date
                    ),
                    "quantity": (
                        get_serial_delivered_quantity(
                            serial
                        )
                    ),
                }
            )

        return rows

    if (
        batch.inventory_allocation_id
        and InventoryItem is not None
    ):
        inventory_items = (
            InventoryItem.objects.filter(
                allocation=(
                    batch.inventory_allocation
                )
            )
            .order_by(
                "product_code",
                "serial_number",
                "id",
            )
        )

        for inventory_item in inventory_items:
            rows.append(
                {
                    "source": (
                        "inventory_allocation"
                    ),
                    "source_id": (
                        inventory_item.id
                    ),
                    "product_code": str(
                        inventory_item.product_code
                        or ""
                    ).strip(),
                    "serial_number": str(
                        inventory_item.serial_number
                        or ""
                    ).strip(),
                    "expiration_date": (
                        inventory_item
                        .expiration_date
                    ),
                    "quantity": Decimal("1.00"),
                }
            )

        return rows

    return rows


def get_expected_batch_quantities(batch):
    """
    ShipmentBatchItem 中记录的本批应发数量。
    """
    quantities = defaultdict(
        lambda: Decimal("0.00")
    )

    batch_items = (
        ShipmentBatchItem.objects.filter(
            batch=batch
        )
        .order_by(
            "product_code",
            "id",
        )
    )

    for batch_item in batch_items:
        product_code = str(
            batch_item.product_code or ""
        ).strip()

        quantity = to_decimal(
            batch_item.shipped_quantity,
            Decimal("0.00"),
        )

        if product_code:
            quantities[product_code] += (
                quantity
            )

    return dict(quantities)


def build_batch_price_snapshot(
    batch,
    factory_shipping_date=None,
    factory=None,
    factory_shipping_date_source="",
) -> Dict[str, Any]:
    """
    对当前 ShipmentBatch 进行 Serial 级价格计算。

    临期判断：

        expiration_date
        <
        factory_shipping_date
        + PricePolicy.expiration_threshold_days

    工厂价格和折扣规则统一来自该日期有效的 PricePolicy。

    不允许回退：
        Product 当前价格
        默认工厂价
        固定 365 天门槛
    """
    errors: List[str] = []
    warnings: List[str] = []

    result = {
        "batch_id": batch.id,
        "order_id": batch.order_id,
        "reference_date": None,
        "price_basis_type": (
            "factory_shipping_date"
        ),
        "product_rows": [],
        "po_groups": [],
        "errors": errors,
        "warnings": warnings,
        "base_hospital_total": (
            Decimal("0.00")
        ),
        "base_factory_total": (
            Decimal("0.00")
        ),
        "actual_factory_total": (
            Decimal("0.00")
        ),
        "factory_discount_savings": (
            Decimal("0.00")
        ),
        "priced_serial_quantity": (
            Decimal("0.00")
        ),
        "is_valid": False,
    }

    if not batch.order_id:
        errors.append(
            "ShipmentBatch 没有关联医院订单。"
        )
        return result

    if batch.factory_confirmation_id:
        confirmation = (
            batch.factory_confirmation
        )
        shipping_date = (
            confirmation.shipping_date
        )

        if not shipping_date:
            errors.append(
                "FactoryConfirmation 缺少 shipping_date，"
                "无法解析本批工厂价格或折扣。"
            )
            return result

        if not batch.batch_date:
            errors.append(
                "ShipmentBatch 缺少 batch_date，"
                "无法确认工厂价格基准日期。"
            )
            return result

        if batch.batch_date != shipping_date:
            errors.append(
                "ShipmentBatch.batch_date "
                f"({batch.batch_date}) 与 "
                "FactoryConfirmation.shipping_date "
                f"({shipping_date}) 不一致。"
            )
            return result

        effective_factory = (
            factory
            or confirmation.factory
            or batch.order.factory
        )
        date_source = (
            "factory_confirmation.shipping_date"
        )

    elif batch.inventory_allocation_id:
        shipping_date = factory_shipping_date

        if not shipping_date:
            errors.append(
                "当前库存补发批次没有可用的工厂实际"
                "发货日期。请使用 "
                "--factory-shipping-date YYYY-MM-DD 指定。"
            )
            return result

        effective_factory = (
            factory
            or batch.order.factory
        )
        date_source = (
            factory_shipping_date_source
            or "explicit_factory_shipping_date"
        )

    else:
        errors.append(
            "Factory PO 定价要求 ShipmentBatch 关联 "
            "FactoryConfirmation 或 InventoryAllocation。"
        )
        return result

    if not effective_factory:
        errors.append(
            "无法确定本批 Factory PO 的工厂。"
        )
        return result

    result["reference_date"] = shipping_date
    result["factory_shipping_date_source"] = (
        date_source
    )
    result["factory_id"] = effective_factory.id

    expected_quantities = (
        get_expected_batch_quantities(batch)
    )

    serial_rows = (
        get_batch_pricing_serial_rows(batch)
    )

    if not serial_rows:
        errors.append(
            "当前 ShipmentBatch 没有可用于"
            "价格计算的 Serial 明细。"
        )
        return result

    order_items = list(
        batch.order.items
        .select_related(
            "product",
            "price_policy",
        )
        .order_by("id")
    )

    order_items_by_code = {
        str(
            order_item.product_code or ""
        ).strip(): order_item
        for order_item in order_items
        if str(
            order_item.product_code or ""
        ).strip()
    }

    order_index = {
        str(
            order_item.product_code or ""
        ).strip(): index
        for index, order_item
        in enumerate(order_items)
    }

    product_accumulator = {}
    po_group_accumulator = {}

    for serial_row in serial_rows:
        product_code = (
            serial_row["product_code"]
        )

        serial_number = (
            serial_row["serial_number"]
        )

        expiration_date = (
            serial_row["expiration_date"]
        )

        quantity = to_decimal(
            serial_row["quantity"],
            Decimal("0.00"),
        )

        serial_label = (
            f"{product_code or 'NO_PRODUCT'}"
            f" / "
            f"{serial_number or 'NO_SERIAL'}"
        )

        if not product_code:
            errors.append(
                f"Serial {serial_label} "
                "缺少产品编号。"
            )
            continue

        if quantity is None or quantity <= 0:
            errors.append(
                f"Serial {serial_label} "
                "数量无效。"
            )
            continue

        if not expiration_date:
            errors.append(
                f"Serial {serial_label} "
                "缺少 expiration_date。"
            )
            continue

        order_item = (
            order_items_by_code.get(
                product_code
            )
        )

        if order_item is None:
            errors.append(
                f"产品 {product_code} "
                "在当前医院订单中不存在。"
            )
            continue

        hospital_price = to_decimal(
            order_item.hospital_unit_price
        )

        factory_resolution = (
            resolve_factory_unit_price(
                product=order_item.product,
                factory=effective_factory,
                reference_date=shipping_date,
            )
        )

        if factory_resolution["errors"]:
            for resolution_error in (
                factory_resolution["errors"]
            ):
                errors.append(
                    f"产品 {product_code}："
                    f"{resolution_error}"
                )
            continue

        factory_policy = (
            factory_resolution["policy"]
        )
        base_factory_price = to_decimal(
            factory_resolution["unit_price"]
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

        row_has_error = False

        if factory_policy is None:
            errors.append(
                f"产品 {product_code} 在发货日期 "
                f"{shipping_date} 找不到唯一有效工厂价格。"
            )
            row_has_error = True

        if (
            base_factory_price is None
            or base_factory_price <= 0
        ):
            errors.append(
                f"产品 {product_code} "
                "缺少有效工厂采购价。"
            )
            row_has_error = True

        if (
            hospital_price is None
            or hospital_price <= 0
        ):
            errors.append(
                f"产品 {product_code} "
                "缺少有效医院销售价快照。"
            )
            row_has_error = True

        if discount_rate is None:
            errors.append(
                f"产品 {product_code} "
                "缺少临期折扣率。"
            )
            row_has_error = True

        elif (
            discount_rate < Decimal("0")
            or discount_rate > Decimal("1")
        ):
            errors.append(
                f"产品 {product_code} "
                "临期折扣率不在 0 到 1 之间。"
            )
            row_has_error = True

        if (
            threshold_days is None
            or int(threshold_days) <= 0
        ):
            errors.append(
                f"产品 {product_code} "
                "缺少有效临期门槛。"
            )
            row_has_error = True

        if row_has_error:
            continue

        pricing = calculate_expiration_pricing(
            factory_unit_price=(
                base_factory_price
            ),
            expiration_date=expiration_date,
            reference_date=shipping_date,
            expiration_threshold_days=(
                threshold_days
            ),
            expiration_discount_rate=(
                discount_rate
            ),
        )

        base_unit_price = (
            pricing["base_price"]
        )

        final_unit_price = (
            pricing["final_price"]
        )

        discount_applied = (
            pricing["discount_applied"]
        )

        applied_discount_rate = (
            discount_rate
            if discount_applied
            else Decimal("0.00")
        )

        base_factory_amount = (
            base_unit_price * quantity
        ).quantize(MONEY_QUANT)

        actual_factory_amount = (
            final_unit_price * quantity
        ).quantize(MONEY_QUANT)

        hospital_amount = (
            hospital_price * quantity
        ).quantize(MONEY_QUANT)

        discount_savings = (
            base_factory_amount
            - actual_factory_amount
        ).quantize(MONEY_QUANT)

        if product_code not in (
            product_accumulator
        ):
            discounted_unit_price = (
                base_factory_price
                * (
                    Decimal("1.00")
                    - discount_rate
                )
            ).quantize(MONEY_QUANT)

            product_accumulator[
                product_code
            ] = {
                "product_code": (
                    product_code
                ),
                "description": (
                    order_item.product.description
                    if (
                        order_item.product
                        and order_item
                        .product
                        .description
                    )
                    else (
                        order_item.description
                        or ""
                    )
                ),
                "quantity": (
                    Decimal("0.00")
                ),
                "normal_quantity": (
                    Decimal("0.00")
                ),
                "discounted_quantity": (
                    Decimal("0.00")
                ),
                "hospital_unit_price": (
                    hospital_price
                ),
                "base_factory_unit_price": (
                    base_factory_price
                ),
                "discounted_factory_unit_price": (
                    discounted_unit_price
                ),
                "expiration_discount_rate": (
                    discount_rate
                ),
                "expiration_threshold_days": (
                    int(threshold_days)
                ),
                "price_policy_id": (
                    factory_policy.id
                ),
                "price_policy_name": (
                    factory_policy.name
                    or ""
                ),
                "price_policy_date": (
                    shipping_date
                ),
                "factory_shipping_date": (
                    shipping_date
                ),
                "price_basis_type": (
                    "factory_shipping_date"
                ),
                "hospital_amount": (
                    Decimal("0.00")
                ),
                "base_factory_amount": (
                    Decimal("0.00")
                ),
                "actual_factory_amount": (
                    Decimal("0.00")
                ),
                "discount_savings": (
                    Decimal("0.00")
                ),
                "serial_count": 0,
            }

        product_row = (
            product_accumulator[
                product_code
            ]
        )

        product_row["quantity"] += quantity
        product_row["serial_count"] += 1

        if discount_applied:
            product_row[
                "discounted_quantity"
            ] += quantity
        else:
            product_row[
                "normal_quantity"
            ] += quantity

        product_row[
            "hospital_amount"
        ] += hospital_amount

        product_row[
            "base_factory_amount"
        ] += base_factory_amount

        product_row[
            "actual_factory_amount"
        ] += actual_factory_amount

        product_row[
            "discount_savings"
        ] += discount_savings

        po_group_key = (
            product_code,
            str(base_unit_price),
            str(applied_discount_rate),
        )

        if po_group_key not in (
            po_group_accumulator
        ):
            po_group_accumulator[
                po_group_key
            ] = {
                "product_code": (
                    product_code
                ),
                "description": (
                    product_row[
                        "description"
                    ]
                ),
                "quantity": (
                    Decimal("0.00")
                ),
                "unit_price": (
                    base_unit_price
                ),
                "discount_rate": (
                    applied_discount_rate
                ),
                "final_unit_price": (
                    final_unit_price
                ),
                "amount": (
                    Decimal("0.00")
                ),
                "discount_applied": (
                    discount_applied
                ),
                "serial_numbers": [],
                "expiration_dates": [],
                "min_expiration_date": (
                    expiration_date
                ),
                "expiration_threshold_days": (
                    int(threshold_days)
                ),
                "factory_shipping_date": (
                    shipping_date
                ),
                "price_basis_type": (
                    "factory_shipping_date"
                ),
                "price_policy_id": (
                    factory_policy.id
                ),
                "price_policy_name": (
                    factory_policy.name
                    or ""
                ),
            }

        po_group = (
            po_group_accumulator[
                po_group_key
            ]
        )

        po_group["quantity"] += quantity

        po_group["amount"] += (
            actual_factory_amount
        )

        if serial_number:
            po_group[
                "serial_numbers"
            ].append(serial_number)

        if expiration_date:
            po_group[
                "expiration_dates"
            ].append(
                expiration_date.isoformat()
            )

            current_min = po_group[
                "min_expiration_date"
            ]

            if (
                current_min is None
                or expiration_date
                < current_min
            ):
                po_group[
                    "min_expiration_date"
                ] = expiration_date

        result[
            "base_hospital_total"
        ] += hospital_amount

        result[
            "base_factory_total"
        ] += base_factory_amount

        result[
            "actual_factory_total"
        ] += actual_factory_amount

        result[
            "factory_discount_savings"
        ] += discount_savings

        result[
            "priced_serial_quantity"
        ] += quantity

    for (
        product_code,
        expected_quantity,
    ) in expected_quantities.items():
        product_row = (
            product_accumulator.get(
                product_code
            )
        )

        priced_quantity = (
            product_row["quantity"]
            if product_row
            else Decimal("0.00")
        )

        if priced_quantity != expected_quantity:
            errors.append(
                f"产品 {product_code}："
                f"ShipmentBatch 数量="
                f"{expected_quantity}，"
                f"但完成价格计算的 Serial 数量="
                f"{priced_quantity}。"
            )

    for product_code in (
        product_accumulator.keys()
    ):
        if product_code not in (
            expected_quantities
        ):
            errors.append(
                f"产品 {product_code} "
                "存在 Serial 价格记录，"
                "但不在 ShipmentBatchItem 中。"
            )

    result["product_rows"] = sorted(
        product_accumulator.values(),
        key=lambda row: (
            order_index.get(
                row["product_code"],
                999999,
            ),
            row["product_code"],
        ),
    )

    result["po_groups"] = sorted(
        po_group_accumulator.values(),
        key=lambda row: (
            order_index.get(
                row["product_code"],
                999999,
            ),
            row["product_code"],
            row["discount_rate"],
        ),
    )

    for key in [
        "base_hospital_total",
        "base_factory_total",
        "actual_factory_total",
        "factory_discount_savings",
    ]:
        result[key] = result[key].quantize(
            MONEY_QUANT
        )

    result["is_valid"] = (
        len(errors) == 0
    )

    return result
