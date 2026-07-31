from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management.base import BaseCommand, CommandError

from workflow.services.historical_invoice_numbering_apply_service import (
    apply_historical_invoice_numbering,
    backup_historical_invoice_numbering,
    timestamped_backup_root,
)
from workflow.services.historical_invoice_numbering_prerender_service import (
    build_historical_invoice_numbering_plan,
    prerender_historical_invoice,
    production_file_fingerprints,
)


APPLY_CONFIRMATION_REQUIRED_MESSAGE = (
    "Historical Invoice apply requires --confirm-historical-invoice-renumbering."
)


class Command(BaseCommand):
    help = (
        "Historical Hospital Invoice numbering plan, isolated temporary prerender "
        "validation, and explicit production apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--validate-render",
            action="store_true",
            help="Render affected Hospital Invoices only inside a temporary directory.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Execute the historical Hospital Invoice renumbering apply.",
        )
        parser.add_argument(
            "--confirm-historical-invoice-renumbering",
            action="store_true",
            help="Required confirmation flag for production apply.",
        )
        parser.add_argument(
            "--backup-root",
            default="",
            help="Optional explicit backup root directory.",
        )

    def handle(self, *args, **options):
        plan = build_historical_invoice_numbering_plan()
        self._write_plan(plan)

        if options["validate_render"] and not options["apply"]:
            self._run_validate_render(plan)
            return

        if not options["apply"]:
            self.stdout.write(
                "DRY-RUN: no database records or production files were changed."
            )
            return

        if not options["confirm_historical_invoice_renumbering"]:
            raise CommandError(APPLY_CONFIRMATION_REQUIRED_MESSAGE)

        backup_root = None
        try:
            requested_root = (
                Path(options["backup_root"]).expanduser()
                if options["backup_root"]
                else None
            )
            backup_root = timestamped_backup_root(requested_root)
            backup = backup_historical_invoice_numbering(
                plan=plan,
                backup_root=backup_root,
            )
            self.stdout.write(
                "BACKUP "
                f"root={backup['backup_root']} "
                f"database={backup['database_backup_path']} "
                f"files={backup['files_backup_path']} "
                f"mapping={backup['mapping_path']}"
            )
            apply_result = apply_historical_invoice_numbering(plan=plan)
            self.stdout.write(
                "APPLY "
                f"document_sequences={apply_result['document_sequence_update_count']} "
                f"generated_documents={apply_result['generated_document_update_count']} "
                f"regenerated_invoices={apply_result['regenerated_invoice_count']}"
            )
            post_plan = build_historical_invoice_numbering_plan()
            post_summary = post_plan["summary"]
            temp_sequence_count = self._count_temp_sequences()
            temp_document_count = self._count_temp_documents()
            self.stdout.write(
                "POST-APPLY "
                f"orders={post_summary['order_count']} "
                f"correct={post_summary['correct_order_count']} "
                f"incorrect={post_summary['incorrect_order_count']} "
                f"document_sequences_to_update={post_summary['document_sequence_update_count']} "
                f"affected_hospital_invoices={post_summary['affected_hospital_invoice_count']} "
                f"temp_sequences={temp_sequence_count} "
                f"temp_documents={temp_document_count}"
            )
        except Exception as exc:
            backup_text = str(backup_root) if backup_root else ""
            raise CommandError(
                f"Historical Invoice apply failed: {type(exc).__name__}: {exc}. "
                f"backup_root={backup_text}"
            )

    def _run_validate_render(self, plan):
        failures = []
        results = []
        affected = plan["affected_invoice_items"]
        production_before = production_file_fingerprints(affected)
        temporary_path = None

        with TemporaryDirectory(
            prefix="historical_invoice_numbering_"
        ) as temporary_name:
            temporary_path = Path(temporary_name).resolve()
            for item in affected:
                try:
                    result = prerender_historical_invoice(
                        plan_item=item,
                        temporary_root=temporary_path,
                    )
                    results.append(result)
                    self.stdout.write(
                        "VALIDATE "
                        f"document_id={item['generated_document_id']} "
                        f"expected={item['expected_document_number']} "
                        f"success={result['success']} "
                        f"html={result['validations']['html_number_valid']} "
                        f"pdf={result['validations']['pdf_structure_valid']} "
                        f"snapshot={result['validations']['business_snapshot_matches']}"
                    )
                    if not result["success"]:
                        failures.append(
                            {
                                "document_id": item["generated_document_id"],
                                "error": "; ".join(result["warnings"])
                                or "validation failed",
                            }
                        )
                except Exception as exc:
                    failures.append(
                        {
                            "document_id": item["generated_document_id"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    self.stderr.write(
                        "VALIDATE-FAILED "
                        f"document_id={item['generated_document_id']} "
                        f"error={type(exc).__name__}: {exc}"
                    )

        temporary_cleaned = bool(
            temporary_path and not temporary_path.exists()
        )
        production_after = production_file_fingerprints(affected)
        unchanged_count = sum(
            production_before[item["generated_document_id"]]
            == production_after[item["generated_document_id"]]
            for item in affected
        )
        html_valid_count = sum(
            result["validations"]["html_number_valid"]
            for result in results
        )
        pdf_valid_count = sum(
            result["validations"]["pdf_structure_valid"]
            for result in results
        )
        snapshot_match_count = sum(
            result["validations"]["business_snapshot_matches"]
            for result in results
        )
        success_count = sum(result["success"] for result in results)
        self.stdout.write(
            "VALIDATE-SUMMARY "
            f"planned_invoices={len(affected)} "
            f"success={success_count} "
            f"failed={len(failures)} "
            f"html_number_valid={html_valid_count} "
            f"pdf_structure_valid={pdf_valid_count} "
            f"business_snapshot_matches={snapshot_match_count} "
            f"production_files_unchanged={unchanged_count} "
            f"temporary_directory_cleaned={temporary_cleaned} "
            f"blockers={plan['summary']['blocker_count']}"
        )
        if failures:
            detail = " | ".join(
                f"document_id={row['document_id']}: {row['error']}"
                for row in failures
            )
            raise CommandError(
                f"Historical Invoice prerender validation failed: {detail}"
            )

    def _count_temp_sequences(self):
        from documents.models import DocumentSequence

        return DocumentSequence.objects.filter(
            invoice_number__startswith="TEMP-INVOICE-ORDER-"
        ).count()

    def _count_temp_documents(self):
        from documents.models import GeneratedDocument

        return GeneratedDocument.objects.filter(
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            document_number__startswith="TEMP-INVOICE-DOC-",
        ).count()

    def _write_plan(self, plan):
        summary = plan["summary"]
        self.stdout.write(
            "SUMMARY "
            f"orders={summary['order_count']} "
            f"correct={summary['correct_order_count']} "
            f"incorrect={summary['incorrect_order_count']} "
            f"document_sequences_to_update={summary['document_sequence_update_count']} "
            f"hospital_invoices={summary['hospital_invoice_count']} "
            f"affected_hospital_invoices={summary['affected_hospital_invoice_count']} "
            f"expected_full_numbers_unique={summary['expected_document_numbers_unique']} "
            f"blockers={summary['blocker_count']} "
            f"validate_render_allowed={summary['validate_render_allowed']}"
        )
        for item in plan["invoice_items"]:
            self.stdout.write(
                "INVOICE "
                f"order_id={item['order_id']} "
                f"bon={item['bon_de_commande']} "
                f"current_base={item['current_base_number']!r} "
                f"expected_base={item['expected_base_number']!r} "
                f"document_id={item['generated_document_id']} "
                f"batch_id={item['shipment_batch_id']} "
                f"batch_number={item['batch_number']} "
                f"current={item['current_document_number']!r} "
                f"expected={item['expected_document_number']!r} "
                f"pdf={item['current_pdf_path']!r} "
                f"needs_update={item['needs_update']} "
                f"blockers={item['blockers']}"
            )
