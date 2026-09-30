"""Explicit historical PO repair only; never called by normal generation."""
import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.apps import apps
from django.db import connection, transaction

from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_numbering_service import (
    audit_document_sequences, build_invoice_number, build_po_number,
)
from orders.models import Order
from settlements.models import SettlementAccount, PaymentTransaction
from workflow.models import DocumentWorkflowItem
from legacy_services.factory_po_generator import render_po_html, write_html_and_pdf

PREFIX = "__PO_RENUMBER_TMP__"
PO_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])DELAHK[0-9]+S(?:-B[0-9]+)?(?![A-Za-z0-9_-])")
MODELS = (DocumentSequence, GeneratedDocument, DocumentWorkflowItem, SettlementAccount, PaymentTransaction)


class RepairError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def database_state():
    return {
        model._meta.label: list(model.objects.order_by("pk").values())
        for model in MODELS
    }


def protected_business_hashes():
    labels = (
        "orders.Order", "orders.OrderItem", "shipments.ShipmentBatch",
        "shipments.ShipmentBatchItem", "factory_confirmations.FactoryConfirmation",
        "factory_confirmations.SerialItem", "pricing.PricePolicy",
        "products.Product", "factories.Factory",
    )
    return {
        label: digest(list(apps.get_model(label).objects.order_by("pk").values()))
        for label in labels
    }


def invoice_file_hashes():
    result = {}
    for document in GeneratedDocument.objects.filter(document_type="hospital_invoice"):
        for field in (document.pdf_file, document.html_file):
            if field:
                path = media_path(field.name)
                result[str(path)] = file_digest(path) if path.is_file() else None
    return result


def media_path(value):
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / str(value)).resolve()
    if not path.is_relative_to(root) or path == root:
        raise RepairError(f"File path escapes MEDIA_ROOT: {value}")
    return path


def patch_po_snapshot(source, old_base, old_number, new_base, new_number):
    """Only existing explicitly named PO number leaves may change."""
    changes = []
    def visit(value, path=()):
        if isinstance(value, dict):
            return {key: visit(child, path + (key,)) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child, path + (str(index),)) for index, child in enumerate(value)]
        if isinstance(value, str) and path:
            key = path[-1]
            if key == "base_po_number":
                if value not in (old_base, new_base):
                    raise RepairError(f"Unexpected PO snapshot at {'.'.join(path)}: {value}")
                target = new_base
            elif key in ("po_number", "document_number") and PO_TOKEN.fullmatch(value):
                if value not in (old_number, new_number):
                    raise RepairError(f"Unexpected PO snapshot at {'.'.join(path)}: {value}")
                target = new_number
            else:
                # Never alter invoice snapshots or arbitrary historical business text.
                if PO_TOKEN.search(value) and key not in ("invoice_number", "base_invoice_number"):
                    raise RepairError(f"Unclassified PO reference at {'.'.join(path)}; manual review required")
                return value
            if target != value:
                changes.append({"path": ".".join(path), "old": value, "new": target})
            return target
        return value
    patched = visit(copy.deepcopy(source))
    return patched, changes


def snapshot_ready(source, doc_id):
    data = source.get("po_data") if isinstance(source, dict) else None
    if not isinstance(data, dict):
        raise RepairError(f"Document {doc_id}: missing frozen po_data")
    for key in ("po", "company", "factory", "totals"):
        if not isinstance(data.get(key), dict):
            raise RepairError(f"Document {doc_id}: missing snapshot {key}")
    for parent, keys in (
        ("po", ("po_number", "order_date", "expected_arrival")),
        ("company", ("logo_path", "po_company", "company_name", "registration_no")),
        ("factory", ("factory_name", "factory_address", "buyer")),
        ("totals", ("total",)),
    ):
        if any(key not in data[parent] for key in keys):
            raise RepairError(f"Document {doc_id}: incomplete snapshot {parent}")
    if not isinstance(data.get("shipping_address"), list) or not data.get("items"):
        raise RepairError(f"Document {doc_id}: missing address/items")
    for item in data["items"]:
        if not isinstance(item, dict) or any(
            key not in item for key in ("product_code", "description", "quantity", "unit_price", "discount", "amount")
        ):
            raise RepairError(f"Document {doc_id}: incomplete historical item")
    return data


def build_plan():
    """SELECT and file reads only; even dry-run creates no directories."""
    state = database_state()
    for record in DocumentSequence.objects.all():
        orders = list(Order.objects.filter(bon_de_commande=record.bon_de_commande))
        if len(orders) != 1 or not orders[0].order_date:
            raise RepairError(f"Sequence {record.pk}: unique dated Order required")
        order_date = orders[0].order_date
        if (
            order_date.strftime("%Y-%m") != record.month_key
            or record.sequence < 1
            or build_invoice_number(order_date, record.sequence) != record.invoice_number
        ):
            raise RepairError(f"Sequence {record.pk}: frozen date/Invoice inconsistency")
    sequences = []
    documents = []
    auxiliary = []
    all_documents = {row["id"]: row for row in state["documents.GeneratedDocument"]}
    for row in audit_document_sequences():
        if not row["invoice_ok"]:
            raise RepairError(f"Sequence {row['document_sequence_id']}: Invoice inconsistency; out of scope")
        orders = list(Order.objects.filter(bon_de_commande=row["bon_de_commande"]))
        if len(orders) != 1 or not orders[0].order_date:
            raise RepairError(f"Sequence {row['document_sequence_id']}: unique dated Order required")
        order = orders[0]
        if order.order_date.strftime("%Y-%m") != row["month_key"]:
            raise RepairError(f"Sequence {row['document_sequence_id']}: order date/month mismatch")
        target = build_po_number(order.order_date, row["sequence"])
        if build_invoice_number(order.order_date, row["sequence"]) != row["stored_invoice"]:
            raise RepairError("Frozen Invoice mismatch")
        seq = {**row, "order_id": order.pk, "order_date": order.order_date.isoformat(), "target": target}
        sequences.append(seq)
        found = list(GeneratedDocument.objects.filter(
            order=order, document_type="factory_po",
        ).select_related("shipment_batch").order_by("pk"))
        if not found:
            raise RepairError(f"Sequence {row['document_sequence_id']}: no Factory PO documents")
        for doc in found:
            batch = doc.shipment_batch
            if not batch or batch.order_id != order.pk or batch.batch_number < 1:
                raise RepairError(f"Document {doc.pk}: indeterminate batch")
            suffix = f"-B{batch.batch_number}" if batch.batch_number > 1 else ""
            old = row["stored_po"] + suffix
            new = target + suffix
            if doc.document_number != old:
                raise RepairError(f"Document {doc.pk}: document/sequence mismatch")
            snapshot_ready(doc.source_data, doc.pk)
            patched, changes = patch_po_snapshot(doc.source_data, row["stored_po"], old, target, new)
            if patched["po_data"]["po"]["po_number"] != new:
                raise RepairError(f"Document {doc.pk}: missing PO title snapshot")
            files = []
            for kind, field in (("html", doc.html_file), ("pdf", doc.pdf_file)):
                path = media_path(field.name)
                if not path.is_file():
                    raise RepairError(f"Document {doc.pk}: missing {kind} {path}")
                target_path = path.with_name(f"Purchase_Order_{new}.{kind}")
                files.append({"kind": kind, "old": str(path), "target": str(target_path), "sha256": file_digest(path)})
            html = Path(files[0]["old"])
            json_path = html.with_name(html.stem + "_data.json")
            if json_path.exists():
                if json.loads(json_path.read_text()) != doc.source_data:
                    raise RepairError(f"Document {doc.pk}: file/DB JSON snapshot mismatch")
                files.append({"kind": "json", "old": str(json_path),
                              "target": str(html.with_name(f"Purchase_Order_{new}_data.json")),
                              "sha256": file_digest(json_path)})
            documents.append({
                "id": doc.pk, "order_id": order.pk, "bon": order.bon_de_commande,
                "sequence_id": row["document_sequence_id"], "batch_id": batch.pk,
                "batch_number": batch.batch_number, "old": old, "target": new,
                "old_base": row["stored_po"], "target_base": target,
                "source_data": patched, "changes": changes, "files": files,
            })
    ids = {doc["id"] for doc in documents}
    targets = [doc["target"] for doc in documents]
    if len(targets) != len(set(targets)):
        raise RepairError("Duplicate canonical targets")
    for existing in all_documents.values():
        if existing["document_type"] == "factory_po":
            if existing["document_number"].startswith(PREFIX):
                raise RepairError("Unfinished temporary numbering exists")
            if existing["id"] not in ids and existing["document_number"] in targets:
                raise RepairError(f"External canonical collision with document {existing['id']}")
    old_paths = [f["old"] for doc in documents for f in doc["files"]]
    target_paths = [f["target"] for doc in documents for f in doc["files"]]
    if len(old_paths) != len(set(old_paths)) or len(target_paths) != len(set(target_paths)):
        raise RepairError("Shared or duplicate repair paths")
    external_paths = {
        str(media_path(row[field]))
        for row in all_documents.values() if row["id"] not in ids
        for field in ("pdf_file", "html_file") if row[field]
    }
    for path in target_paths + old_paths:
        if path in external_paths:
            raise RepairError(f"Path used by external document: {path}")
    for path in target_paths:
        if Path(path).exists() and path not in old_paths:
            raise RepairError(f"Occupied target path: {path}")
    # Existing contextual redundant PO strings can be changed without rebuilding FKs.
    by_id = {doc["id"]: doc for doc in documents}
    accounts = {row["id"]: row for row in state["settlements.SettlementAccount"]}
    for label, fields in (
        ("workflow.DocumentWorkflowItem", ("validation_data", "notes")),
        ("settlements.SettlementAccount", ("notes", "cancellation_reason")),
        ("settlements.PaymentTransaction", ("reference", "notes", "reversal_reason")),
    ):
        for record in state[label]:
            doc_id = (
                record["po_document_id"] if label.startswith("workflow.") else
                record["document_id"] if label.endswith("SettlementAccount") else
                accounts[record["account_id"]]["document_id"]
            )
            if doc_id not in by_id:
                continue
            doc = by_id[doc_id]
            def replace_refs(value):
                if isinstance(value, dict):
                    return {key: replace_refs(child) for key, child in value.items()}
                if isinstance(value, list):
                    return [replace_refs(child) for child in value]
                if isinstance(value, str):
                    mapping = {doc["old_base"]: doc["target_base"], doc["old"]: doc["target"]}
                    return PO_TOKEN.sub(lambda match: mapping.get(match[0], match[0]), value)
                return value
            updates = {field: replace_refs(record[field]) for field in fields}
            updates = {key: value for key, value in updates.items() if value != record[key]}
            if updates:
                auxiliary.append({"model": label, "id": record["id"], "updates": updates})
    plan = {
        "sequences": sequences, "documents": documents, "auxiliary_updates": auxiliary,
        "database_state": state,
        "protected_business_hashes": protected_business_hashes(),
        "invoice_file_hashes": invoice_file_hashes(),
        "template_sha256": file_digest(Path(settings.BASE_DIR) / "templates/factory_purchase_order.html"),
    }
    plan["fingerprint"] = digest(plan)
    return plan


def plan_summary(plan):
    return {
        "sequence_count": len(plan["sequences"]), "document_count": len(plan["documents"]),
        "fingerprint": plan["fingerprint"],
        "sequences": [{"id": row["document_sequence_id"], "bon": row["bon_de_commande"],
                       "sequence": row["sequence"], "old": row["stored_po"], "target": row["target"]}
                      for row in plan["sequences"]],
        "documents": [{key: row[key] for key in ("id", "bon", "batch_number", "old", "target", "files", "changes")}
                      for row in plan["documents"]],
        "auxiliary_updates": plan["auxiliary_updates"],
    }


def visible_html(html, number):
    class Visible(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip = 0
            self.parts = []
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip -= 1
        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)
    parser = Visible()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).replace(number, "__PO_NUMBER__").split())


def verify_pdf(path, number):
    import fitz
    with fitz.open(str(path)) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        if number not in text:
            raise RepairError(f"PDF does not contain {number}: {path}")
    return text


def expected_state(plan):
    expected = copy.deepcopy(plan["database_state"])
    seqs = {row["document_sequence_id"]: row for row in plan["sequences"]}
    docs = {row["id"]: row for row in plan["documents"]}
    for row in expected["documents.DocumentSequence"]:
        if row["id"] in seqs:
            row["po_number"] = seqs[row["id"]]["target"]
    for row in expected["documents.GeneratedDocument"]:
        if row["id"] in docs:
            doc = docs[row["id"]]
            row["document_number"] = doc["target"]
            row["source_data"] = doc["source_data"]
            for f in doc["files"]:
                if f["kind"] in ("pdf", "html"):
                    row[f["kind"] + "_file"] = str(Path(f["target"]).relative_to(Path(settings.MEDIA_ROOT).resolve()))
    for change in plan["auxiliary_updates"]:
        next(row for row in expected[change["model"]] if row["id"] == change["id"]).update(change["updates"])
    return expected


def verify_result(plan):
    if protected_business_hashes() != plan["protected_business_hashes"]:
        raise RepairError("Historical order/shipment/serial/price facts changed")
    if invoice_file_hashes() != plan["invoice_file_hashes"]:
        raise RepairError("Historical Invoice file changed")
    if database_state() != expected_state(plan):
        raise RepairError("Database invariant verification failed")
    if list(audit_document_sequences()):
        raise RepairError("Structural audit is not zero")
    for doc in plan["documents"]:
        for f in doc["files"]:
            path = Path(f["target"])
            if not path.is_file():
                raise RepairError(f"Missing target {path}")
            if f["kind"] == "pdf":
                verify_pdf(path, doc["target"])
            elif f["kind"] == "html":
                if doc["target"] not in path.read_text():
                    raise RepairError(f"HTML missing canonical PO: {path}")
            elif json.loads(path.read_text()) != doc["source_data"]:
                raise RepairError(f"JSON snapshot mismatch: {path}")


def apply_plan(plan, database_backup, expected_fingerprint):
    if plan["fingerprint"] != expected_fingerprint:
        raise RepairError("Audit changed since reviewed dry-run; abort")
    dump = Path(database_backup).resolve()
    if not dump.is_file() or dump.stat().st_size < 5 or dump.read_bytes()[:5] != b"PGDMP":
        raise RepairError("A confirmed PostgreSQL custom-format dump is required")
    if not plan["documents"]:
        return {"applied": 0, "sequence_count": 0}
    root = Path(settings.BASE_DIR) / "outputs"
    root.mkdir(exist_ok=True)
    for previous in root.glob("factory_po_number_repair_backup_*.json"):
        if json.loads(previous.read_text()).get("status") in ("prepared", "applying", "rollback_failed"):
            raise RepairError(f"Unfinished repair journal: {previous}")
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    backup_path = root / f"factory_po_number_repair_backup_{token}.json"
    work = root / f"factory_po_number_repair_files_{token}"
    work.mkdir()
    journal = {
        "status": "preparing", "database_backup": str(dump), "database_backup_sha256": file_digest(dump),
        "plan": plan, "file_backups": [], "published": [],
    }
    def save_journal():
        temp = backup_path.with_suffix(".tmp")
        temp.write_text(json.dumps(journal, ensure_ascii=False, indent=2, default=str))
        os.replace(temp, backup_path)
    save_journal()
    staged = []
    original_paths = {f["old"] for doc in plan["documents"] for f in doc["files"]}
    committed = False
    try:
        for doc in plan["documents"]:
            directory = work / str(doc["id"])
            directory.mkdir()
            for f in doc["files"]:
                old_backup = directory / ("original." + f["kind"])
                shutil.copy2(f["old"], old_backup)
                if file_digest(old_backup) != f["sha256"]:
                    raise RepairError("Historical file changed during backup")
                journal["file_backups"].append({"original": f["old"], "backup": str(old_backup), "sha256": f["sha256"]})
            html = directory / "new.html"
            pdf = directory / "new.pdf"
            content = render_po_html(
                po_data=doc["source_data"]["po_data"],
                template_path=Path(settings.BASE_DIR) / "templates/factory_purchase_order.html",
            )
            old_html = Path(next(f["old"] for f in doc["files"] if f["kind"] == "html")).read_text()
            if visible_html(old_html, doc["old"]) != visible_html(content, doc["target"]):
                raise RepairError(f"Document {doc['id']}: rendered non-number content changed")
            write_html_and_pdf(content, html, pdf, Path(settings.BASE_DIR))
            new_text = verify_pdf(pdf, doc["target"])
            old_pdf = next(f["old"] for f in doc["files"] if f["kind"] == "pdf")
            old_text = verify_pdf(old_pdf, doc["old"])
            if (
                " ".join(old_text.replace(doc["old"], "__PO_NUMBER__").split())
                != " ".join(new_text.replace(doc["target"], "__PO_NUMBER__").split())
            ):
                raise RepairError(f"Document {doc['id']}: PDF non-number text changed")
            for f in doc["files"]:
                source = directory / ("new." + f["kind"])
                if f["kind"] == "json":
                    source.write_text(json.dumps(doc["source_data"], ensure_ascii=False, indent=2))
                staged.append({"source": str(source), "target": f["target"]})
            save_journal()
        journal["status"] = "prepared"
        save_journal()
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    tables = ", ".join(connection.ops.quote_name(model._meta.db_table) for model in MODELS)
                    cursor.execute(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE")
            current = build_plan()
            if current["fingerprint"] != expected_fingerprint:
                raise RepairError("Database/files changed during staging; abort")
            journal["status"] = "applying"
            save_journal()
            # Retain originals in durable backup; never expose temp IDs in files.
            for f in staged:
                target = media_path(f["target"])
                journal["published"].append(str(target))
                save_journal()
                shutil.copy2(f["source"], target)
            for doc in plan["documents"]:
                GeneratedDocument.objects.filter(pk=doc["id"]).update(document_number=f"{PREFIX}{doc['id']}_{token}")
            for row in plan["sequences"]:
                DocumentSequence.objects.filter(pk=row["document_sequence_id"]).update(po_number=row["target"])
            expected = expected_state(plan)
            for doc in plan["documents"]:
                row = next(row for row in expected["documents.GeneratedDocument"] if row["id"] == doc["id"])
                GeneratedDocument.objects.filter(pk=doc["id"]).update(**{
                    key: row[key] for key in ("document_number", "source_data", "pdf_file", "html_file")
                })
            models = {model._meta.label: model for model in MODELS}
            for change in plan["auxiliary_updates"]:
                models[change["model"]].objects.filter(pk=change["id"]).update(**change["updates"])
            verify_result(plan)
            targets = {f["target"] for f in staged}
            for old in original_paths - targets:
                media_path(old).unlink()
            if any(Path(old).exists() for old in original_paths - targets):
                raise RepairError("Old repair file remains")
        committed = True
        journal["status"] = "complete"
        save_journal()
    except Exception:
        if committed:
            raise RepairError(f"Repair committed but journal finalization failed; verify {backup_path}")
        # Restore filesystem when the transaction failed; keep backup metadata.
        try:
            if journal["published"]:
                for row in journal["file_backups"]:
                    shutil.copy2(row["backup"], media_path(row["original"]))
            for target in journal["published"]:
                if target not in original_paths:
                    media_path(target).unlink(missing_ok=True)
            journal["status"] = "rolled_back"
            save_journal()
        except Exception as rollback_error:
            journal["status"] = "rollback_failed"
            journal["rollback_error"] = str(rollback_error)
            save_journal()
        raise
    return {"applied": len(plan["documents"]), "sequence_count": len(plan["sequences"]),
            "backup_json": str(backup_path), "backup_files": str(work),
            "sequence_ids": [row["document_sequence_id"] for row in plan["sequences"]],
            "document_ids": [row["id"] for row in plan["documents"]],
            "post_audit_inconsistent_count": len(list(audit_document_sequences()))}
