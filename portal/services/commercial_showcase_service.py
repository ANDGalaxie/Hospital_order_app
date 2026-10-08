"""Dedicated display DTOs. Never return business models, validation JSON or costs."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.core.exceptions import SuspiciousFileOperation
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.dateparse import parse_date

from commercial_pos.services.price_service import CommercialPOError
from commercial_pos.services.snapshot_service import validated_snapshot
from documents.models import GeneratedDocument
from shipments.models import ShipmentBatch
from portal.services.common import get_global_numeric_bon_ordinals, get_user_display_name
from portal.commercial_access import document_matches_batch


def money_display(value):
    return f"{Decimal(value):,.2f} €".replace(",", " ")


def file_links(document):
    links = {}
    for kind in ("pdf", "html"):
        field = getattr(document, f"{kind}_file")
        try:
            available = field and field.storage.exists(field.name)
        except (OSError, SuspiciousFileOperation):
            available = False
        links[kind] = (reverse("portal:commercial_document_file", args=[document.pk, kind])
                       if available else None)
    links["download"] = links["pdf"] + "?download=1" if links["pdf"] else None
    return links


def document_dto(document):
    if document is None:
        return {"status": _("未生成"), "ready": False, "number": "", "amount": None}
    if not document_matches_batch(document):
        return {"status": _("暂不可用"), "ready": False, "number": "", "amount": None}
    if document.document_type == "commercial_po":
        try:
            data = validated_snapshot(document)
        except CommercialPOError:
            return {"status": _("暂不可用"), "ready": False, "number": "", "amount": None}
        amount = money_display(data["total_amount"])
    else:
        amount = None
    links = file_links(document)
    return {"status": _("已生成") if links["pdf"] else _("文件暂不可用"),
            "ready": bool(links["pdf"]), "number": document.document_number, "amount": amount, **links}


def _batch_rows(query="", batch_id=None, *, include_invoice=False):
    batches = ShipmentBatch.objects.all()
    if query:
        batches = batches.filter(Q(order__bon_de_commande__icontains=query)
                                 | Q(order__hospital_name__icontains=query)
                                 | Q(order__hospital__name__icontains=query))
    if batch_id is not None:
        batches = batches.filter(pk=batch_id)
    facts = list(batches.annotate(physical_units=Sum("shipped_items__shipped_quantity")).order_by(
        F("batch_date").desc(nulls_last=True), "-batch_number", "-id",
    ).values("id", "order_id", "order__bon_de_commande", "order__hospital_name",
             "order__hospital__name", "batch_number", "batch_date", "physical_units"))
    allowed = {row["id"]: row["order_id"] for row in facts}
    documents = {}
    for doc in GeneratedDocument.objects.filter(
        shipment_batch_id__in=allowed, document_type__in=["hospital_invoice", "commercial_po"] if include_invoice else ["commercial_po"],
    ).select_related("shipment_batch").order_by("id"):
        if doc.order_id == allowed[doc.shipment_batch_id]:
            documents.setdefault((doc.shipment_batch_id, doc.document_type), doc)
    ordinals = get_global_numeric_bon_ordinals()
    rows = []
    for fact in facts:
        batch_id = fact["id"]
        row = {
            "batch_id": batch_id, "bon": fact["order__bon_de_commande"],
            "ordinal": ordinals.get(fact["order_id"]), "batch_number": fact["batch_number"],
            "hospital": fact["order__hospital_name"] or fact["order__hospital__name"] or "—",
            "shipping_date": fact["batch_date"],
            "quantity": fact["physical_units"] or 0,
            "commercial_po": document_dto(documents.get((batch_id, "commercial_po"))),
            "detail_url": reverse("portal:commercial_operations_detail" if include_invoice else "portal:commercial_purchase_detail", args=[batch_id]),
        }
        if include_invoice:
            row["invoice"] = document_dto(documents.get((batch_id, "hospital_invoice")))
        row["status_class"] = "success" if row["commercial_po"]["ready"] else "warning"
        rows.append(row)
    return rows


def build_showcase_list_context(request, *, operations=False):
    query = (request.GET.get("q") or "").strip()[:200]
    status = request.GET.get("status", "all")
    if status not in {"all", "generated", "missing"}:
        status = "all"
    rows = _batch_rows(query, include_invoice=operations)
    generated = sum(row["commercial_po"]["ready"] for row in rows)
    cards = [{"label": _("发货批次"), "value": len(rows)},
             {"label": _("已生成新版 PO"), "value": generated},
             {"label": _("暂未生成或不可用"), "value": len(rows) - generated},
             {"label": _("对应 BON"), "value": len({row["bon"] for row in rows})}]
    for card, value in zip(cards, ("all", "generated", "missing", "all")):
        card.update(count=card["value"], url="?" + urlencode({"status": value, "q": query}), active=status == value)
    if status != "all":
        rows = [row for row in rows if row["commercial_po"]["ready"] == (status == "generated")]
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return {"page_title": _("操作平台") if operations else _("采购订单"),
            "operations": operations, "page_obj": page, "query": query, "status_filter": status,
            "query_without_page": urlencode({"q": query, "status": status}), "cards": cards,
            "user_display_name": get_user_display_name(request.user)}


def build_showcase_batch_context(request, batch_id, *, operations=None):
    from django.http import Http404
    if operations is None:
        operations = request.GET.get("module") == "operations"
    rows = _batch_rows(batch_id=batch_id, include_invoice=operations)
    if not rows:
        raise Http404("Batch not found")
    row = rows[0]
    batch = ShipmentBatch.objects.get(pk=batch_id)
    # Explicit projections only; models and original pricing/validation JSON are
    # never handed to the template. Source association must match this order.
    products = list(batch.shipped_items.order_by("product_code", "id").values(
        "product_id", "product_code", "product__description", "shipped_quantity"))
    serials = []
    if batch.factory_confirmation_id and batch.factory_confirmation.order_id == batch.order_id:
        serials = list(batch.factory_confirmation.serial_items.filter(order_id=batch.order_id).values(
            "product_id", "product_code", "serial_number", "expiration_date"))
    elif batch.inventory_allocation_id and batch.inventory_allocation.order_id == batch.order_id:
        serials = list(batch.inventory_allocation.items.filter(allocated_order_id=batch.order_id).values(
            "product_id", "product_code", "serial_number", "expiration_date"))
    identities = {(p["product_code"], p["product_id"]) for p in products}
    serials = [{key: s[key] for key in ("product_code", "serial_number", "expiration_date")}
               for s in serials if (s["product_code"], s["product_id"]) in identities]
    for product in products:
        product["serial_count"] = sum(s["product_code"] == product["product_code"] for s in serials)
    priced_items = []
    price_summary = None
    document = GeneratedDocument.objects.filter(shipment_batch_id=batch_id, order_id=batch.order_id, document_type="commercial_po").first()
    if document:
        try:
            data = validated_snapshot(document)
            if operations:
                # Only the purchase order's frozen render payload is authoritative
                # for this detail. Project four business fields, never the payload
                # itself or its factory metadata, into the template context.
                payload = data["po_data"]
                priced_items = [{
                    "product_code": item["product_code"],
                    "quantity": Decimal(str(item["quantity_raw"])),
                    "unit_price": f"€{Decimal(str(item['unit_price_raw'])):,.2f}",
                    "amount": f"€{Decimal(str(item['amount_raw'])):,.2f}",
                } for item in payload["items"]]
                price_summary = {
                    # Historical render payloads omit units in totals; their
                    # canonical units are frozen on this same document.
                    "quantity": Decimal(str(payload["totals"].get("total_units_raw", data["total_units"]))),
                    "amount": f"€{Decimal(str(payload['totals']['total_raw'])):,.2f}",
                }
                row["shipping_date"] = date.fromisoformat(data["shipping_date"])
                row["commercial_po"]["amount"] = price_summary["amount"]
            else:
                priced_items = [{"product_code": p["product_code"], "quantity": p["quantity"],
                                 "unit_price": money_display(p["unit_price"]), "amount": money_display(p["line_amount"])} for p in data["items"]]
        except (CommercialPOError, KeyError, TypeError, ValueError, InvalidOperation):
            priced_items = []
            price_summary = None
    context = {"row": row, "page_title": _("操作平台") if operations else _("采购订单"), "operations": operations,
               "products": products, "serial_rows": serials, "price_rows": priced_items,
               "product_count": len(products), "serial_count": len(serials),
               "list_url": reverse("portal:commercial_operations" if operations else "portal:commercial_purchase_orders"),
               "user_display_name": get_user_display_name(request.user)}
    if operations:
        context["price_summary"] = price_summary
    return context


def build_commercial_finance_context(request):
    from portal.services.commercial_finance_service import build_finance_context
    return build_finance_context(request)
