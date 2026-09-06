from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from shipments.models import ShipmentBatch
from workflow.services.backorder_document_regeneration_service import (
    ALL,
    INVOICE,
    PURCHASE_ORDER,
    build_regeneration_preview,
    get_backorder_batch_reason,
    is_backorder_batch,
    iter_backorder_batches,
    regenerate_backorder_documents,
)


def format_value(value):
    if value in (None, ""):
        return "-"

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def load_source_dict(document):
    if not document:
        return {}

    value = document.source_data or {}
    if isinstance(value, str):
        try:
            import json
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}

    return value if isinstance(value, dict) else {}


def get_price_rows(document):
    source_data = load_source_dict(document)
    basis = source_data.get("pricing_basis") or {}
    rows = basis.get("rows") or []
    if not rows:
        po_data = source_data.get("po_data") or {}
        rows = po_data.get("items") or []
    return {
        str(row.get("product_code") or ""): row
        for row in rows
        if isinstance(row, dict)
    }


def build_price_comparisons(preview):
    current_rows = get_price_rows(
        preview.get("po_document")
    )
    new_rows = {
        str(row.get("product_code") or ""): row
        for row in preview.get("po_new_prices") or []
    }
    comparisons = []

    for product_code in sorted(
        set(current_rows) | set(new_rows)
    ):
        current = current_rows.get(product_code) or {}
        new = new_rows.get(product_code) or {}
        current_price = (
            current.get("factory_base_unit_price")
            or current.get("base_factory_unit_price")
            or current.get("unit_price_raw")
            or current.get("unit_price")
        )
        new_price = new.get("base_unit_price")

        if not current_price or not new_price:
            continue

        try:
            current_price = Decimal(str(current_price))
            new_price = Decimal(str(new_price))
        except (InvalidOperation, TypeError, ValueError):
            continue

        if current_price == new_price:
            reason = (
                "当前有效 PricePolicy 与现有文档基础工厂价一致。"
            )
        else:
            reason = (
                "当前批次工厂实际发货日命中的 PricePolicy "
                "基础工厂价不同。"
            )

        comparisons.append(
            {
                "product_code": product_code,
                "current": current_price,
                "new": new_price,
                "reason": reason,
            }
        )

    return comparisons


class Command(BaseCommand):
    help = (
        "Dry-run or explicitly regenerate Invoice / Factory PO "
        "for one backorder batch or all backorder batches."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(
            required=True
        )
        group.add_argument(
            "--batch-id",
            type=int,
            help="Existing replenishment ShipmentBatch ID.",
        )
        group.add_argument(
            "--all-backorders",
            action="store_true",
            help="Select all existing replenishment ShipmentBatch records.",
        )
        parser.add_argument(
            "--document-type",
            choices=[
                INVOICE,
                PURCHASE_ORDER,
                ALL,
            ],
            default=ALL,
            help="Document type to regenerate (default: all).",
        )
        parser.add_argument(
            "--factory-shipping-date",
            help=(
                "YYYY-MM-DD; used only when an inventory allocation "
                "has no stored actual factory shipping date."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Actually overwrite/generate selected documents. "
                "Default is dry-run."
            ),
        )

    def parse_manual_date(self, raw_value):
        if not raw_value:
            return None

        try:
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise CommandError(
                "--factory-shipping-date 必须使用 YYYY-MM-DD 格式。"
            ) from exc

    def get_selected_batches(self, options):
        if options.get("all_backorders"):
            return list(iter_backorder_batches())

        try:
            return [
                ShipmentBatch.objects
                .select_related(
                    "order",
                    "factory_confirmation",
                    "inventory_allocation",
                )
                .get(id=options["batch_id"])
            ]
        except ShipmentBatch.DoesNotExist as exc:
            raise CommandError(
                "指定的 ShipmentBatch 不存在。"
            ) from exc

    def print_preview(self, preview, apply):
        self.stdout.write(
            f"--- ShipmentBatch {preview['batch_id']} ---"
        )
        self.stdout.write(
            f"bon_de_commande: {preview['bon_de_commande']}"
        )
        self.stdout.write(
            f"source_type: {preview['source_type']}"
        )
        self.stdout.write(
            f"补发判定依据: "
            f"{format_value(preview['backorder_reason'])}"
        )
        self.stdout.write(
            f"batch_number: {preview['batch_number']}"
        )
        self.stdout.write(
            f"Order.order_date: "
            f"{format_value(preview['order_date'])}"
        )
        self.stdout.write(
            f"Invoice 当前价格基准: "
            f"{format_value(preview['invoice_current_basis'])}"
        )
        self.stdout.write(
            f"Invoice 新价格基准: "
            f"{format_value(preview['invoice_new_basis'])}"
        )
        self.stdout.write(
            f"Factory PO 当前价格基准: "
            f"{format_value(preview['po_current_basis'])}"
        )
        self.stdout.write(
            f"Factory PO 新价格基准: "
            f"{format_value(preview['po_new_basis'])}"
        )
        self.stdout.write(
            f"工厂发货日期: "
            f"{format_value(preview['factory_shipping_date'])}"
        )
        self.stdout.write(
            f"工厂发货日期来源: "
            f"{format_value(preview['factory_shipping_date_source'])}"
        )
        self.stdout.write(
            f"当前 Invoice ID: "
            f"{format_value(preview['invoice_document_id'])}"
        )
        self.stdout.write(
            f"当前 Factory PO ID: "
            f"{format_value(preview['po_document_id'])}"
        )
        self.stdout.write(
            "是否需要创建缺失文档: "
            f"Invoice={'是' if preview['missing_invoice'] else '否'}, "
            f"Factory PO={'是' if preview['missing_po'] else '否'}"
        )
        self.stdout.write(
            "将重新生成: "
            + ", ".join(preview["requested_documents"])
        )
        self.stdout.write(
            f"执行模式: {'APPLY' if apply else 'DRY-RUN'}"
        )
        self.stdout.write(
            f"是否允许执行: "
            f"{'是' if preview['can_execute'] else '否'}"
        )

        if preview["blockers"]:
            self.stdout.write("阻止原因:")
            for blocker in preview["blockers"]:
                self.stdout.write(
                    self.style.ERROR(f"- {blocker}")
                )
        else:
            self.stdout.write("阻止原因: 无")

    def print_summary(
        self,
        previews,
        apply,
        applied_count=0,
        failed_count=0,
        created_counts=None,
        updated_counts=None,
        failures=None,
    ):
        created_counts = created_counts or Counter()
        updated_counts = updated_counts or Counter()
        failures = failures or []

        allowed = sum(
            1
            for preview in previews
            if preview["can_execute"]
        )
        blocked = len(previews) - allowed
        existing_invoice = sum(
            1
            for preview in previews
            if preview["invoice_document_id"]
        )
        existing_po = sum(
            1
            for preview in previews
            if preview["po_document_id"]
        )
        missing_invoice = sum(
            1
            for preview in previews
            if preview["missing_invoice"]
        )
        missing_po = sum(
            1
            for preview in previews
            if preview["missing_po"]
        )

        self.stdout.write("=== 批量汇总 ===")
        self.stdout.write(
            f"找到的补发批次数: {len(previews)}"
        )
        self.stdout.write(
            f"可执行批次数: {allowed}"
        )
        self.stdout.write(
            f"被阻止批次数: {blocked}"
        )
        self.stdout.write(
            f"已有 Invoice 数量: {existing_invoice}"
        )
        self.stdout.write(
            f"已有 Factory PO 数量: {existing_po}"
        )
        self.stdout.write(
            f"缺失 Invoice 数量: {missing_invoice}"
        )
        self.stdout.write(
            f"缺失 Factory PO 数量: {missing_po}"
        )

        if apply:
            self.stdout.write(
                f"实际重生成批次数: {applied_count}"
            )
            self.stdout.write(
                f"失败批次数: {failed_count}"
            )
            self.stdout.write(
                f"更新 Invoice 数量: "
                f"{updated_counts[INVOICE]}"
            )
            self.stdout.write(
                f"更新 Factory PO 数量: "
                f"{updated_counts[PURCHASE_ORDER]}"
            )
            self.stdout.write(
                f"新建 Invoice 数量: "
                f"{created_counts[INVOICE]}"
            )
            self.stdout.write(
                f"新建 Factory PO 数量: "
                f"{created_counts[PURCHASE_ORDER]}"
            )

        changed = []
        unchanged_165 = []
        for preview in previews:
            for comparison in build_price_comparisons(
                preview
            ):
                if (
                    comparison["current"] == Decimal("165")
                    and comparison["new"] == Decimal("120")
                ):
                    changed.append(
                        (
                            preview["batch_id"],
                            comparison["product_code"],
                        )
                    )
                elif (
                    comparison["current"] == Decimal("165")
                    and comparison["new"] == Decimal("165")
                ):
                    unchanged_165.append(
                        (
                            preview["batch_id"],
                            comparison["product_code"],
                            comparison["reason"],
                        )
                    )

        self.stdout.write(
            "165 -> 120 的批次/产品: "
            + (
                ", ".join(
                    f"{batch_id}/{product_code}"
                    for batch_id, product_code in changed
                )
                if changed
                else "无"
            )
        )
        self.stdout.write(
            "仍为 165 的批次/产品及原因: "
            + (
                "; ".join(
                    f"{batch_id}/{product_code}: {reason}"
                    for batch_id, product_code, reason
                    in unchanged_165
                )
                if unchanged_165
                else "无"
            )
        )

        if failures:
            self.stdout.write("失败批次:")
            for batch_id, message in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"- {batch_id}: {message}"
                    )
                )

    def handle(self, *args, **options):
        manual_date = self.parse_manual_date(
            options.get("factory_shipping_date")
        )
        batches = self.get_selected_batches(options)

        if (
            manual_date
            and not options.get("all_backorders")
            and batches
            and batches[0].source_type
            != ShipmentBatch.SourceType.INVENTORY_ALLOCATION
        ):
            raise CommandError(
                "--factory-shipping-date 只允许用于 "
                "inventory_allocation 补发批次。"
            )

        previews = [
            build_regeneration_preview(
                batch=batch,
                document_type=options["document_type"],
                manual_factory_shipping_date=manual_date,
            )
            for batch in batches
        ]

        for preview in previews:
            self.print_preview(
                preview,
                apply=options["apply"],
            )

        self.print_summary(
            previews,
            apply=options["apply"],
        )

        if not options["apply"]:
            return

        if not previews:
            return

        blocked = [
            preview
            for preview in previews
            if not preview["can_execute"]
        ]
        if blocked:
            raise CommandError(
                "存在阻止批次，未执行任何 --apply。"
            )

        applied_count = 0
        failed_count = 0
        created_counts = Counter()
        updated_counts = Counter()
        failures = []

        for preview in previews:
            try:
                result = regenerate_backorder_documents(
                    batch=preview["batch"],
                    document_type=options["document_type"],
                    manual_factory_shipping_date=manual_date,
                )
                applied_count += 1

                for document_type, key in (
                    (INVOICE, "invoice_document"),
                    (PURCHASE_ORDER, "po_document"),
                ):
                    if (
                        document_type
                        not in preview["requested_documents"]
                    ):
                        continue
                    if preview[key] is None:
                        created_counts[document_type] += 1
                    else:
                        updated_counts[document_type] += 1
            except Exception as exc:
                failed_count += 1
                failures.append(
                    (
                        preview["batch_id"],
                        str(exc),
                    )
                )

        self.print_summary(
            previews,
            apply=True,
            applied_count=applied_count,
            failed_count=failed_count,
            created_counts=created_counts,
            updated_counts=updated_counts,
            failures=failures,
        )

        if failures:
            raise CommandError(
                "部分补发批次执行失败，请查看失败批次明细。"
            )
