from django.core.management.base import BaseCommand, CommandError

from workflow.services.backorder_history_repair_service import (
    repair_backorder_history_batches,
)


class Command(BaseCommand):
    help = (
        "Audit or repair historical backorder WorkflowItem links "
        "and FactoryConfirmation batch dates."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all-backorders",
            action="store_true",
            required=True,
            help="Audit/repair all existing replenishment batches.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist safe repairs. Default is dry-run.",
        )

    def handle(self, *args, **options):
        result = repair_backorder_history_batches(
            apply=options["apply"]
        )
        audits = result["audits"]

        for audit in audits:
            self.stdout.write(
                f"ShipmentBatch {audit['batch_id']} "
                f"Order {audit['order_id']} "
                f"({audit['bon_de_commande']}) "
                f"WorkflowItem={audit['workflow_item_id'] or '-'} "
                f"batch_date={audit['batch_date']} "
                f"shipping_date={audit['shipping_date'] or '-'}"
            )
            if audit["actions"]:
                self.stdout.write(
                    "  actions: " + ", ".join(audit["actions"])
                )
            for error in audit["errors"]:
                self.stdout.write(
                    self.style.ERROR(f"  blocker: {error}")
                )

        if result["blockers"]:
            self.stdout.write(
                self.style.ERROR(
                    "存在无法安全修复的历史批次，未修改任何数据。"
                )
            )
            raise CommandError(
                "历史 blocker 未全部满足安全修复条件。"
            )

        if not options["apply"]:
            self.stdout.write("REPAIR DRY-RUN: 未修改数据库。")
            self.stdout.write(
                f"可安全修复批次数: {len(audits)}"
            )
            return

        workflow_created = sum(
            1
            for item in result["results"]
            if item["workflow_created"]
        )
        date_repaired = sum(
            1
            for item in result["results"]
            if item["date_repaired"]
        )
        self.stdout.write(
            self.style.SUCCESS(
                "历史 blocker 修复完成："
                f"WorkflowItem 新建 {workflow_created} 个，"
                f"batch_date 修复 {date_repaired} 个。"
            )
        )
