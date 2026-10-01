import json
from pathlib import PurePosixPath

from django.core.management.base import BaseCommand, CommandError

from backorders.models import InventoryBatch
from documents.models import GeneratedDocument
from factory_confirmations.models import FactoryConfirmation
from orders.models import Order


FILE_FIELDS = (
    (Order, "hospital_order_pdf", True),
    (FactoryConfirmation, "confirmation_pdf", True),
    (InventoryBatch, "source_pdf", False),
    (GeneratedDocument, "pdf_file", False),
    (GeneratedDocument, "html_file", False),
)


class Command(BaseCommand):
    help = "Read-only verification that business FileField references are safe and readable."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--fail-on-error", action="store_true")

    def handle(self, *args, **options):
        result = {
            "records_checked": 0,
            "references_checked": 0,
            "readable": 0,
            "missing": [],
            "inaccessible": [],
            "unexpected": [],
            "blank_optional": 0,
        }

        for model, field_name, required in FILE_FIELDS:
            label = model._meta.label
            for instance in model._default_manager.order_by("pk").iterator():
                result["records_checked"] += 1
                field_file = getattr(instance, field_name)
                name = str(getattr(field_file, "name", "") or "")
                reference = f"{label}:{instance.pk}:{field_name}"

                if not name:
                    if required:
                        result["unexpected"].append(
                            {"reference": reference, "reason": "required reference is blank"}
                        )
                    else:
                        result["blank_optional"] += 1
                    continue

                result["references_checked"] += 1
                normalized = name.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts:
                    result["unexpected"].append(
                        {"reference": reference, "name": name, "reason": "unsafe path"}
                    )
                    continue

                storage = field_file.storage
                try:
                    if not storage.exists(name):
                        result["missing"].append(
                            {"reference": reference, "name": name}
                        )
                        continue
                    with storage.open(name, "rb") as handle:
                        handle.read(1)
                    result["readable"] += 1
                except Exception as exc:
                    result["inaccessible"].append(
                        {
                            "reference": reference,
                            "name": name,
                            "error_type": type(exc).__name__,
                        }
                    )

        result["problem_count"] = sum(
            len(result[key]) for key in ("missing", "inaccessible", "unexpected")
        )

        if options["as_json"]:
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                "Media integrity: "
                f"records={result['records_checked']} "
                f"references={result['references_checked']} "
                f"readable={result['readable']} "
                f"blank_optional={result['blank_optional']} "
                f"problems={result['problem_count']}"
            )
            for category in ("missing", "inaccessible", "unexpected"):
                self.stdout.write(f"{category.upper()}={len(result[category])}")
                for item in result[category]:
                    self.stdout.write(f"  {json.dumps(item, ensure_ascii=False)}")

        if options["fail_on_error"] and result["problem_count"]:
            raise CommandError("Media integrity problems detected; no data was changed.")
