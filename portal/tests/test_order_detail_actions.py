import io
import tempfile
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve, reverse

from documents.models import GeneratedDocument
from documents.services.factory_order_request_service import (
    generate_factory_order_request,
)
from factories.models import Factory
from hospitals.models import Hospital
from orders.models import Order, OrderItem
from portal import views
from portal.services.order_portal_service import get_order_next_action
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class FakePdfFile:
    def __init__(self, content=b"%PDF-current"):
        self.content = content
        self.name = "factory_requests/current-request.pdf"

    def __bool__(self):
        return True

    def open(self, mode):
        return io.BytesIO(self.content)


class HospitalOrderDetailActionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="order-detail-actions",
            password="test",
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.factory = Factory.objects.create(
            name="Detail Action Factory",
            short_name="DAF",
            address="1 Factory Street",
        )
        self.hospital = Hospital.objects.create(
            name="Detail Action Hospital",
            billing_address="1 Billing Street",
            default_shipping_address="2 Shipping Street",
        )
        self.department = ProductCategory.objects.create(
            name="Detail Action Department",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        self.factory_node = ProductCategory.objects.create(
            name="Detail Action Factory Node",
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        self.category = ProductCategory.objects.create(
            name="Detail Action Category",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product_a = Product.objects.create(
            code="DETAIL-A",
            description="Original detail product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("250.00"),
        )
        self.product_b = Product.objects.create(
            code="DETAIL-B",
            description="Edited detail product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("250.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Detail action policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
        )
        self.order = Order.objects.create(
            bon_de_commande="DETAIL-ACTION-001",
            hospital_name=self.hospital.name,
            hospital=self.hospital,
            hospital_order_pdf="hospital_orders/detail-action.pdf",
            order_date=date(2026, 9, 1),
            extraction_status=Order.ExtractionStatus.SUCCESS,
            extracted_order_data={
                "header": {"order_date": "2026-09-01"},
                "summary": {"bon_de_commande": "DETAIL-ACTION-001"},
            },
            shipping_address_data={"lines": ["2 Shipping Street"]},
            billing_address_data={"lines": ["1 Billing Street"]},
            factory=self.factory,
            created_by=self.user,
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            product=self.product_a,
            product_code=self.product_a.code,
            description=self.product_a.description,
            requested_quantity=1,
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            price_policy=self.policy,
            price_policy_date=self.order.order_date,
            product_match_status=OrderItem.ProductMatchStatus.OK,
        )

    def edit_post_data(self, *, product=None, notes="", action="save"):
        product = product or self.product_a
        prefix = f"item_{self.item.id}_"
        return {
            "action": action,
            "bon_de_commande": self.order.bon_de_commande,
            "hospital_name": self.order.hospital_name,
            "notes": notes,
            "shipping_address_text": "2 Shipping Street",
            "billing_address_text": "1 Billing Street",
            prefix + "product_code": product.code,
            prefix + "description": product.description,
            prefix + "requested_quantity": "1",
            prefix + "hospital_unit_price": "250.00",
        }

    def fake_document(self, content=b"%PDF-current"):
        return SimpleNamespace(pdf_file=FakePdfFile(content))

    def test_detail_has_combined_actions_and_no_factory_upload_buttons(self):
        response = self.client.get(
            reverse("portal:order_detail", args=[self.order.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "生成并下载 Factory Request",
            count=2,
        )
        self.assertNotContains(response, ">生成 Factory Request</button>")
        self.assertNotContains(response, ">下载 Factory Request</a>")
        self.assertNotContains(response, "上传首批工厂文件")
        self.assertNotContains(response, "上传补发工厂文件")

        content = response.content.decode()
        self.assertLess(content.index("关联工作流"), content.index("备注"))

        upload_url = reverse(
            "portal:order_factory_upload",
            args=[self.order.id],
        )
        self.assertEqual(resolve(upload_url).func, views.order_factory_upload)

    @patch("portal.views.generate_factory_request_for_order")
    def test_top_generate_returns_pdf_download(self, generate_mock):
        generate_mock.return_value = (
            self.fake_document(b"%PDF-top"),
            [],
            [],
        )

        response = self.client.post(
            reverse("portal:order_action", args=[self.order.id]),
            {"action": "generate_request"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-top")

    def test_top_generate_with_blocking_errors_returns_detail(self):
        self.order.bon_de_commande = "UPLOAD-DETAIL-ACTION"
        self.order.save(update_fields=["bon_de_commande", "updated_at"])

        response = self.client.post(
            reverse("portal:order_action", args=[self.order.id]),
            {"action": "generate_request"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "没有成功提取 bon de commande")
        self.assertNotEqual(response.get("Content-Type"), "application/pdf")

    @patch("portal.views.generate_factory_request_for_order")
    def test_bottom_generate_saves_current_product_and_notes_before_download(
        self,
        generate_mock,
    ):
        captured = {}

        def generate_side_effect(*, order, user):
            order.refresh_from_db()
            item = order.items.get(id=self.item.id)
            captured["product_code"] = item.product_code
            captured["notes"] = order.notes
            captured["is_manually_confirmed"] = item.is_manually_confirmed
            return self.fake_document(b"%PDF-bottom"), [], []

        generate_mock.side_effect = generate_side_effect

        response = self.client.post(
            reverse("portal:order_edit", args=[self.order.id]),
            self.edit_post_data(
                product=self.product_b,
                notes="Saved before generation",
                action="generate_request",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-bottom")
        self.assertEqual(captured["product_code"], self.product_b.code)
        self.assertEqual(captured["notes"], "Saved before generation")
        self.assertTrue(captured["is_manually_confirmed"])

        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.item.product, self.product_b)
        self.assertEqual(self.order.notes, "Saved before generation")

    @patch("portal.views.generate_factory_request_for_order")
    def test_bottom_generate_saves_but_does_not_generate_when_blocked(
        self,
        generate_mock,
    ):
        post_data = self.edit_post_data(
            notes="Saved despite validation error",
            action="generate_request",
        )
        post_data[f"item_{self.item.id}_product_code"] = "UNKNOWN-CODE"

        response = self.client.post(
            reverse("portal:order_edit", args=[self.order.id]),
            post_data,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        generate_mock.assert_not_called()
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(
            self.order.notes,
            "Saved despite validation error",
        )
        self.assertIsNone(self.item.product_id)
        self.assertContains(response, "请核对产品编码")

    def test_save_action_still_saves_notes_and_confirms_product(self):
        response = self.client.post(
            reverse("portal:order_edit", args=[self.order.id]),
            self.edit_post_data(notes="Saved by normal action"),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.notes, "Saved by normal action")
        self.assertTrue(self.item.is_manually_confirmed)
        self.assertEqual(
            self.item.product_match_status,
            OrderItem.ProductMatchStatus.MANUALLY_CONFIRMED,
        )

    def test_confirmed_order_actions_follow_workflow_presence(self):
        self.item.is_manually_confirmed = True
        self.item.save(update_fields=["is_manually_confirmed", "updated_at"])

        self.assertEqual(
            get_order_next_action(self.order, workflow_count=0),
            ("等待工厂确认", "info"),
        )

        batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=1,
            batch_date=date(2026, 9, 2),
            month_key="2026-09",
        )
        DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=batch,
        )

        self.assertEqual(
            get_order_next_action(self.order, workflow_count=1),
            ("查看工作流", "success"),
        )

    def test_future_unconfirmed_order_still_requests_product_review(self):
        self.item.is_manually_confirmed = False
        self.item.product_match_status = OrderItem.ProductMatchStatus.OK
        self.item.save(
            update_fields=[
                "is_manually_confirmed",
                "product_match_status",
                "updated_at",
            ]
        )

        self.assertEqual(
            get_order_next_action(self.order, workflow_count=0),
            ("请核对产品编码", "warning"),
        )


class FactoryRequestLatestGenerationTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_settings = self.settings(MEDIA_ROOT=self.media_dir.name)
        self.media_settings.enable()

        self.user = get_user_model().objects.create_user(
            username="factory-request-latest",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="Latest Request Factory",
            address="Factory Address",
        )
        self.product = Product.objects.create(
            code="LATEST-001",
            description="First description",
            factory=self.factory,
        )
        self.order = Order.objects.create(
            bon_de_commande="LATEST-REQUEST-001",
            hospital_name="Latest Hospital",
            hospital_order_pdf="hospital_orders/latest.pdf",
            extracted_order_data={"header": {"order_date": "2026-09-01"}},
            factory=self.factory,
            created_by=self.user,
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=1,
        )

    def tearDown(self):
        self.media_settings.disable()
        self.media_dir.cleanup()

    @patch(
        "documents.services.factory_order_request_service.get_weasyprint_html"
    )
    def test_existing_request_is_updated_with_latest_order_data(
        self,
        get_html_mock,
    ):
        html_instance = MagicMock()
        html_instance.write_pdf.return_value = b"%PDF-latest"
        get_html_mock.return_value.return_value = html_instance

        first = generate_factory_order_request(
            self.order,
            generated_by=self.user,
        )

        self.product.description = "Latest description"
        self.product.save(update_fields=["description", "updated_at"])

        second = generate_factory_order_request(
            self.order,
            generated_by=self.user,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            GeneratedDocument.objects.filter(
                order=self.order,
                document_type=GeneratedDocument.DocumentType.FACTORY_ORDER_REQUEST,
            ).count(),
            1,
        )
        second.refresh_from_db()
        self.assertEqual(
            second.source_data["items"][0]["description"],
            "Latest description",
        )

