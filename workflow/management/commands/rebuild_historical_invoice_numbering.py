from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management.base import BaseCommand, CommandError

from workflow.services.historical_invoice_numbering_prerender_service import (
    build_historical_invoice_numbering_plan,
    prerender_historical_invoice,
    production_file_fingerprints,
)


APPLY_DISABLED_MESSAGE = (
    "Production apply is disabled because the atomic file replacement "
    "phase has not yet been implemented and validated."
)


class Command(BaseCommand):
    help = (
        "Read-only historical Hospital Invoice numbering plan and isolated "
        "temporary prerender validation."
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
            help="Disabled: production apply is not implemented.",
        )
        parser.add_argument(
            "--confirm-all-documents-are-unissued",
            action="store_true",
            help="Accepted for safety compatibility; it cannot enable --apply.",
        )

    def handle(self, *args, **options):
        if options["apply"]:
            raise CommandError(APPLY_DISABLED_MESSAGE)

        plan = build_historical_invoice_numbering_plan()
        self._write_plan(plan)

        if not options["validate_render"]:
            self.stdout.write(
                "DRY-RUN: no database records or production files were changed."
            )
            return

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
            f"blockers={plan['summary']['blocker_count']} "
            "production_apply=BLOCKED"
        )
        if failures:
            detail = " | ".join(
                f"document_id={row['document_id']}: {row['error']}"
                for row in failures
            )
            raise CommandError(
                f"Historical Invoice prerender validation failed: {detail}"
            )

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
            f"validate_render_allowed={summary['validate_render_allowed']} "
            "production_apply=BLOCKED"
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
