"""Clone the same batch's frozen factory payload; patch prices only."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import re

from documents.models import GeneratedDocument
from documents.services.document_generation_service import (
    format_po_discount, format_po_eur, format_po_unit_price,
)

from .batch_facts_service import fingerprint
from .price_service import CommercialPOError, MONEY_QUANT


# Exact paths only. Quantities, row order, identities and all other keys survive.
PRICE_FIELD_ALLOWLIST = {
    "items[*]": frozenset({
        "unit_price_raw", "unit_price", "factory_base_unit_price",
        "discount_rate", "discount_rate_raw", "discount", "discount_note",
        "factory_net_unit_price", "final_unit_price_raw", "final_unit_price",
        "amount_raw", "amount", "line_total",
    }),
    "totals": frozenset({"total_raw", "total"}),
}


def non_price_payload(po_data):
    """Comparison helper: remove only explicitly allowed price paths."""
    result = deepcopy(po_data)
    for row in result["items"]:
        for key in PRICE_FIELD_ALLOWLIST["items[*]"]:
            row.pop(key, None)
    for key in PRICE_FIELD_ALLOWLIST["totals"]:
        result["totals"].pop(key, None)
    return result


def frozen_row_quantity(row):
    try:
        quantity = Decimal(str(row["quantity_raw"]))
        displayed = Decimal(str(row["quantity"]).replace(",", ""))
        if (not quantity.is_finite() or quantity <= 0
                or quantity != quantity.to_integral_value() or quantity != displayed):
            raise ValueError
        return quantity
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CommercialPOError("Factory PO frozen row quantity is missing or inconsistent.") from exc


def source_factory_po(batch):
    try:
        document = GeneratedDocument.objects.get(
            shipment_batch_id=batch.pk, document_type=GeneratedDocument.DocumentType.FACTORY_PO,
        )
    except GeneratedDocument.DoesNotExist as exc:
        raise CommercialPOError("This batch has no generated Factory PO; commercial generation requires its frozen po_data.") from exc
    except GeneratedDocument.MultipleObjectsReturned as exc:
        raise CommercialPOError("This batch has multiple Factory POs; the frozen source is ambiguous.") from exc
    if document.order_id != batch.order_id:
        raise CommercialPOError("Factory PO does not belong to this batch order.")
    source = document.source_data
    try:
        payload = source["po_data"]
        if source.get("shipment_batch_id", batch.pk) != batch.pk:
            raise ValueError
        for key in ("po", "company", "factory", "totals"):
            if not isinstance(payload[key], dict):
                raise ValueError
        if not payload["po"]["po_number"] or not isinstance(payload["shipping_address"], list):
            raise ValueError
        if not isinstance(payload["items"], list) or not payload["items"]:
            raise ValueError
        for row in payload["items"]:
            if not row["product_code"] or not isinstance(row["description"], str):
                raise ValueError
        units = sum((frozen_row_quantity(row) for row in payload["items"]), Decimal("0"))
        totals = payload["totals"]
        if (("total_units_raw" in totals and Decimal(str(totals["total_units_raw"])) != units)
                or ("total_units" in totals and Decimal(str(totals["total_units"]).replace(",", "")) != units)):
            raise ValueError
        debug = payload.get("debug", {})
        if debug.get("shipment_batch_id", batch.pk) != batch.pk or debug.get("order_id", batch.order_id) != batch.order_id:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as exc:
        raise CommercialPOError("Factory PO frozen po_data is missing or inconsistent.") from exc
    return document


def factory_html_fingerprint(document):
    if not document.html_file or not document.html_file.storage.exists(document.html_file.name):
        raise CommercialPOError("Source Factory PO HTML is unavailable; visual parity cannot be verified.")
    with document.html_file.open("rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def reprice_factory_payload(original, unit_price):
    result = deepcopy(original)
    total = Decimal("0.00")
    for row in result["items"]:
        amount = (frozen_row_quantity(row) * unit_price).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        values = {
            "unit_price_raw": str(unit_price), "unit_price": format_po_unit_price(unit_price),
            "factory_base_unit_price": str(unit_price), "discount_rate": "0.00",
            "discount_rate_raw": 0.0, "discount": format_po_discount(0),
            "discount_note": zero_discount_note(row.get("discount_note", "")),
            "factory_net_unit_price": str(unit_price), "final_unit_price_raw": str(unit_price),
            "final_unit_price": format_po_unit_price(unit_price),
            "amount_raw": str(amount), "amount": format_po_eur(amount), "line_total": str(amount),
        }
        for key in PRICE_FIELD_ALLOWLIST["items[*]"]:
            if key in row:
                row[key] = values[key]
        # The canonical template consumes these fields; do not invent a row.
        if not {"unit_price", "amount", "discount"}.issubset(row):
            raise CommercialPOError("Factory PO frozen row lacks template price fields.")
        total += amount
    result["totals"]["total_raw"] = str(total)
    result["totals"]["total"] = format_po_eur(total)
    if non_price_payload(result) != non_price_payload(original):
        raise CommercialPOError("Commercial payload changed a non-price field.")
    return result


def zero_discount_note(note):
    # Keep the historical expiration/threshold text and its row geometry.
    # Only the percentage is a price field; it must describe zero discount.
    if not note:
        return note
    if not isinstance(note, str):
        raise CommercialPOError("Factory PO discount note is malformed.")
    result, count = re.subn(r"\d+(?:\.\d+)?%", "0%", note, count=1)
    if not count:
        raise CommercialPOError("Factory PO discount note cannot be safely repriced.")
    return result


def non_price_html(html):
    """Compare rendered structure/text, masking exact template price nodes."""
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for row in soup.select(".po-items-table tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 5:
            raise CommercialPOError("Factory PO HTML has an unsupported item layout.")
        for cell in cells[2:]:
            cell.clear()
            cell.append("PRICE")
    for node in soup.select(".discount-note"):
        text = re.sub(r"\d+(?:\.\d+)?%", "PRICE%", node.get_text(), count=1)
        node.clear()
        node.append(text)
    for node in html_amount_nodes(soup):
        node.clear()
        node.append("PRICE")
    for text in soup.find_all(string=True):
        if text.parent.name not in ("style", "script"):
            text.replace_with(" ".join(text.split()))
    # CSS, logo URLs, identities, quantities and every non-price node remain.
    return str(soup)


def html_amount_nodes(soup):
    nodes = soup.select(".total-row.amount .total-value")
    if nodes:
        return nodes
    # Older frozen Factory HTML has one total-box without total-row wrappers.
    nodes = soup.select(".total-box > .total-value")
    labels = soup.select(".total-box > .total-label")
    if len(nodes) == len(labels) == 1 and labels[0].get_text(strip=True) == "Total":
        return nodes
    raise CommercialPOError("Factory PO HTML total layout is unsupported.")


def reprice_frozen_factory_html(html, original, commercial):
    """Authorised historical exception: retain frozen HTML and patch prices."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".po-items-table tbody tr")
    if len(rows) != len(original["items"]):
        raise CommercialPOError("Factory PO HTML rows conflict with its frozen po_data.")
    normal = lambda text: " ".join(str(text).split())
    visible = normal(soup.get_text(" ", strip=True))
    # Every template-consumed frozen identity must be present in the source HTML.
    expected = [original["po"]["po_number"], original["po"]["order_date"], original["po"]["expected_arrival"],
                original["factory"]["factory_name"], original["factory"]["buyer"],
                original["company"]["company_name"], original["company"]["registration_no"],
                original["company"]["po_company"]["display_name"]]
    expected += original["shipping_address"] + original["factory"]["factory_address"] + original["company"]["po_company"]["address"]
    if original["po"].get("source"):
        expected.append(original["po"]["source"])
    if any(normal(value) not in visible for value in expected if value):
        raise CommercialPOError("Factory PO HTML identity conflicts with its frozen po_data.")
    for html_row, source_row, priced_row in zip(rows, original["items"], commercial["items"]):
        cells = html_row.find_all("td", recursive=False)
        code, description = html_row.select_one(".product-code"), html_row.select_one(".product-description")
        if (len(cells) != 5 or code is None or description is None
                or normal(code.get_text()) != normal(source_row["product_code"])
                or normal(description.get_text()) != normal(source_row["description"])
                or normal(cells[1].get_text()) != normal(source_row["quantity"])):
            raise CommercialPOError("Factory PO HTML product/quantity conflicts with its frozen po_data.")
        for cell, field in zip(cells[2:], ("unit_price", "discount", "amount")):
            cell.clear()
            cell.append(priced_row[field])
        note = html_row.select_one(".discount-note")
        if note is not None:
            if normal(note.get_text()) != normal(source_row.get("discount_note", "")):
                raise CommercialPOError("Factory PO HTML discount note conflicts with its frozen po_data.")
            note.clear()
            note.append(priced_row["discount_note"])
        elif source_row.get("discount_note"):
            raise CommercialPOError("Factory PO HTML is missing its frozen discount note.")
    amounts = html_amount_nodes(soup)
    if len(amounts) != 1:
        raise CommercialPOError("Factory PO HTML total layout is unsupported.")
    amounts[0].clear()
    amounts[0].append(commercial["totals"]["total"])
    result = str(soup)
    if non_price_html(result) != non_price_html(html):
        raise CommercialPOError("Historical Commercial HTML changed non-price content.")
    return result


def validate_frozen_payload(snapshot):
    """Validate new render snapshots on reads without looking up live prices."""
    if "source_factory_po_document_id" not in snapshot:
        if "po_data" in snapshot:
            raise CommercialPOError("Commercial render payload has no frozen Factory PO provenance.")
        return  # Legacy schema-1 snapshots remain readable until explicit regeneration.
    try:
        payload = snapshot["po_data"]
        if (type(snapshot["source_factory_po_document_id"]) is not int
                or snapshot["source_factory_po_document_id"] <= 0
                or not snapshot["source_factory_po_document_number"]
                or any(not re.fullmatch(r"[0-9a-f]{64}", snapshot[key]) for key in (
                    "source_factory_po_fingerprint", "source_factory_po_non_price_fingerprint", "commercial_payload_fingerprint"))
                or fingerprint(non_price_payload(payload)) != snapshot["source_factory_po_non_price_fingerprint"]
                or fingerprint(payload) != snapshot["commercial_payload_fingerprint"]
                or reprice_factory_payload(payload, Decimal(snapshot["unit_price"])) != payload
                or Decimal(str(payload["totals"]["total_raw"])) != Decimal(snapshot["total_amount"])
                or sum((frozen_row_quantity(r) for r in payload["items"]), Decimal(0)) != Decimal(snapshot["total_units"])):
            raise CommercialPOError("Frozen Commercial render payload is inconsistent.")
        if snapshot["commercial_pricing_basis"] != {
            "reference_date": snapshot["shipping_date"], "reference_date_source": snapshot["shipping_date_source"],
            "price_policy_id": snapshot["price_policy_id"], "unit_price": snapshot["unit_price"], "currency": "EUR",
        }:
            raise CommercialPOError("Frozen Commercial pricing audit is inconsistent.")
        quantities = {}
        for row in payload["items"]:
            code = row["product_code"]
            quantities[code] = quantities.get(code, Decimal(0)) + frozen_row_quantity(row)
        if quantities != {r["product_code"]: Decimal(r["quantity"]) for r in snapshot["items"]}:
            raise CommercialPOError("Frozen display quantities conflict with the render payload.")
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as exc:
        raise CommercialPOError("Frozen Commercial render payload is damaged.") from exc
