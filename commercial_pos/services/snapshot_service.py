"""Validate frozen snapshots without consulting current prices or regenerating."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from documents.models import GeneratedDocument

from .price_service import CommercialPOError, MONEY_QUANT
from .batch_facts_service import fingerprint
from .factory_payload_service import validate_frozen_payload


def _decimal(value, *, integer=False):
    try:
        result = Decimal(value)
        if not isinstance(value, str) or not result.is_finite() or result <= 0:
            raise ValueError
        if integer and result != result.to_integral_value():
            raise ValueError
        if not integer and result != result.quantize(MONEY_QUANT):
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise CommercialPOError("Invalid frozen quantity or amount.")


def validated_snapshot(document):
    data = document.source_data
    try:
        if (document.document_type != GeneratedDocument.DocumentType.COMMERCIAL_PO
                or not isinstance(data, dict) or data["schema_version"] != 1
                or data["document_type"] != "commercial_po" or data["currency"] != "EUR"
                or data["order_id"] != document.order_id
                or data["shipment_batch_id"] != document.shipment_batch_id
                or not document.shipment_batch_id
                or document.shipment_batch.order_id != document.order_id
                or data["batch_number"] != document.shipment_batch.batch_number
                or data["document_number"] != document.document_number
                or not isinstance(data["bon_de_commande"], str) or not data["bon_de_commande"]
                or not isinstance(data["hospital_name"], str)
                or type(data["price_policy_id"]) is not int or data["price_policy_id"] <= 0
                or data["issuer"] != "Acoeur"
                or not data["shipping_date_source"]
                or not re.fullmatch(r"[0-9a-f]{64}", data["facts_fingerprint"])):
            raise CommercialPOError("Commercial snapshot identity is invalid.")
        fact_keys = ("order_id", "bon_de_commande", "hospital_id", "hospital_name", "shipment_batch_id",
                     "batch_number", "shipping_date", "shipping_date_source", "source_type", "source_id",
                     "shipping_address", "serials")
        facts = {key: data[key] for key in fact_keys}
        facts["items"] = [{key: row[key] for key in ("batch_item_id", "order_item_id", "product_id", "product_code", "description", "quantity")}
                          for row in data["items"]]
        if fingerprint(facts) != data["facts_fingerprint"]:
            raise CommercialPOError("Frozen batch facts fingerprint is inconsistent.")
        shipping_date = date.fromisoformat(data["shipping_date"])
        for field, lower in (("rule_start_date", True), ("rule_end_date", False)):
            if data[field]:
                boundary = date.fromisoformat(data[field])
                if (lower and shipping_date < boundary) or (not lower and shipping_date > boundary):
                    raise CommercialPOError("Frozen price interval conflicts with shipment date.")
        price = _decimal(data["unit_price"])
        units, amount = Decimal("0"), Decimal("0.00")
        products = set()
        if not isinstance(data["items"], list) or not data["items"]:
            raise CommercialPOError("Commercial snapshot has no lines.")
        for row in data["items"]:
            code = row["product_code"]
            if not isinstance(code, str) or not code or code in products or not isinstance(row["description"], str):
                raise CommercialPOError("Invalid frozen product line.")
            products.add(code)
            quantity = _decimal(row["quantity"], integer=True)
            line_amount = _decimal(row["line_amount"])
            if _decimal(row["unit_price"]) != price or line_amount != (quantity * price).quantize(MONEY_QUANT):
                raise CommercialPOError("Commercial snapshot totals are inconsistent.")
            units += quantity
            amount += line_amount
        if units != _decimal(data["total_units"], integer=True) or amount != _decimal(data["total_amount"]):
            raise CommercialPOError("Commercial snapshot total is inconsistent.")
        validate_frozen_payload(data)
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as exc:
        raise CommercialPOError("Commercial snapshot is missing or damaged.") from exc
    return data
