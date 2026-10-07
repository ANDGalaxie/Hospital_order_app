"""Read only shipping facts. No factory prices, discounts or workflow sync."""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from workflow.services.batch_price_snapshot_service import get_batch_pricing_serial_rows
from workflow.services.workflow_document_generation_service import (
    get_batch_shipping_date, get_po_shipping_address_lines,
)

from .price_service import CommercialPOError
from shipments.services.shipment_validation_service import check_duplicate_serials_global


def fingerprint(facts):
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def read_batch_facts(batch):
    # This existing helper has no order/today fallback.
    shipping_date = get_batch_shipping_date(batch)
    if bool(batch.factory_confirmation_id) == bool(batch.inventory_allocation_id):
        raise CommercialPOError("Exactly one supported batch source is required.")
    if batch.factory_confirmation_id:
        source = batch.factory_confirmation
        if batch.source_type != "factory_confirmation" or source.order_id != batch.order_id:
            raise CommercialPOError("Factory confirmation does not belong to this batch order.")
        if not source.shipping_date or source.shipping_date != shipping_date:
            raise CommercialPOError("Batch date and factory shipping date are missing or conflict.")
        if source.serial_items.exclude(order_id=batch.order_id).exists():
            raise CommercialPOError("Factory serials contain a different order.")
        serial_objects = source.serial_items.all()
        date_source = "shipment_batch.batch_date / factory_confirmation.shipping_date"
    else:
        source = batch.inventory_allocation
        if batch.source_type != "inventory_allocation" or source.order_id != batch.order_id:
            raise CommercialPOError("Inventory allocation does not belong to this batch order.")
        if source.items.exclude(allocated_order_id=batch.order_id).exists():
            raise CommercialPOError("Inventory serials contain an unassigned or different order.")
        serial_objects = source.items.all()
        date_source = "shipment_batch.batch_date"

    order_items = defaultdict(list)
    for item in batch.order.items.select_related("product").all():
        order_items[item.product_code.strip()].append(item)
    items = []
    quantities = {}
    for item in batch.shipped_items.select_related("product").order_by("product_code", "id"):
        code = item.product_code.strip()
        if not code or code in quantities or len(order_items[code]) != 1:
            raise CommercialPOError(f"Ambiguous batch product {code}.")
        order_item = order_items[code][0]
        if item.product_id != order_item.product_id or (item.product and item.product.code.strip() != code):
            raise CommercialPOError(f"Batch product identity conflicts for {code}.")
        quantity = Decimal(item.shipped_quantity)
        if quantity <= 0:
            raise CommercialPOError(f"Invalid batch quantity for {code}.")
        quantities[code] = quantity
        items.append({
            "batch_item_id": item.pk, "order_item_id": order_item.pk,
            "product_id": item.product_id, "product_code": code,
            "description": (order_item.product.description if order_item.product else "") or order_item.description or "",
            "quantity": str(quantity),
        })
    if not items:
        raise CommercialPOError("Batch has no physical product quantities.")

    # Validate raw quantities before using the existing read helper (which has a legacy fallback).
    for serial in serial_objects:
        code = serial.product_code.strip()
        if code not in quantities or serial.product_id != order_items[code][0].product_id:
            raise CommercialPOError("Serial product is outside this batch or has a conflicting identity.")
        raw = serial.raw_data
        if raw is not None and not isinstance(raw, dict):
            raise CommercialPOError("Malformed serial facts.")
        if batch.factory_confirmation_id and isinstance(raw, dict) and "delivered_quantity" in raw:
            try:
                quantity = Decimal(str(raw["delivered_quantity"]))
                valid = quantity.is_finite() and quantity > 0 and quantity == quantity.to_integral_value()
            except (InvalidOperation, ValueError, TypeError):
                valid = False
            if not valid:
                raise CommercialPOError("Invalid delivered serial quantity.")

    serials = []
    serial_quantities = defaultdict(lambda: Decimal("0"))
    serial_numbers = set()
    for row in get_batch_pricing_serial_rows(batch):
        number, code = row["serial_number"], row["product_code"]
        if code not in quantities or not number or number in serial_numbers or not row["expiration_date"]:
            raise CommercialPOError("Missing, duplicate or out-of-batch serial facts.")
        serial_numbers.add(number)
        serial_quantities[code] += row["quantity"]
        serials.append({
            "source": row["source"], "source_id": row["source_id"],
            "product_code": code, "serial_number": number,
            "expiration_date": row["expiration_date"].isoformat(), "quantity": str(row["quantity"]),
        })
    if dict(serial_quantities) != quantities:
        raise CommercialPOError("Serial quantities do not match this batch's physical quantities.")
    if check_duplicate_serials_global(batch, serials):
        raise CommercialPOError("Serial identity conflicts with another shipment source.")
    if not batch.batch_number or not batch.order.bon_de_commande:
        raise CommercialPOError("Batch number and original BON are required.")
    return {
        "order_id": batch.order_id, "bon_de_commande": batch.order.bon_de_commande,
        "hospital_id": batch.order.hospital_id,
        "hospital_name": batch.order.hospital_name or (batch.order.hospital.name if batch.order.hospital else ""),
        "shipment_batch_id": batch.pk, "batch_number": batch.batch_number,
        "shipping_date": shipping_date.isoformat(), "shipping_date_source": date_source,
        "source_type": batch.source_type, "source_id": source.pk,
        "shipping_address": get_po_shipping_address_lines(batch.order),
        "items": items, "serials": serials,
    }
