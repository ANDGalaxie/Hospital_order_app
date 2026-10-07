import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.models import GeneratedDocument
from shipments.models import ShipmentBatch
from commercial_pos.services.generation_service import build_commercial_snapshot, ensure_commercial_po_for_batch
from commercial_pos.services.price_service import CommercialPOError


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Preview or generate missing commercial POs. Explicit scoped regeneration is optional. Defaults to dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--bon", action="append")
        parser.add_argument("--batch-id", action="append", type=int)
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--actor-id", type=int, help="Required for apply; actual internal executing user ID.")
        parser.add_argument("--regenerate-existing", action="store_true", help="Re-render existing commercial POs from the same batch's frozen Factory PO with commercial prices; requires --bon or --batch-id, never --all.")

    def handle(self, *args, **options):
        if options["regenerate_existing"] and (options["all"] or not (options["bon"] or options["batch_id"])):
            raise CommandError("--regenerate-existing requires --bon or --batch-id and cannot be combined with --all.")
        if not (options["all"] or options["bon"] or options["batch_id"]):
            raise CommandError("Specify --bon, --batch-id or explicitly --all.")
        if options["all"] and (options["bon"] or options["batch_id"]):
            raise CommandError("--all cannot be combined with a scoped selector.")
        actor = None
        if options["apply"]:
            if not options["actor_id"]:
                raise CommandError("--apply requires --actor-id identifying the actual executing user.")
            actor = get_user_model().objects.filter(pk=options["actor_id"], is_active=True, is_staff=True).first()
            if not actor or actor.groups.filter(name="Hospital Demo").exists():
                raise CommandError("Actor must be an active internal staff user, outside Hospital Demo.")
        batches = ShipmentBatch.objects.select_related("order__hospital", "factory_confirmation", "inventory_allocation").order_by("id")
        if options["bon"]:
            batches = batches.filter(order__bon_de_commande__in=options["bon"])
        if options["batch_id"]:
            batches = batches.filter(pk__in=options["batch_id"])
        mode = "APPLY" if options["apply"] else "DRY-RUN"
        untouched = "Invoice/Factory PO and sequences are untouched" if options["regenerate_existing"] else "old documents and sequences are untouched"
        self.stdout.write(f"{mode} actor_id={actor.pk if actor else 'none'}; {untouched}")
        counts = {"selected": 0, "existing": 0, "eligible": 0, "generated": 0, "invalid": 0}
        if options["regenerate_existing"]:
            counts["regenerated"] = 0
        for batch in batches.iterator():
            counts["selected"] += 1
            label = f"batch_id={batch.pk} order_id={batch.order_id} BON={batch.order.bon_de_commande} B{batch.batch_number}"
            existing = GeneratedDocument.objects.filter(shipment_batch=batch, document_type="commercial_po").first()
            if existing:
                counts["existing"] += 1
                if not options["regenerate_existing"]:
                    self.stdout.write(f"SKIP {label}: commercial PO already exists")
                    continue
            try:
                snapshot = build_commercial_snapshot(batch, document_number=existing.document_number if existing else None)
                counts["eligible"] += 1
                if options["apply"]:
                    doc = ensure_commercial_po_for_batch(batch.pk, actor, regenerate_existing=options["regenerate_existing"])
                    counts["regenerated" if existing else "generated"] += 1
                    logger.info("Commercial backfill actor_id=%s batch_id=%s document_id=%s", actor.pk, batch.pk, doc.pk)
                if existing:
                    action = "REGENERATED" if options["apply"] else "WOULD REGENERATE"
                    self.stdout.write(f"{action} {label} Batch={batch.batch_number} document={existing.document_number} document_id={existing.pk} shipping={snapshot['shipping_date']} units={snapshot['total_units']} EUR={snapshot['total_amount']}")
                    continue
                self.stdout.write(f"{mode} {label}: {snapshot['document_number']} shipping={snapshot['shipping_date']} units={snapshot['total_units']} EUR={snapshot['total_amount']}")
            except Exception as exc:
                counts["invalid"] += 1
                logger.exception("Commercial backfill failed actor_id=%s batch_id=%s order_id=%s", actor.pk if actor else None, batch.pk, batch.order_id)
                message = str(exc) if isinstance(exc, (CommercialPOError, ValueError)) else type(exc).__name__
                self.stdout.write(f"INVALID {label}: {message}")
        self.stdout.write(" ".join(f"{key}={value}" for key, value in counts.items()))
        if options["apply"] and counts["invalid"]:
            raise CommandError("Some commercial documents could not be generated; see internal logs.")
