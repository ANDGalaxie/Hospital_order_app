from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import override_settings

from commercial_pos.models import CommercialPOPricePolicy
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem


def seed_policies():
    CommercialPOPricePolicy.objects.all().delete()
    CommercialPOPricePolicy.objects.create(name="165", end_date=date(2026, 4, 19), unit_price=Decimal("165.00"))
    CommercialPOPricePolicy.objects.create(name="160", start_date=date(2026, 4, 20), unit_price=Decimal("160.00"))


def make_batch(user, *, bon="147891", quantity=1, shipping_date=date(2026, 4, 22)):
    factory = Factory.objects.create(name=f"Synthetic factory {bon}", short_name="SYN")
    dept = ProductCategory.objects.create(name=f"Department {bon}", node_type="department")
    factory_node = ProductCategory.objects.create(name=f"Factory {bon}", parent=dept, node_type="factory", factory=factory)
    category = ProductCategory.objects.create(name=f"Category {bon}", parent=factory_node, node_type="category")
    product = Product.objects.create(code=f"SYN-{bon}", description="Synthetic hospital product", category=category,
                                     factory=factory, hospital_unit_price=Decimal("270.00"), factory_unit_price=Decimal("120.00"))
    policy = PricePolicy.objects.create(name="Internal synthetic policy", factory=factory, category=category,
                                       start_date=date(2026, 1, 1), hospital_unit_price=Decimal("270.00"),
                                       factory_unit_price=Decimal("120.00"), expiration_discount_rate=Decimal("0.30"),
                                       expiration_threshold_days=365)
    order = Order.objects.create(bon_de_commande=bon, order_date=date(2026, 4, 13), hospital_name="Synthetic Hospital",
                                 hospital_order_pdf="hospital_orders/synthetic.pdf", factory=factory, created_by=user,
                                 shipping_address_data={"street": "10 Synthetic Avenue", "postal_city": "75000 Paris", "country": "France"})
    order_item = OrderItem.objects.create(order=order, product=product, product_code=product.code,
                                         requested_quantity=quantity, confirmed_quantity=quantity,
                                         hospital_unit_price=Decimal("270.00"), price_policy=policy, price_policy_date=order.order_date)
    confirmation = FactoryConfirmation.objects.create(order=order, factory=factory, confirmation_pdf="factory_confirmations/synthetic.pdf",
                                                      extraction_status="success", shipping_date=shipping_date, created_by=user)
    batch = ShipmentBatch.objects.create(order=order, factory_confirmation=confirmation, batch_date=shipping_date, month_key="2026-04",
                                         total_requested_quantity=quantity, shipped_this_batch_quantity=quantity,
                                         total_shipped_after_batch_quantity=quantity)
    batch_item = ShipmentBatchItem.objects.create(batch=batch, product=product, product_code=product.code, shipped_quantity=quantity)
    serial = SerialItem.objects.create(order=order, factory_confirmation=confirmation, product=product, product_code=product.code,
                                       serial_number=f"SERIAL-{bon}", expiration_date=date(2026, 6, 1), raw_data={"delivered_quantity": quantity})
    workflow = DocumentWorkflowItem.objects.create(order=order, shipment_batch=batch)
    return order, batch, workflow, order_item, batch_item, serial


def fake_render(snapshot, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "document.html").write_text(f"<html>{snapshot['document_number']}</html>")
    (directory / "document.pdf").write_bytes(b"%PDF-1.7\n" + b"synthetic " * 20)


def fake_legacy_writer(*, html_content, html_path, pdf_path, project_root):
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content)
    pdf_path.write_bytes(b"%PDF-1.7\n" + b"original synthetic " * 20)


def make_factory_po(user, batch, *, quantity=None, number=None):
    """An explicitly generated frozen Factory PO for Commercial-only fixtures."""
    from documents.models import GeneratedDocument
    from documents.services.document_generation_service import load_json_config, format_po_eur, render_po_html
    from django.conf import settings
    from workflow.services.workflow_document_generation_service import build_batch_factory_po_data, build_factory_info_from_model
    payload = build_batch_factory_po_data(
        batch, load_json_config(Path(settings.BASE_DIR) / "config/company_info.json"),
        build_factory_info_from_model(batch.order.factory),
        {"po_number": number or f"DELAHK-FROZEN-B{batch.batch_number}"},
        factory_shipping_date=batch.batch_date,
        factory_shipping_date_source="factory_confirmation.shipping_date", factory=batch.order.factory,
    )
    if quantity is not None:
        assert len(payload["items"]) == 1
        row = payload["items"][0]
        row.update(quantity_raw=float(quantity), quantity=f"{quantity:.2f}", batch_quantity=str(quantity))
        amount = Decimal(str(row["factory_net_unit_price"])) * Decimal(quantity)
        row.update(amount_raw=float(amount), amount=format_po_eur(amount), line_total=str(amount))
        payload["totals"].update(total_units_raw=float(quantity), total_units=str(quantity), total_raw=float(amount), total=format_po_eur(amount))
    path = Path(settings.MEDIA_ROOT) / "frozen_factory" / str(batch.pk)
    fake_render({"document_number": payload["po"]["po_number"]}, path)
    (path / "document.html").write_text(render_po_html(
        po_data=payload, template_path=Path(settings.BASE_DIR) / "templates/factory_purchase_order.html"), encoding="utf-8")
    return GeneratedDocument.objects.create(
        order=batch.order, shipment_batch=batch, document_type="factory_po",
        document_number=payload["po"]["po_number"], generated_by=user,
        source_data={"shipment_batch_id": batch.pk, "po_data": payload},
        pdf_file=str((path / "document.pdf").relative_to(settings.MEDIA_ROOT)),
        html_file=str((path / "document.html").relative_to(settings.MEDIA_ROOT)),
    )


class FixtureMixin:
    def setUp(self):
        super().setUp()
        seed_policies()
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_settings = override_settings(MEDIA_ROOT=self.media.name, MEDIA_URL="/portal/files/")
        self.media_settings.enable()
        self.addCleanup(self.media_settings.disable)
        self.user = get_user_model().objects.create_user(username="synthetic-internal", password="test", is_staff=True)
        self.order, self.batch, self.workflow, self.order_item, self.batch_item, self.serial = make_batch(self.user)
