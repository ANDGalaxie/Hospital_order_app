from __future__ import annotations

import json
import os
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max

from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_generation_service import (
    get_order_document_workspace,
    json_safe,
    load_json_config,
    media_relative_path,
    render_invoice_html,
    sanitize_filename,
    save_json_file,
    write_invoice_html_and_pdf,
)
from workflow.services.historical_invoice_numbering_prerender_service import (
    _pricing_basis,
    _require_child_path,
    build_historical_invoice_numbering_plan,
    build_invoice_business_snapshot,
    file_fingerprint,
)
from workflow.services.workflow_document_generation_service import (
    build_batch_hospital_invoice_data,
)


def timestamped_backup_root(base_dir: Optional[Path] = None) -> Path:
    root = Path(base_dir or "/home/sidake/hospital_order_backups").resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = root / f"invoice_numbering_{timestamp}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _current_data_path(plan_item: Dict[str, Any]) -> Optional[Path]:
    html_path = plan_item.get("current_html_resolved_path")
    if html_path:
        return Path(html_path).with_name(
            f"{Path(html_path).stem}_data.json"
        )
    pdf_path = plan_item.get("current_pdf_resolved_path")
    if pdf_path:
        return Path(pdf_path).with_name(
            f"{Path(pdf_path).stem}_data.json"
        )
    return None


def _target_invoice_paths(order, batch, document_number: str) -> Dict[str, Path]:
    workspace = (
        get_order_document_workspace(order)
        / "workflow_batches"
        / f"batch_{batch.id}"
        / "invoices"
    )
    base_name = sanitize_filename(document_number)
    return {
        "html": workspace / f"{base_name}.html",
        "pdf": workspace / f"{base_name}.pdf",
        "data": workspace / f"{base_name}_data.json",
    }


def _deep_json_copy(value: Any) -> Any:
    return deepcopy(value) if value is not None else {}


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return json_safe(value)


def _build_updated_numbers(
    *,
    existing_numbers: Dict[str, Any],
    plan_item: Dict[str, Any],
    sequence: DocumentSequence,
) -> Dict[str, Any]:
    numbers = _deep_json_copy(existing_numbers or {})
    numbers["invoice_number"] = plan_item["expected_document_number"]
    numbers["base_invoice_number"] = plan_item["expected_base_number"]
    numbers["batch_number"] = plan_item["batch_number"]
    numbers["batch_numbering_warnings"] = []
    numbers["sequence"] = plan_item["expected_sequence"]
    numbers["sequence_id"] = sequence.id
    numbers["month_key"] = sequence.month_key
    numbers["bon_de_commande"] = sequence.bon_de_commande
    if plan_item.get("order_date"):
        numbers["document_date"] = plan_item["order_date"].isoformat()
    if "created" not in numbers:
        numbers["created"] = False
    return numbers


def _build_regenerated_source_data(
    *,
    plan_item: Dict[str, Any],
    document: GeneratedDocument,
    sequence: DocumentSequence,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    batch = document.shipment_batch
    order = document.order
    existing_source = _deep_json_copy(document.source_data or {})
    numbers = _build_updated_numbers(
        existing_numbers=existing_source.get("numbers") or {},
        plan_item=plan_item,
        sequence=sequence,
    )
    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )
    invoice_data = build_batch_hospital_invoice_data(
        batch=batch,
        company_info=company_info,
        numbers=numbers,
    )
    source_data = _deep_json_copy(existing_source)
    source_data["workflow_item_id"] = (
        existing_source.get("workflow_item_id")
        or plan_item["workflow_item_id"]
    )
    source_data["shipment_batch_id"] = batch.id
    source_data["numbers"] = numbers
    source_data["pricing_basis"] = _deep_json_copy(
        existing_source.get("pricing_basis")
        or _pricing_basis(
            existing_source,
            invoice_data=invoice_data,
            order_date=order.order_date,
        )
    )
    source_data["invoice_data"] = invoice_data
    return json_safe(source_data), invoice_data


def _validate_rendered_invoice(
    *,
    plan_item: Dict[str, Any],
    source_data: Dict[str, Any],
    html_path: Path,
    pdf_path: Path,
    data_path: Path,
    root: Path,
) -> Dict[str, Any]:
    html_exists = html_path.is_file() and html_path.stat().st_size > 0
    html_number_valid = (
        html_exists
        and plan_item["expected_document_number"]
        in html_path.read_text(encoding="utf-8")
    )
    pdf_exists = pdf_path.is_file() and pdf_path.stat().st_size > 0
    pdf_structure_valid = False
    pdf_page_count = 0
    pdf_error = ""
    if pdf_exists:
        try:
            import pdfplumber

            with pdfplumber.open(str(pdf_path)) as pdf:
                pdf_page_count = len(pdf.pages)
            pdf_structure_valid = pdf_page_count > 0
        except Exception as exc:
            pdf_error = f"{type(exc).__name__}: {exc}"

    snapshot = build_invoice_business_snapshot(
        source_data,
        order_date=plan_item["order_date"],
    )
    snapshot_matches = snapshot == plan_item["business_snapshot"]
    validations = {
        "html_exists_nonempty": html_exists,
        "html_number_valid": html_number_valid,
        "pdf_exists_nonempty": pdf_exists,
        "pdf_structure_valid": pdf_structure_valid,
        "pdf_page_count": pdf_page_count,
        "temporary_paths_valid": all(
            _require_child_path(root, path)
            for path in (html_path, pdf_path, data_path)
        ),
        "business_snapshot_matches": snapshot_matches,
    }
    warnings = []
    if pdf_error:
        warnings.append(pdf_error)
    if not snapshot_matches:
        warnings.append("Business snapshot differs from the existing Invoice.")
    return {
        "business_snapshot": snapshot,
        "validations": validations,
        "warnings": warnings,
        "success": all(
            value
            for key, value in validations.items()
            if key != "pdf_page_count"
        ),
    }


def stage_historical_invoice_regenerations(
    plan_items: Iterable[Dict[str, Any]],
    temporary_root: Path,
) -> List[Dict[str, Any]]:
    staged_results: List[Dict[str, Any]] = []
    temporary_root = Path(temporary_root).resolve()
    temporary_root.mkdir(parents=True, exist_ok=True)

    for plan_item in plan_items:
        if plan_item.get("blockers"):
            raise ValueError(
                "Historical Invoice apply is blocked: "
                + ", ".join(plan_item["blockers"])
            )
        document = (
            GeneratedDocument.objects.select_related(
                "order",
                "shipment_batch__order",
            )
            .get(id=plan_item["generated_document_id"])
        )
        if document.document_type != GeneratedDocument.DocumentType.HOSPITAL_INVOICE:
            raise ValueError(
                f"Document {document.id} is not a Hospital Invoice."
            )
        if not document.shipment_batch_id:
            raise ValueError(f"Document {document.id} has no ShipmentBatch.")
        month_key = plan_item["order_date"].strftime("%Y-%m")
        sequence = DocumentSequence.objects.get(
            month_key=month_key,
            bon_de_commande=plan_item["bon_de_commande"],
        )
        source_data, invoice_data = _build_regenerated_source_data(
            plan_item=plan_item,
            document=document,
            sequence=sequence,
        )
        target_paths = _target_invoice_paths(
            document.order,
            document.shipment_batch,
            plan_item["expected_document_number"],
        )
        output_dir = _require_child_path(
            temporary_root,
            temporary_root / f"document_{document.id}",
        )
        output_dir.mkdir(parents=True, exist_ok=False)
        staging_paths = {
            "html": _require_child_path(
                temporary_root,
                output_dir / target_paths["html"].name,
            ),
            "pdf": _require_child_path(
                temporary_root,
                output_dir / target_paths["pdf"].name,
            ),
            "data": _require_child_path(
                temporary_root,
                output_dir / target_paths["data"].name,
            ),
        }
        html_content = render_invoice_html(
            invoice_data=invoice_data,
            template_dir=Path(settings.BASE_DIR) / "templates",
            template_name="hospital_invoice.html",
        )
        write_invoice_html_and_pdf(
            html_content=html_content,
            html_path=staging_paths["html"],
            pdf_path=staging_paths["pdf"],
            project_root=Path(settings.BASE_DIR),
        )
        save_json_file(source_data, staging_paths["data"])
        validation = _validate_rendered_invoice(
            plan_item=plan_item,
            source_data=source_data,
            html_path=staging_paths["html"],
            pdf_path=staging_paths["pdf"],
            data_path=staging_paths["data"],
            root=temporary_root,
        )
        if not validation["success"]:
            raise ValueError(
                f"Document {document.id} staged regeneration failed: "
                + "; ".join(
                    validation["warnings"] or ["validation failed"]
                )
            )
        staged_results.append(
            {
                "order_id": plan_item["order_id"],
                "generated_document_id": document.id,
                "workflow_item_id": plan_item["workflow_item_id"],
                "shipment_batch_id": plan_item["shipment_batch_id"],
                "expected_sequence": plan_item["expected_sequence"],
                "expected_base_number": plan_item["expected_base_number"],
                "expected_document_number": plan_item["expected_document_number"],
                "sequence_id": sequence.id,
                "current_document_number": document.document_number,
                "current_pdf_path": plan_item.get("current_pdf_resolved_path"),
                "current_html_path": plan_item.get("current_html_resolved_path"),
                "current_data_path": _current_data_path(plan_item),
                "target_paths": target_paths,
                "staging_paths": staging_paths,
                "source_data": source_data,
                "business_snapshot": validation["business_snapshot"],
                "validations": validation["validations"],
                "warnings": validation["warnings"],
            }
        )
    return staged_results


def backup_historical_invoice_numbering(
    *,
    plan: Dict[str, Any],
    backup_root: Path,
) -> Dict[str, Any]:
    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    database_path = _backup_database(backup_root)
    files_dir = backup_root / "files"
    copied_files = _backup_invoice_files(plan["affected_invoice_items"], files_dir)
    mapping_path = backup_root / "numbering_mapping.json"
    mapping_path.write_text(
        json.dumps(_json_ready(plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    snapshot_dir = backup_root / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    _write_model_snapshots(plan, snapshot_dir)
    git_dir = backup_root / "git"
    git_dir.mkdir(parents=True, exist_ok=True)
    _write_git_snapshots(git_dir)
    return {
        "backup_root": str(backup_root),
        "database_backup_path": str(database_path),
        "files_backup_path": str(files_dir),
        "mapping_path": str(mapping_path),
        "copied_files": copied_files,
    }


def _backup_database(backup_root: Path) -> Path:
    settings_dict = connection.settings_dict
    db_name = settings_dict.get("NAME")
    if not db_name:
        raise ValueError("Database NAME is missing; backup is blocked.")
    output_path = backup_root / "database" / "historical_invoice_numbering.dump"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    password = settings_dict.get("PASSWORD")
    if password:
        env["PGPASSWORD"] = str(password)
    command = [
        "pg_dump",
        "-Fc",
        "-f",
        str(output_path),
        "-h",
        str(settings_dict.get("HOST") or ""),
        "-p",
        str(settings_dict.get("PORT") or "5432"),
        "-U",
        str(settings_dict.get("USER") or ""),
        str(db_name),
    ]
    executable = shutil.which("pg_dump")
    if executable:
        completed = subprocess.run(
            [executable, *command[1:]],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Database backup failed: "
                f"stdout={completed.stdout.strip()} stderr={completed.stderr.strip()}"
            )
    else:
        container_name = os.getenv("POSTGRES_DOCKER_CONTAINER", "acoeur_postgres")
        docker_command = [
            "docker",
            "exec",
            "-i",
            container_name,
            "pg_dump",
            "-U",
            str(settings_dict.get("USER") or ""),
            "-d",
            str(db_name),
            "-Fc",
        ]
        completed = subprocess.run(
            docker_command,
            check=False,
            capture_output=True,
            env=env,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "Database backup failed: "
                f"stderr={stderr}"
            )
        output_path.write_bytes(completed.stdout)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(
            f"Database backup is missing or empty: {output_path}"
        )
    metadata = {
        "engine": settings_dict.get("ENGINE"),
        "name": db_name,
        "user": settings_dict.get("USER"),
        "host": settings_dict.get("HOST"),
        "port": settings_dict.get("PORT"),
        "size": output_path.stat().st_size,
    }
    (backup_root / "database" / "connection.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def _copy_if_exists(source: Optional[Path], destination: Path) -> Optional[Dict[str, Any]]:
    if source is None or not source.exists() or not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return file_fingerprint(destination)


def _backup_invoice_files(
    plan_items: Iterable[Dict[str, Any]],
    files_dir: Path,
) -> List[Dict[str, Any]]:
    copied: List[Dict[str, Any]] = []
    media_root = Path(settings.MEDIA_ROOT).resolve()
    for item in plan_items:
        document_dir = files_dir / f"document_{item['generated_document_id']}"
        current_paths = {
            "pdf": item.get("current_pdf_resolved_path"),
            "html": item.get("current_html_resolved_path"),
            "data": _current_data_path(item),
        }
        for kind, source in current_paths.items():
            if source is None:
                copied.append(
                    {
                        "document_id": item["generated_document_id"],
                        "kind": kind,
                        "source": "",
                        "backup": "",
                        "exists": False,
                    }
                )
                continue
            source = Path(source)
            relative = (
                source.relative_to(media_root)
                if source.exists()
                else Path(source.name)
            )
            destination = document_dir / relative
            fingerprint = _copy_if_exists(source, destination)
            copied.append(
                {
                    "document_id": item["generated_document_id"],
                    "kind": kind,
                    "source": str(source),
                    "backup": str(destination),
                    "exists": bool(fingerprint),
                    "fingerprint": fingerprint or {},
                }
            )
    return copied


def _write_model_snapshots(plan: Dict[str, Any], snapshot_dir: Path) -> None:
    order_ids = sorted({item["order_id"] for item in plan["invoice_items"]})
    document_ids = sorted(
        item["generated_document_id"]
        for item in plan["affected_invoice_items"]
    )
    batch_ids = sorted(
        item["shipment_batch_id"]
        for item in plan["affected_invoice_items"]
    )
    sequence_rows = list(
        DocumentSequence.objects.filter(
            bon_de_commande__in=[item["bon_de_commande"] for item in plan["orders"]]
        )
        .order_by("id")
        .values()
    )
    document_rows = list(
        GeneratedDocument.objects.filter(id__in=document_ids)
        .order_by("id")
        .values()
    )
    workflow_rows = list(
        GeneratedDocument.objects.filter(id__in=document_ids)
        .order_by("id")
        .values("id", "order_id", "shipment_batch_id", "document_number")
    )
    (snapshot_dir / "document_sequences.json").write_text(
        json.dumps(json_safe(sequence_rows), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (snapshot_dir / "generated_documents.json").write_text(
        json.dumps(json_safe(document_rows), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (snapshot_dir / "workflow_linkage.json").write_text(
        json.dumps(
            json_safe(
                {
                    "order_ids": order_ids,
                    "batch_ids": batch_ids,
                    "documents": workflow_rows,
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_git_snapshots(git_dir: Path) -> None:
    cwd = str(Path(settings.BASE_DIR))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    (git_dir / "head.txt").write_text(head.stdout, encoding="utf-8")
    (git_dir / "status.txt").write_text(status.stdout, encoding="utf-8")


def apply_historical_invoice_numbering(
    *,
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    plan = plan or build_historical_invoice_numbering_plan()
    summary = plan["summary"]
    if summary["blocker_count"]:
        raise ValueError(
            "Historical Invoice apply is blocked by current plan blockers."
        )
    if not summary["expected_document_numbers_unique"]:
        raise ValueError(
            "Historical Invoice apply requires unique expected document numbers."
        )

    with TemporaryDirectory(prefix="historical_invoice_apply_") as temp_name:
        temporary_root = Path(temp_name).resolve()
        staged = stage_historical_invoice_regenerations(
            plan["affected_invoice_items"],
            temporary_root,
        )
        file_phase = _install_staged_files(staged, temporary_root)
        try:
            db_phase = _apply_database_updates(plan=plan, staged=staged)
        except Exception:
            _rollback_installed_files(file_phase)
            raise
        return {
            "staged_count": len(staged),
            "document_sequence_update_count": db_phase[
                "document_sequence_update_count"
            ],
            "generated_document_update_count": db_phase[
                "generated_document_update_count"
            ],
            "regenerated_invoice_count": len(staged),
            "temporary_root": str(temporary_root),
            "updated_document_ids": db_phase["updated_document_ids"],
            "updated_sequence_ids": db_phase["updated_sequence_ids"],
            "installed_files": file_phase["installed_files"],
        }


def _install_staged_files(
    staged: Iterable[Dict[str, Any]],
    temporary_root: Path,
) -> Dict[str, Any]:
    hold_root = temporary_root / "holds"
    installed_files: List[str] = []
    moved_holds: List[Tuple[Path, Path]] = []
    seen_current_paths = set()
    for row in staged:
        for current_path in (
            row.get("current_pdf_path"),
            row.get("current_html_path"),
            row.get("current_data_path"),
        ):
            if not current_path:
                continue
            current_path = Path(current_path)
            if current_path in seen_current_paths:
                continue
            seen_current_paths.add(current_path)
            if current_path.exists():
                hold_path = hold_root / str(len(moved_holds)) / current_path.name
                hold_path.parent.mkdir(parents=True, exist_ok=True)
                current_path.rename(hold_path)
                moved_holds.append((current_path, hold_path))
    try:
        for row in staged:
            for kind in ("html", "pdf", "data"):
                source = Path(row["staging_paths"][kind])
                target = Path(row["target_paths"][kind])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                installed_files.append(str(target))
    except Exception:
        _rollback_installed_files(
            {
                "moved_holds": moved_holds,
                "installed_files": installed_files,
            }
        )
        raise
    return {
        "moved_holds": moved_holds,
        "installed_files": installed_files,
    }


def _rollback_installed_files(file_phase: Dict[str, Any]) -> None:
    for path_text in reversed(file_phase.get("installed_files") or []):
        path = Path(path_text)
        if path.exists() and path.is_file():
            path.unlink()
    for original, hold in reversed(file_phase.get("moved_holds") or []):
        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists() and original.is_file():
            original.unlink()
        if hold.exists():
            hold.rename(original)


def _apply_database_updates(
    *,
    plan: Dict[str, Any],
    staged: List[Dict[str, Any]],
) -> Dict[str, Any]:
    order_items = [
        item
        for item in plan["orders"]
        if item["sequence_needs_update"]
        and (
            item.get("current_sequence") is not None
            or bool(item.get("current_base_number"))
        )
    ]
    with transaction.atomic():
        sequence_rows = []
        for item in order_items:
            month_key = item["order_date"].strftime("%Y-%m")
            sequence_rows.append(
                DocumentSequence.objects.select_for_update().get(
                    month_key=month_key,
                    bon_de_commande=item["bon_de_commande"],
                )
            )
        document_rows = {
            document.id: document
            for document in GeneratedDocument.objects.select_for_update().filter(
                id__in=[row["generated_document_id"] for row in staged]
            )
        }
        max_sequence = (
            DocumentSequence.objects.aggregate(max_value=Max("sequence"))["max_value"]
            or 0
        )
        for offset, sequence in enumerate(
            sorted(sequence_rows, key=lambda row: row.id),
            start=1,
        ):
            sequence.sequence = max_sequence + offset
            sequence.invoice_number = f"TEMP-INVOICE-ORDER-{sequence.id}"
            sequence.save(
                update_fields=["sequence", "invoice_number", "updated_at"]
            )
        for row in staged:
            document = document_rows[row["generated_document_id"]]
            document.document_number = f"TEMP-INVOICE-DOC-{document.id}"
            document.save(update_fields=["document_number"])
        for item in order_items:
            month_key = item["order_date"].strftime("%Y-%m")
            sequence = next(
                row
                for row in sequence_rows
                if row.month_key == month_key
                and row.bon_de_commande == item["bon_de_commande"]
            )
            sequence.sequence = item["expected_sequence"]
            sequence.invoice_number = item["expected_base_number"]
            sequence.save(
                update_fields=["sequence", "invoice_number", "updated_at"]
            )
        for row in staged:
            document = document_rows[row["generated_document_id"]]
            document.document_number = row["expected_document_number"]
            document.pdf_file = media_relative_path(
                Path(row["target_paths"]["pdf"])
            )
            document.html_file = media_relative_path(
                Path(row["target_paths"]["html"])
            )
            document.source_data = json_safe(row["source_data"])
            document.save(
                update_fields=[
                    "document_number",
                    "pdf_file",
                    "html_file",
                    "source_data",
                ]
            )
    return {
        "document_sequence_update_count": len(order_items),
        "generated_document_update_count": len(staged),
        "updated_document_ids": sorted(document_rows),
        "updated_sequence_ids": sorted(row.id for row in sequence_rows),
    }
