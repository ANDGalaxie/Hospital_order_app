import json

from django.core.management.base import (
    BaseCommand,
)
from django.db import transaction

from documents.models import GeneratedDocument
from factory_confirmations.models import (
    FactoryConfirmation,
)
from orders.models import Order
from orders.services.order_date_service import (
    parse_business_date,
    resolve_extracted_order_date,
)
from pricing.services.price_policy_service import (
    resolve_hospital_unit_price,
)


def load_dict(value):
    data = value or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return {}

    return data if isinstance(data, dict) else {}


def resolve_extracted_shipping_date(confirmation):
    data = load_dict(
        confirmation.extracted_confirmation_data
    )
    factory_document = (
        data.get("factory_document")
        if isinstance(data, dict)
        else None
    )

    # 只有完整 extraction 结构才允许自动回填。
    if (
        not isinstance(factory_document, dict)
        or "serial_items" not in data
    ):
        return None, "incomplete_extracted_confirmation_data"

    raw_date = (
        factory_document.get(
            "shipping_date_only_iso"
        )
        or factory_document.get("shipping_date")
        or factory_document.get("date")
    )
    parsed = parse_business_date(raw_date)

    if parsed:
        return (
            parsed,
            "factory_document.shipping_date",
        )

    return None, "unparseable_shipping_date"


class Command(BaseCommand):
    help = (
        "Dry-run audit for hospital/factory pricing "
        "reference dates. Prices are never overwritten."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply-date-backfill",
            action="store_true",
            help=(
                "Persist only safely parsed Order.order_date "
                "and FactoryConfirmation.shipping_date. "
                "Price snapshots are never changed."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        apply_backfill = options[
            "apply_date_backfill"
        ]
        counters = {
            "orders_backfillable": 0,
            "orders_unresolved": 0,
            "orders_backfilled": 0,
            "hospital_snapshot_issues": 0,
            "confirmations_backfillable": 0,
            "confirmations_unresolved": 0,
            "confirmations_backfilled": 0,
            "batch_date_mismatches": 0,
        }

        generated_invoice_order_ids = set(
            GeneratedDocument.objects.filter(
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                )
            ).values_list(
                "order_id",
                flat=True,
            )
        )

        for order in Order.objects.order_by("id"):
            if not order.order_date:
                parsed, source = (
                    resolve_extracted_order_date(
                        order.extracted_order_data
                    )
                )

                if parsed:
                    counters[
                        "orders_backfillable"
                    ] += 1
                    self.stdout.write(
                        f"ORDER_BACKFILL order={order.id} "
                        f"date={parsed} source={source}"
                    )

                    if apply_backfill:
                        order.order_date = parsed
                        order.save(
                            update_fields=[
                                "order_date",
                                "updated_at",
                            ]
                        )
                        counters[
                            "orders_backfilled"
                        ] += 1
                else:
                    counters[
                        "orders_unresolved"
                    ] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            "ORDER_MANUAL_REVIEW "
                            f"order={order.id} "
                            f"bon={order.bon_de_commande}"
                        )
                    )
                continue

            # 已生成医院发票必须保留旧文档和快照。
            if order.id in generated_invoice_order_ids:
                continue

            for item in order.items.select_related(
                "product",
                "price_policy",
            ):
                issue_messages = []

                if (
                    item.price_policy_date
                    != order.order_date
                ):
                    issue_messages.append(
                        "snapshot_date_mismatch"
                    )

                if not item.product_id:
                    issue_messages.append(
                        "missing_product"
                    )
                else:
                    resolution = (
                        resolve_hospital_unit_price(
                            product=item.product,
                            hospital=order.hospital,
                            reference_date=(
                                order.order_date
                            ),
                            order_factory=(
                                order.factory
                            ),
                        )
                    )

                    if resolution["errors"]:
                        issue_messages.append(
                            "price_policy_conflict"
                        )
                    elif not resolution["policy"]:
                        issue_messages.append(
                            "missing_dated_price"
                        )
                    elif (
                        resolution["unit_price"]
                        != item.hospital_unit_price
                    ):
                        issue_messages.append(
                            "snapshot_price_mismatch"
                        )

                if issue_messages:
                    counters[
                        "hospital_snapshot_issues"
                    ] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            "HOSPITAL_SNAPSHOT_REVIEW "
                            f"order={order.id} "
                            f"item={item.id} "
                            f"product={item.product_code} "
                            f"issues={','.join(issue_messages)}"
                        )
                    )

        for confirmation in (
            FactoryConfirmation.objects
            .select_related("shipment_batch")
            .order_by("id")
        ):
            if not confirmation.shipping_date:
                parsed, source = (
                    resolve_extracted_shipping_date(
                        confirmation
                    )
                )

                if parsed:
                    counters[
                        "confirmations_backfillable"
                    ] += 1
                    self.stdout.write(
                        "CONFIRMATION_BACKFILL "
                        f"confirmation={confirmation.id} "
                        f"date={parsed} source={source}"
                    )

                    if apply_backfill:
                        confirmation.shipping_date = (
                            parsed
                        )
                        confirmation.save(
                            update_fields=[
                                "shipping_date",
                                "updated_at",
                            ]
                        )
                        counters[
                            "confirmations_backfilled"
                        ] += 1
                else:
                    counters[
                        "confirmations_unresolved"
                    ] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            "CONFIRMATION_MANUAL_REVIEW "
                            f"confirmation={confirmation.id} "
                            f"reason={source}"
                        )
                    )

            shipping_date = (
                confirmation.shipping_date
            )

            if (
                shipping_date
                and hasattr(
                    confirmation,
                    "shipment_batch",
                )
                and (
                    confirmation
                    .shipment_batch
                    .batch_date
                    != shipping_date
                )
            ):
                counters[
                    "batch_date_mismatches"
                ] += 1
                self.stdout.write(
                    self.style.WARNING(
                        "BATCH_DATE_MISMATCH "
                        f"confirmation={confirmation.id} "
                        f"batch={confirmation.shipment_batch.id} "
                        f"batch_date="
                        f"{confirmation.shipment_batch.batch_date} "
                        f"shipping_date={shipping_date}"
                    )
                )

        mode = (
            "APPLY_DATE_BACKFILL"
            if apply_backfill
            else "DRY_RUN"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode} SUMMARY {counters}"
            )
        )
