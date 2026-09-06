import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from workflow.services.order_150222_invoice_repair_service import (
    _read_original_order_prices,
    apply_order_150222_repair,
)


class OriginalOrderPriceParsingTests(SimpleTestCase):
    def test_price_column_excludes_line_total_column(self):
        """A 270.00 line total must not make a nearby 300.00 price ambiguous."""
        workspace = Path(self.temporary_directory())
        ocr_dir = workspace / "ocr"
        ocr_dir.mkdir()
        (ocr_dir / "page_1_res.json").write_text(
            json.dumps({
                "rec_texts": ["BMA-2.2510", "300.00", "900.00"],
                "rec_boxes": [
                    [100, 100, 400, 130],
                    [1400, 100, 1500, 130],
                    [1650, 100, 1750, 130],
                ],
            }),
            encoding="utf-8",
        )
        order = SimpleNamespace(extracted_order_data={"django": {"workspace": str(workspace)}})

        rows, errors = _read_original_order_prices(order)

        self.assertEqual(errors, [])
        self.assertEqual(str(rows["BMA-2.2510"]["unit_price"]), "300.00")
        self.assertEqual(
            [str(value) for value in rows["BMA-2.2510"]["nearby_totals"]],
            ["300.00", "900.00"],
        )

    def temporary_directory(self):
        from tempfile import TemporaryDirectory

        if not hasattr(self, "_temporary_directory"):
            self._temporary_directory = TemporaryDirectory()
            self.addCleanup(self._temporary_directory.cleanup)
        return self._temporary_directory.name


class RepairCommandSafetyTests(TestCase):
    def test_apply_is_refused_when_target_number_belongs_to_another_order(self):
        plan = {
            "order": SimpleNamespace(),
            "order_id": 60,
            "order_date": "2026-05-29",
            "hospital_order_pdf": "hospital_orders/original.pdf",
            "price_rows": [],
            "batches": [],
            "current_base_invoice_number": "Invoice 20260105",
            "target_base_invoice_number": "Invoice 20260505",
            "conflicts": [{"id": 110, "order_id": 55, "document_number": "Invoice 20260505"}],
            "errors": ["目标 Invoice 编号已被其他订单占用。"],
            "can_apply": False,
        }
        with patch(
            "workflow.management.commands.repair_order_150222_invoice_data.build_order_150222_repair_plan",
            return_value=plan,
        ), patch(
            "workflow.management.commands.repair_order_150222_invoice_data.apply_order_150222_repair"
        ) as apply_mock:
            with self.assertRaises(CommandError):
                call_command("repair_order_150222_invoice_data", "--apply")

        apply_mock.assert_not_called()

    def test_service_refuses_a_non_applicable_plan(self):
        with self.assertRaises(ValueError):
            apply_order_150222_repair({"can_apply": False})
