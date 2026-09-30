from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase

from factory_confirmations.models import FactoryConfirmation, SerialItem
from hospitals.models import Hospital
from orders.models import Order, OrderItem
from portal.services.order_portal_service import (
    build_order_detail_context,
    get_order_next_action,
    order_combined_status,
    save_order_manual_edit,
    validate_portal_order_after_extraction,
)
from pricing.models import PricePolicy
from products.models import Product
from shipments.models import ShipmentBatch, ShipmentBatchItem
from shipments.services.shipment_validation_service import (
    validate_shipment_batch,
)


class HospitalOrderStageValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="hospital-order-stage",
            password="test",
        )
        self.hospital = Hospital.objects.create(
            name="Hospital Order Stage Test Hospital",
            billing_address="1 Billing Street",
            default_shipping_address="2 Shipping Street",
        )
        self.product = Product.objects.create(
            code="BMA-STAGE-001",
            description="Stage test product",
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=None,
        )
        self.policy = PricePolicy.objects.create(
            name="Hospital stage policy",
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
        )
        self.order = Order.objects.create(
            bon_de_commande="HOSPITAL-STAGE-001",
            hospital_name=self.hospital.name,
            hospital=self.hospital,
            hospital_order_pdf="hospital_orders/stage-test.pdf",
            order_date=date(2026, 9, 1),
            extraction_status=Order.ExtractionStatus.SUCCESS,
            extracted_order_data={"summary": {"bon_de_commande": "HOSPITAL-STAGE-001"}},
            shipping_address_data={"lines": ["2 Shipping Street"]},
            billing_address_data={"lines": ["1 Billing Street"]},
            created_by=self.user,
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            description=self.product.description,
            requested_quantity=1,
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=None,
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            price_policy=self.policy,
            price_policy_date=self.order.order_date,
            product_match_status=OrderItem.ProductMatchStatus.OK,
        )

    def test_basic_validation_without_factory_data_has_no_serial_or_price_warning(self):
        errors, warnings = validate_portal_order_after_extraction(self.order)

        validation_text = " ".join(errors + warnings).lower()
        for forbidden in ["serial", "sn", "expiration", "缺少有效工厂采购价"]:
            self.assertNotIn(forbidden.lower(), validation_text)

    def test_missing_expiration_discount_is_not_a_basic_warning(self):
        self.item.expiration_discount_rate = None
        self.item.save(
            update_fields=["expiration_discount_rate", "updated_at"]
        )

        errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertEqual(errors, [])
        self.assertNotIn("缺少临期折扣率快照", " ".join(warnings))

    def test_missing_expiration_threshold_is_not_a_basic_warning(self):
        self.item.expiration_threshold_days = None
        self.item.save(
            update_fields=["expiration_threshold_days", "updated_at"]
        )

        errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertEqual(errors, [])
        self.assertNotIn("缺少临期门槛快照", " ".join(warnings))

    def test_all_factory_side_snapshots_are_optional_in_basic_validation(self):
        self.item.factory_unit_price = None
        self.item.expiration_discount_rate = None
        self.item.expiration_threshold_days = None
        self.item.save(
            update_fields=[
                "factory_unit_price",
                "expiration_discount_rate",
                "expiration_threshold_days",
                "updated_at",
            ]
        )

        errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_hospital_unit_price_is_still_required(self):
        self.item.hospital_unit_price = Decimal("0.00")
        self.item.save(
            update_fields=["hospital_unit_price", "updated_at"]
        )

        _errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertIn("缺少有效医院单价", " ".join(warnings))

    def test_hospital_price_policy_is_still_required(self):
        self.item.price_policy = None
        self.item.save(
            update_fields=["price_policy", "updated_at"]
        )

        _errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertIn("尚未应用医院价格规则", " ".join(warnings))

    def test_stale_factory_side_warnings_are_rebuilt(self):
        self.order.document_validation_data = {
            "source": "portal_order_basic_validation",
            "warnings": [
                "产品 BMA-STAGE-001 缺少有效工厂采购价。",
                "产品 BMA-STAGE-001 缺少临期折扣率快照。",
                "产品 BMA-STAGE-001 缺少临期门槛快照。",
            ],
            "errors": [],
        }
        self.order.save(
            update_fields=["document_validation_data", "updated_at"]
        )

        validate_portal_order_after_extraction(self.order)
        self.order.refresh_from_db()

        warning_text = " ".join(
            self.order.document_validation_data["warnings"]
        )
        self.assertNotIn("缺少有效工厂采购价", warning_text)
        self.assertNotIn("缺少临期折扣率快照", warning_text)
        self.assertNotIn("缺少临期门槛快照", warning_text)

    def test_refresh_selection_protects_non_portal_validation_source(self):
        original_data = {
            "source": "workflow_formal_validation",
            "warnings": ["正式 Workflow warning"],
            "errors": ["正式 Workflow error"],
        }
        self.order.document_validation_data = original_data
        self.order.save(
            update_fields=["document_validation_data", "updated_at"]
        )

        for candidate in Order.objects.all():
            data = candidate.document_validation_data or {}
            if data.get("source") == "portal_order_basic_validation":
                validate_portal_order_after_extraction(candidate)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.document_validation_data,
            original_data,
        )

    def test_unmatched_product_reports_product_code_and_next_action(self):
        self.item.product = None
        self.item.product_match_status = OrderItem.ProductMatchStatus.NEEDS_REVIEW
        self.item.price_policy = None
        self.item.save(
            update_fields=[
                "product",
                "product_match_status",
                "price_policy",
                "updated_at",
            ]
        )

        errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertIn("请核对产品编码", " ".join(errors + warnings))
        self.assertEqual(
            get_order_next_action(self.order, workflow_count=0),
            ("请核对产品编码", "warning"),
        )
        self.assertNotIn("Serial", " ".join(errors + warnings))

    def test_matched_product_has_no_fake_product_warning_but_still_requests_review(self):
        errors, warnings = validate_portal_order_after_extraction(self.order)

        self.assertEqual(errors, [])
        self.assertNotIn("产品编码", " ".join(warnings))
        self.assertEqual(
            get_order_next_action(self.order, workflow_count=0),
            ("请核对产品编码", "warning"),
        )
        self.assertEqual(
            order_combined_status(self.order),
            ("产品编码待核对", "warning", "product_review"),
        )

    def test_manual_save_confirms_products_then_waits_for_factory(self):
        prefix = f"item_{self.item.id}_"
        save_order_manual_edit(
            self.order,
            {
                "bon_de_commande": self.order.bon_de_commande,
                "hospital_name": self.order.hospital_name,
                "notes": "",
                "shipping_address_text": "2 Shipping Street",
                "billing_address_text": "1 Billing Street",
                prefix + "product_code": self.product.code,
                prefix + "description": self.product.description,
                prefix + "requested_quantity": "1",
                prefix + "hospital_unit_price": "250.00",
            },
        )

        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertTrue(self.item.is_manually_confirmed)
        self.assertEqual(
            self.item.product_match_status,
            OrderItem.ProductMatchStatus.MANUALLY_CONFIRMED,
        )
        self.assertEqual(
            get_order_next_action(self.order, workflow_count=0),
            ("等待工厂确认", "info"),
        )

    def test_stale_formal_serial_findings_are_hidden_and_replaced(self):
        self.order.document_validation_status = Order.DocumentValidationStatus.BLOCKED
        self.order.document_validation_data = {
            "errors": ["SerialItem BMA-STAGE-001 / NO_SERIAL: missing serial_number."],
            "warnings": ["missing expiration_date"],
        }
        self.order.save(
            update_fields=[
                "document_validation_status",
                "document_validation_data",
                "updated_at",
            ]
        )

        request = RequestFactory().get("/portal/orders/")
        request.session = {}
        request.user = self.user
        context = build_order_detail_context(request, self.order.id)
        self.assertEqual(context["validation_errors"], [])
        self.assertEqual(context["validation_warnings"], [])

        rendered = render_to_string(
            "portal/orders/detail.html",
            context,
            request=request,
        )
        self.assertIn("当前没有提取或验证问题。", rendered)
        self.assertNotIn("NO_SERIAL", rendered)

        validate_portal_order_after_extraction(self.order)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.document_validation_data["source"],
            "portal_order_basic_validation",
        )
        self.assertNotIn(
            "serial",
            str(self.order.document_validation_data).lower(),
        )


class WorkflowSerialProtectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="workflow-serial-stage",
            password="test",
        )
        self.product = Product.objects.create(
            code="BMA-WORKFLOW-001",
            description="Workflow serial test",
        )
        self.order = Order.objects.create(
            bon_de_commande="WORKFLOW-SERIAL-001",
            hospital_name="Workflow Hospital",
            hospital_order_pdf="hospital_orders/workflow-test.pdf",
            created_by=self.user,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=1,
            confirmed_quantity=1,
            backordered_quantity=0,
            product_match_status=OrderItem.ProductMatchStatus.OK,
        )
        self.confirmation = FactoryConfirmation.objects.create(
            order=self.order,
            confirmation_pdf="factory_confirmations/workflow-test.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            shipping_date=date(2026, 9, 10),
            created_by=self.user,
        )
        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            factory_confirmation=self.confirmation,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            batch_number=1,
            batch_date=date(2026, 9, 10),
            month_key="2026-09",
        )
        ShipmentBatchItem.objects.create(
            batch=self.batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=1,
        )

    def test_workflow_still_blocks_missing_serial_number(self):
        SerialItem.objects.create(
            order=self.order,
            factory_confirmation=self.confirmation,
            product=self.product,
            product_code=self.product.code,
            serial_number="",
            expiration_date=date(2028, 1, 1),
        )

        result = validate_shipment_batch(self.batch, save=False)

        self.assertEqual(result["validation_status"], ShipmentBatch.ValidationStatus.BLOCKED)
        self.assertIn("缺少 serial_number", " ".join(result["errors"]))

    def test_workflow_still_blocks_missing_expiration_date(self):
        SerialItem.objects.create(
            order=self.order,
            factory_confirmation=self.confirmation,
            product=self.product,
            product_code=self.product.code,
            serial_number="SERIAL-WORKFLOW-001",
            expiration_date=None,
        )

        result = validate_shipment_batch(self.batch, save=False)

        self.assertEqual(result["validation_status"], ShipmentBatch.ValidationStatus.BLOCKED)
        self.assertIn("缺少 expiration_date", " ".join(result["errors"]))

