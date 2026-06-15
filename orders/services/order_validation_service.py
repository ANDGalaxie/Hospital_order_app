from collections import Counter
from typing import Any, Dict, List

from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem


def normalize_bon_de_commande(value: Any) -> str:
    """
    标准化 bon_de_commande。

    例如：
        "BON DE COMMANDE N° 150222" -> "150222"
        150222 -> "150222"
    """
    if value is None:
        return ""

    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())

    return digits or text


def get_factory_bon_de_commande(factory_data: Dict[str, Any]) -> str:
    """
    从工厂确认提取结果中读取 bon_de_commande。
    """
    bon = (
        factory_data.get("factory_document", {}).get("bon_de_commande")
        or factory_data.get("summary", {}).get("bon_de_commande")
        or factory_data.get("header", {}).get("bon_de_commande")
    )

    return normalize_bon_de_commande(bon)


def get_successful_factory_confirmations(order: Order):
    """
    只读取当前 Order 绑定的、提取成功的 FactoryConfirmation。

    不能读取别的订单，也不能读取全局最新文件。
    """
    return FactoryConfirmation.objects.filter(
        order=order,
        extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
    ).order_by("-created_at")


def validate_order_for_document_generation(
    order: Order,
    save: bool = True,
) -> Dict[str, Any]:
    """
    检查一个 Order 是否可以生成正式 Invoice / Factory PO。

    检查内容：
        1. 医院订单是否已经提取成功
        2. 医院是否已经匹配或人工确认
        3. 产品行是否都匹配 Product 或人工确认
        4. 是否存在当前 order 的工厂确认文件
        5. 工厂确认文件里的 bon_de_commande 是否和 order 一致
        6. serial number / expiration date 是否完整
        7. 工厂确认产品是否属于医院订单
        8. 是否至少有一个 confirmed_quantity > 0
    """
    errors: List[str] = []
    warnings: List[str] = []

    order_bon = normalize_bon_de_commande(order.bon_de_commande)

    # ------------------------------------------------------------
    # 1. 医院订单提取结果检查
    # ------------------------------------------------------------
    if order.extraction_status != Order.ExtractionStatus.SUCCESS:
        errors.append(
            f"Hospital order extraction is not successful. "
            f"Current extraction_status={order.extraction_status}."
        )

    if not order.extracted_order_data:
        errors.append("Missing Order.extracted_order_data.")

    # ------------------------------------------------------------
    # 2. 医院匹配检查
    # ------------------------------------------------------------
    if order.hospital_match_status == Order.HospitalMatchStatus.OK:
        if not order.hospital:
            errors.append(
                "Hospital match status is OK, but order.hospital is empty."
            )

    elif order.hospital_match_status == Order.HospitalMatchStatus.MANUALLY_CONFIRMED:
        if not order.hospital and not order.billing_address_data:
            errors.append(
                "Hospital was manually confirmed, but there is no hospital "
                "record or billing_address_data."
            )

    else:
        errors.append(
            "Hospital needs review before document generation. "
            f"Current hospital_match_status={order.hospital_match_status}."
        )

    if not order.shipping_address_data:
        errors.append("Missing shipping_address_data.")

    if not order.billing_address_data and not order.hospital:
        errors.append("Missing billing address information.")

    # ------------------------------------------------------------
    # 工厂匹配检查：工厂应来自医院订单 supplier / fournisseur 区域
    # ------------------------------------------------------------
    if order.factory_match_status == Order.FactoryMatchStatus.OK:
        if not order.factory:
            errors.append(
                "Factory match status is OK, but order.factory is empty."
            )

    elif order.factory_match_status == Order.FactoryMatchStatus.MANUALLY_CONFIRMED:
        if not order.factory:
            errors.append(
                "Factory was manually confirmed, but order.factory is empty."
            )

    else:
        errors.append(
            "Factory needs review before document generation. "
            f"Current factory_match_status={order.factory_match_status}."
        )
        
    # ------------------------------------------------------------
    # 3. OrderItem 产品匹配检查
    # ------------------------------------------------------------
    order_items = list(order.items.all())

    if not order_items:
        errors.append("No OrderItem found for this order.")

    for item in order_items:
        label = f"{item.product_code}"

        if item.requested_quantity <= 0:
            errors.append(
                f"OrderItem {label}: requested_quantity is zero or invalid."
            )

        if item.product_match_status == OrderItem.ProductMatchStatus.NEEDS_REVIEW:
            errors.append(
                f"OrderItem {label}: product needs review."
            )

        if item.product_match_status == OrderItem.ProductMatchStatus.OK:
            if not item.product:
                errors.append(
                    f"OrderItem {label}: product_match_status is OK, "
                    f"but product is empty."
                )

        if item.product_match_status == OrderItem.ProductMatchStatus.MANUALLY_CONFIRMED:
            if not item.is_manually_confirmed:
                warnings.append(
                    f"OrderItem {label}: status is manually confirmed, "
                    f"but is_manually_confirmed is not checked."
                )

        if not item.product and not item.is_manually_confirmed:
            errors.append(
                f"OrderItem {label}: no Product linked and not manually confirmed."
            )

    order_product_codes = {
        item.product_code
        for item in order_items
        if item.product_code
    }

    # ------------------------------------------------------------
    # 4. FactoryConfirmation 检查
    # ------------------------------------------------------------
    confirmations = list(get_successful_factory_confirmations(order))

    if not confirmations:
        errors.append(
            "No successful FactoryConfirmation found for this order."
        )
        selected_confirmation = None

    else:
        selected_confirmation = confirmations[0]

        if len(confirmations) > 1:
            warnings.append(
                f"This order has {len(confirmations)} successful factory confirmations. "
                f"Current system will use the latest one unless later we add manual selection."
            )


    # ------------------------------------------------------------
    # 5. 检查工厂确认文件的订单号
    # ------------------------------------------------------------
    if selected_confirmation:
        factory_data = selected_confirmation.extracted_confirmation_data or {}

        if not factory_data:
            errors.append(
                f"FactoryConfirmation {selected_confirmation.id} has no "
                f"extracted_confirmation_data."
            )
        else:
            factory_bon = get_factory_bon_de_commande(factory_data)

            manual_confirmed = getattr(
                selected_confirmation,
                "bon_de_commande_manual_confirmed",
                False,
            )

            if not factory_bon:
                if manual_confirmed:
                    warnings.append(
                        f"FactoryConfirmation {selected_confirmation.id}: "
                        f"bon_de_commande is missing, but it was manually confirmed "
                        f"for Order {order_bon}."
                    )
                else:
                    errors.append(
                        f"FactoryConfirmation {selected_confirmation.id}: "
                        f"cannot find bon_de_commande in extracted data. "
                        f"Manual confirmation is required."
                    )

            elif factory_bon != order_bon:
                if manual_confirmed:
                    warnings.append(
                        f"FactoryConfirmation {selected_confirmation.id}: "
                        f"bon_de_commande mismatch "
                        f"(Order={order_bon}, confirmation={factory_bon}), "
                        f"but it was manually confirmed."
                    )
                else:
                    errors.append(
                        f"FactoryConfirmation {selected_confirmation.id}: "
                        f"bon_de_commande mismatch. "
                        f"Order={order_bon}, confirmation={factory_bon}. "
                        f"Manual confirmation is required."
                    )

    # ------------------------------------------------------------
    # 6. SerialItem 检查
    # ------------------------------------------------------------
    if selected_confirmation:
        serial_items = list(
            SerialItem.objects.filter(
                order=order,
                factory_confirmation=selected_confirmation,
            )
        )
    else:
        serial_items = []

    if selected_confirmation and not serial_items:
        errors.append(
            f"No SerialItem found for FactoryConfirmation {selected_confirmation.id}."
        )

    serial_product_codes = set()

    for serial in serial_items:
        label = (
            f"{serial.product_code} / "
            f"{serial.serial_number or 'NO_SERIAL'}"
        )

        if not serial.product_code:
            errors.append(f"SerialItem {label}: missing product_code.")

        if not serial.serial_number:
            errors.append(f"SerialItem {label}: missing serial_number.")

        if not serial.expiration_date:
            errors.append(f"SerialItem {label}: missing expiration_date.")

        if serial.product_code:
            serial_product_codes.add(serial.product_code)

        if serial.product_code and serial.product_code not in order_product_codes:
            errors.append(
                f"SerialItem {label}: product_code appears in factory confirmation "
                f"but not in hospital order items."
            )

    # ------------------------------------------------------------
    # 7. confirmed quantity 检查
    # ------------------------------------------------------------
    confirmed_items = [
        item for item in order_items
        if item.confirmed_quantity and item.confirmed_quantity > 0
    ]

    if selected_confirmation and not confirmed_items:
        errors.append(
            "No OrderItem has confirmed_quantity > 0. "
            "Factory confirmation may not have been applied correctly."
        )

    serial_count_by_code = Counter(
        serial.product_code
        for serial in serial_items
        if serial.product_code
    )

    for item in order_items:
        serial_count = serial_count_by_code.get(item.product_code, 0)

        if item.confirmed_quantity != serial_count:
            warnings.append(
                f"OrderItem {item.product_code}: confirmed_quantity="
                f"{item.confirmed_quantity}, but SerialItem count={serial_count}."
            )

    # ------------------------------------------------------------
    # 8. 汇总结果
    # ------------------------------------------------------------
    can_generate_documents = len(errors) == 0

    if errors:
        validation_status = Order.DocumentValidationStatus.BLOCKED
    elif warnings:
        validation_status = Order.DocumentValidationStatus.NEEDS_REVIEW
    else:
        validation_status = Order.DocumentValidationStatus.READY

    result = {
        "can_generate_documents": can_generate_documents,
        "validation_status": validation_status,
        "order_id": order.id,
        "bon_de_commande": order_bon,
        "selected_factory_confirmation_id": (
            selected_confirmation.id if selected_confirmation else None
        ),
        "errors": errors,
        "warnings": warnings,
        "checked_at": timezone.now().isoformat(),
    }

    if save:
        order.document_validation_status = validation_status
        order.document_validation_data = result
        order.validated_at = timezone.now()
        order.save(
            update_fields=[
                "document_validation_status",
                "document_validation_data",
                "validated_at",
                "updated_at",
            ]
        )

    return result
