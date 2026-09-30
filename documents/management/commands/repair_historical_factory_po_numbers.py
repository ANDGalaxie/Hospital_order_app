import json

from django.core.management.base import BaseCommand, CommandError
from documents.services.historical_factory_po_repair_service import (
    RepairError, apply_plan, build_plan, plan_summary,
)


class Command(BaseCommand):
    help = "Repair historical Factory PO numbers from frozen snapshots. Default: read-only dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-fingerprint", help="Exact fingerprint from the reviewed dry-run")
        parser.add_argument("--database-backup", help="Existing PostgreSQL custom-format dump")
        parser.add_argument("--confirm-database-backup", action="store_true",
                            help="Confirm the dump is current, verified and available for rollback")

    def handle(self, *args, **options):
        try:
            plan = build_plan()
            if not options["apply"]:
                result = {"mode": "dry-run", **plan_summary(plan)}
            else:
                if not options["confirm_database_backup"] or not options["database_backup"] or not options["expected_fingerprint"]:
                    raise RepairError("--apply requires --database-backup, --confirm-database-backup and --expected-fingerprint")
                result = {"mode": "apply", **apply_plan(
                    plan, options["database_backup"], options["expected_fingerprint"],
                )}
        except (RepairError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
