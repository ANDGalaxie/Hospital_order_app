from django.core.management.base import BaseCommand, CommandError

from workflow.services.order_150222_invoice_repair_service import (
    apply_order_150222_repair,
    build_order_150222_repair_plan,
)


class Command(BaseCommand):
    help = "Dry-run or apply the strictly scoped Order 150222 Invoice repair."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        plan = build_order_150222_repair_plan()
        if not plan.get("order"):
            raise CommandError("; ".join(plan["errors"]))

        self.stdout.write(f"Order ID: {plan['order_id']}")
        self.stdout.write(f"Order date: {plan['order_date']}")
        self.stdout.write(f"Original PDF: {plan['hospital_order_pdf']}")
        self.stdout.write("Original-order prices:")
        for row in plan["price_rows"]:
            self.stdout.write(
                f"  {row['product_code']}: qty={row['quantity']} "
                f"price {row['old_unit_price']} -> {row['new_unit_price']} "
                f"total={row['line_total']} ({row['source_page']})"
            )
        self.stdout.write(
            f"Invoice base number: {plan['current_base_invoice_number']} "
            f"-> {plan['target_base_invoice_number']}"
        )
        for batch in plan["batches"]:
            self.stdout.write(
                f"ShipmentBatch {batch['id']}: Invoice={batch['invoice_id'] or '-'} "
                f"{batch['current_invoice_number'] or '-'} -> "
                f"{batch['target_invoice_number']} sha256={batch['file']['sha256'] or '-'}"
            )
        for conflict in plan["conflicts"]:
            self.stdout.write(self.style.ERROR(
                "NUMBER CONFLICT: "
                f"GeneratedDocument {conflict['id']} Order {conflict['order_id']} "
                f"Invoice {conflict['document_number']}"
            ))
        for error in plan["errors"]:
            self.stdout.write(self.style.ERROR(f"BLOCKER: {error}"))

        if not options["apply"]:
            self.stdout.write("DRY-RUN: database/files were not modified.")
            return
        if not plan["can_apply"]:
            raise CommandError("存在 blocker，未执行 apply。")
        raise CommandError(paused)
        self.stdout.write(self.style.SUCCESS(
            f"APPLY completed: regenerated {len(results)} Invoice document(s)."
        ))
