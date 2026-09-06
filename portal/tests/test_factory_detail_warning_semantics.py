from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation
from orders.models import Order
from portal.services.factory_portal_service import (
    build_factory_detail_context,
)


MISSING_BON_WARNING = "没有提取到 BON DE COMMANDE 编号。"


class FactoryDetailWarningSemanticsTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/portal/factory/1/")
        self.user = get_user_model().objects.create_user(
            username="factory-warning",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="WARNING FACTORY",
            short_name="WGF",
        )
        self.order = Order.objects.create(
            bon_de_commande="153960",
            factory=self.factory,
            created_by=self.user,
        )

    def create_confirmation(
        self,
        *,
        django_data,
        warnings,
        order=None,
        manual_confirmed=False,
    ):
        return FactoryConfirmation.objects.create(
            order=order,
            factory=self.factory,
            confirmation_pdf="factory_confirmations/warning.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            extracted_confirmation_data={
                "django": django_data,
                "warnings": warnings,
            },
            bon_de_commande_manual_confirmed=manual_confirmed,
            created_by=self.user,
        )

    def test_detected_and_matched_bon_hides_only_stale_missing_warning(self):
        confirmation = self.create_confirmation(
            order=self.order,
            manual_confirmed=True,
            django_data={
                "detected_bon_de_commande": "153960",
                "order_match_status": "matched_by_detected_bon_de_commande",
            },
            warnings=[
                MISSING_BON_WARNING,
                "保留这个仍需检查的提醒。",
            ],
        )

        context = build_factory_detail_context(
            self.request,
            confirmation.id,
        )

        self.assertEqual(
            context["warnings"],
            ["保留这个仍需检查的提醒。"],
        )
        confirmation.refresh_from_db()
        self.assertEqual(
            confirmation.extracted_confirmation_data["warnings"],
            [
                MISSING_BON_WARNING,
                "保留这个仍需检查的提醒。",
            ],
        )

    def test_manual_confirmation_hides_stale_missing_warning(self):
        confirmation = self.create_confirmation(
            order=self.order,
            manual_confirmed=True,
            django_data={
                "detected_bon_de_commande": None,
                "order_match_status": "selected_order_without_detected_bon",
            },
            warnings=[MISSING_BON_WARNING],
        )

        context = build_factory_detail_context(
            self.request,
            confirmation.id,
        )

        self.assertEqual(context["warnings"], [])

    def test_unresolved_bon_keeps_missing_warning_visible(self):
        confirmation = self.create_confirmation(
            django_data={
                "detected_bon_de_commande": None,
                "order_match_status": "not_matched",
            },
            warnings=[MISSING_BON_WARNING],
        )

        context = build_factory_detail_context(
            self.request,
            confirmation.id,
        )

        self.assertEqual(
            context["warnings"],
            [MISSING_BON_WARNING],
        )
