from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from openpyxl import load_workbook

from backorders.models import BackorderLine, BackorderOrderFolder
from hospitals.models import Hospital
from orders.models import Order


XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
EXPECTED_HEADERS = [
    "医院订单",
    "医院",
    "产品号",
    "订单数量",
    "已发数量",
    "待补发",
]


class BackorderExportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="backorder-export-staff",
            password="test-password",
            is_staff=True,
        )
        self.hospital = Hospital.objects.create(name="Canonical Hospital")
        self.primary_line = self.create_line(
            order_number="00146769",
            hospital_name="Fallback Hospital",
            hospital=self.hospital,
            product_code="00042-A",
            requested_quantity=7,
            shipped_quantity=2,
            remaining_quantity=5,
        )
        self.fallback_line = self.create_line(
            order_number="FALLBACK-ORDER",
            hospital_name="Fallback Only Hospital",
            product_code="FALLBACK-PRODUCT",
            requested_quantity=9,
            shipped_quantity=4,
            remaining_quantity=5,
        )
        self.completed_line = self.create_line(
            order_number="COMPLETED-ORDER",
            hospital_name="Completed Hospital",
            product_code="COMPLETED-PRODUCT",
            requested_quantity=4,
            shipped_quantity=4,
            remaining_quantity=0,
        )
        self.url = reverse("portal:backorder_export_xlsx")

    def create_line(
        self,
        *,
        order_number,
        hospital_name,
        product_code,
        requested_quantity,
        shipped_quantity,
        remaining_quantity,
        hospital=None,
    ):
        order = Order.objects.create(
            bon_de_commande=order_number,
            hospital_name=hospital_name,
            hospital=hospital,
            hospital_order_pdf=f"hospital_orders/{order_number}.pdf",
            created_by=self.user,
        )
        folder = BackorderOrderFolder.objects.create(order=order)
        return BackorderLine.objects.create(
            order_folder=folder,
            order=order,
            product_code=product_code,
            requested_quantity=requested_quantity,
            shipped_quantity=shipped_quantity,
            remaining_quantity=remaining_quantity,
        )

    def export_workbook(self, query=""):
        self.client.force_login(self.user)
        response = self.client.get(f"{self.url}{query}")
        workbook = load_workbook(BytesIO(response.content))
        self.addCleanup(workbook.close)
        return response, workbook, workbook.active

    def test_export_requires_authenticated_staff_user(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_export_response_headers_and_workbook_layout(self):
        response, workbook, worksheet = self.export_workbook()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_CONTENT_TYPE)
        self.assertIn(".xlsx", response["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", response["Content-Disposition"])
        self.assertEqual(workbook.sheetnames, ["待补发库"])
        self.assertEqual(
            [cell.value for cell in worksheet[1]],
            EXPECTED_HEADERS,
        )
        self.assertEqual(worksheet.freeze_panes, "A2")
        self.assertEqual(worksheet.auto_filter.ref, "A1:F3")
        self.assertTrue(all(cell.font.bold for cell in worksheet[1]))
        self.assertTrue(
            all(cell.alignment.horizontal == "center" for cell in worksheet[1])
        )
        self.assertEqual(
            [
                worksheet.column_dimensions[column].width
                for column in "ABCDEF"
            ],
            [18, 35, 20, 12, 12, 12],
        )

    def test_export_maps_fields_and_prefers_hospital_foreign_key(self):
        _response, _workbook, worksheet = self.export_workbook(
            "?q=00146769"
        )

        self.assertEqual(
            [cell.value for cell in worksheet[2]],
            ["00146769", "Canonical Hospital", "00042-A", 7, 2, 5],
        )
        self.assertEqual(
            [cell.data_type for cell in worksheet[2][3:6]],
            ["n", "n", "n"],
        )
        self.assertEqual(
            [cell.number_format for cell in worksheet[2][3:6]],
            ["0", "0", "0"],
        )
        self.assertTrue(
            all(cell.alignment.vertical == "center" for cell in worksheet[2])
        )

    def test_export_falls_back_to_order_hospital_name(self):
        _response, _workbook, worksheet = self.export_workbook(
            "?q=FALLBACK-ORDER"
        )

        self.assertEqual(worksheet["B2"].value, "Fallback Only Hospital")

    def test_default_export_only_includes_active_remaining_lines(self):
        _response, _workbook, worksheet = self.export_workbook()

        exported_orders = {
            worksheet.cell(row=row, column=1).value
            for row in range(2, worksheet.max_row + 1)
        }
        self.assertEqual(exported_orders, {"00146769", "FALLBACK-ORDER"})
        self.assertNotIn(
            self.completed_line.order.bon_de_commande,
            exported_orders,
        )

    def test_export_applies_current_filters(self):
        _response, _workbook, worksheet = self.export_workbook(
            "?q=00042-A&status=active"
        )

        self.assertEqual(worksheet.max_row, 2)
        self.assertEqual(worksheet["C2"].value, "00042-A")

    def test_export_ignores_page_and_exports_all_filtered_results(self):
        bulk_order = Order.objects.create(
            bon_de_commande="BULK-ORDER",
            hospital_name="Bulk Hospital",
            hospital_order_pdf="hospital_orders/bulk.pdf",
            created_by=self.user,
        )
        folder = BackorderOrderFolder.objects.create(order=bulk_order)
        BackorderLine.objects.bulk_create(
            [
                BackorderLine(
                    order_folder=folder,
                    order=bulk_order,
                    product_code=f"BULK-{index:03d}",
                    requested_quantity=2,
                    shipped_quantity=1,
                    remaining_quantity=1,
                )
                for index in range(301)
            ]
        )

        _response, _workbook, worksheet = self.export_workbook(
            "?q=BULK-ORDER&page=99"
        )

        self.assertEqual(worksheet.max_row, 302)

    def test_empty_export_is_valid_header_only_workbook(self):
        _response, workbook, worksheet = self.export_workbook(
            "?q=NO-SUCH-BACKORDER"
        )

        self.assertEqual(workbook.sheetnames, ["待补发库"])
        self.assertEqual(worksheet.max_row, 1)
        self.assertEqual(
            [cell.value for cell in worksheet[1]],
            EXPECTED_HEADERS,
        )
        self.assertEqual(worksheet.auto_filter.ref, "A1:F1")

    def test_formula_like_text_is_stored_as_text(self):
        dangerous_hospital = Hospital.objects.create(name="-Dangerous Hospital")
        self.create_line(
            order_number="=1+1",
            hospital_name="Unused",
            hospital=dangerous_hospital,
            product_code="@SUM(A1:A2)",
            requested_quantity=1,
            shipped_quantity=0,
            remaining_quantity=1,
        )

        _response, _workbook, worksheet = self.export_workbook("?q=%3D1%2B1")

        self.assertEqual(
            [worksheet.cell(row=2, column=column).value for column in range(1, 4)],
            ["=1+1", "-Dangerous Hospital", "@SUM(A1:A2)"],
        )
        self.assertEqual(
            [
                worksheet.cell(row=2, column=column).data_type
                for column in range(1, 4)
            ],
            ["s", "s", "s"],
        )

    def test_list_export_link_preserves_filters_but_drops_page(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("portal:backorder_list"),
            {
                "q": "00146769",
                "status": "active",
                "hospital": "Canonical",
                "page": "4",
            },
        )

        self.assertEqual(
            response.context["export_query_string"],
            "q=00146769&status=active&hospital=Canonical",
        )
