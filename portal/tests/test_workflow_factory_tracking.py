from datetime import date, datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from factory_confirmations.models import FactoryConfirmation
from orders.models import Order
from portal.forms.shipment_tracking_forms import ShipmentTrackingNumberForm
from portal.services.common import get_global_numeric_bon_ordinals
from portal.services.factory_portal_service import (
    build_factory_list_context, get_batch_workflow_item, get_workflow_item,
)
from portal.services.order_portal_service import build_order_list_context
from portal.services.settlement_portal_service import build_order_batch_amount_rows
from portal.services.workflow_portal_service import build_workflow_list_context
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem
from workflow.tests import test_normal_document_generation as generation_tests


class WorkflowFactoryTrackingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tracking-staff", is_staff=True)
        for bon in ("143211", "145342", "156264"):
            Order.objects.create(bon_de_commande=bon, hospital_name="Other hospital", created_by=cls.user)
        cls.order = Order.objects.create(bon_de_commande="150222", hospital_name="Tracking Hospital", created_by=cls.user)
        cls.confirmations = []
        cls.batches = []
        cls.items = []
        for number, day in ((1, date(2026,9,28)), (2, date(2026,9,29)), (3, None)):
            confirmation = FactoryConfirmation.objects.create(
                order=cls.order, created_by=cls.user, extraction_status="success", shipping_date=day,
            )
            batch = ShipmentBatch.objects.create(
                order=cls.order, factory_confirmation=confirmation, batch_number=number,
                batch_date=day, month_key="2026-09",
            )
            item = DocumentWorkflowItem.objects.create(
                order=cls.order, shipment_batch=batch, validation_data={"batch_number": number},
            )
            cls.confirmations.append(confirmation)
            cls.batches.append(batch)
            cls.items.append(item)

    def setUp(self):
        self.client.force_login(self.user)

    def request(self, name, **params):
        request = RequestFactory().get(reverse("portal:" + name), params)
        request.user = self.user
        return request

    def workflow(self, **params):
        return build_workflow_list_context(self.request("workflow_list", **params))

    def factory(self):
        return build_factory_list_context(self.request("factory_list"))

    def tracking_url(self, index=0):
        return reverse("portal:shipment_tracking_edit", args=[self.batches[index].pk])

    def row_for(self, confirmation):
        return next(row for row in self.factory()["rows"] if row["id"] == confirmation.pk)

    def test_all_workflow_batches_share_global_bon_ordinal(self):
        self.assertEqual([row["bon_ordinal"] for row in self.workflow()["rows"]], [3,3,3])
        response = self.client.get(reverse("portal:workflow_list"))
        self.assertContains(response, '<td class="bon-ordinal-cell"><span class="sub-text">3</span></td>', count=3, html=True)
        for number in (1,2,3):
            self.assertContains(response, f"批次 {number}")

    def test_all_factory_confirmations_share_global_bon_ordinal(self):
        self.assertEqual([row["bon_ordinal"] for row in self.factory()["rows"]], [3,3,3])
        response = self.client.get(reverse("portal:factory_list"))
        self.assertContains(response, '<td class="bon-ordinal-cell"><span class="factory-sub-text">3</span></td>', count=3, html=True)

    def test_non_numeric_orders_remain_unranked_on_both_lists(self):
        self.order.bon_de_commande = "UPLOAD-123"
        self.order.save(update_fields=["bon_de_commande"])
        self.assertTrue(all(row["bon_ordinal"] is None for row in self.workflow()["rows"]))
        self.assertTrue(all(row["bon_ordinal"] is None for row in self.factory()["rows"]))
        for name in ("workflow_list", "factory_list"):
            response = self.client.get(reverse("portal:" + name))
            self.assertContains(response, "—")

    def test_unmatched_detected_bon_does_not_receive_order_ordinal_or_tracking(self):
        confirmation = FactoryConfirmation.objects.create(
            created_by=self.user, extraction_status="success",
            extracted_confirmation_data={"django":{"detected_bon_de_commande":"150222"}},
        )
        row = self.row_for(confirmation)
        self.assertEqual(row["order_number"], "150222")
        self.assertIsNone(row["bon_ordinal"])
        self.assertIsNone(row["tracking_url"])
        self.assertIsNone(row["workflow_detail_url"])
        self.assertEqual(str(row["next_text"]), "人工确认订单")

    def test_all_four_pages_share_same_global_ordinals(self):
        expected = get_global_numeric_bon_ordinals()[self.order.pk]
        order_rows = build_order_list_context(self.request("order_list"))["rows"]
        self.assertEqual(next(row["bon_ordinal"] for row in order_rows if row["id"] == self.order.pk), expected)
        for rows in (self.workflow()["rows"], self.factory()["rows"],
                     [row for row in build_order_batch_amount_rows() if row["bon_ordinal"] is not None]):
            self.assertTrue(rows)
            self.assertTrue(all(row["bon_ordinal"] == expected for row in rows))

    def test_workflow_search_and_status_filter_retain_ordinal_and_stats(self):
        DocumentWorkflowItem.objects.filter(pk=self.items[1].pk).update(validation_status="ready")
        DocumentWorkflowItem.objects.filter(pk=self.items[2].pk).update(
            validation_status="ready", invoice_status="generated", po_status="generated",
        )
        for status in ("pending", "docs", "done"):
            context = self.workflow(q="150222", status=status)
            self.assertEqual(len(context["rows"]), 1)
            self.assertEqual(context["rows"][0]["bon_ordinal"], 3)
            self.assertEqual(context["stats"], {"all":3,"pending":1,"docs":1,"done":1})

    def test_workflow_date_uses_batch_date_not_updated_at(self):
        event = timezone.make_aware(datetime(2040,2,3,4,5))
        DocumentWorkflowItem.objects.update(updated_at=event)
        response = self.client.get(reverse("portal:workflow_list"))
        self.assertContains(response, '<span class="sub-text">2026-09-28</span>', html=True)
        self.assertNotContains(response, "2040-02-03")
        self.assertNotContains(response, "更新时间")
        rows = {row["id"]:row for row in self.workflow()["rows"]}
        self.assertEqual(rows[self.items[0].pk]["shipping_date"], date(2026,9,28))
        self.assertIsNone(rows[self.items[2].pk]["shipping_date"])

    def test_null_shipping_date_displays_dash(self):
        ShipmentBatch.objects.update(batch_date=None)
        response = self.client.get(reverse("portal:workflow_list"))
        self.assertContains(response, '<span class="sub-text">—</span>', count=3, html=True)

    def test_order_by_shipment_date_descending_with_null_last(self):
        DocumentWorkflowItem.objects.filter(pk=self.items[2].pk).update(updated_at=timezone.now())
        self.assertEqual([row["id"] for row in self.workflow()["rows"]], [
            self.items[1].pk, self.items[0].pk, self.items[2].pk,
        ])

    def test_same_day_order_by_batch_number_then_id_descending(self):
        ShipmentBatch.objects.update(batch_date=date(2026,9,28))
        other_batch = ShipmentBatch.objects.create(
            order=self.order, batch_number=2, batch_date=date(2026,9,28), month_key="2026-09",
        )
        other_item = DocumentWorkflowItem.objects.create(order=self.order, shipment_batch=other_batch)
        self.assertEqual([row["id"] for row in self.workflow()["rows"]], [
            self.items[2].pk, other_item.pk, self.items[1].pk, self.items[0].pk,
        ])

    def test_batch_number_display_is_canonical_even_without_validation_json(self):
        DocumentWorkflowItem.objects.filter(pk=self.items[1].pk).update(validation_data={"batch_number":99})
        self.assertEqual(next(row["batch_number"] for row in self.workflow()["rows"] if row["id"] == self.items[1].pk), 2)

    def test_tracking_field_default_and_model_definition(self):
        field = ShipmentBatch._meta.get_field("tracking_number")
        self.assertEqual(field.max_length,200)
        self.assertTrue(field.blank)
        self.assertEqual(field.default,"")
        self.assertEqual(self.batches[0].tracking_number,"")

    def test_tracking_model_can_save_modify_and_clear(self):
        batch = self.batches[0]
        for value in ("1Z999AA10123456784", "ABC-123 456 / REF", ""):
            batch.tracking_number=value
            batch.save(update_fields=["tracking_number"])
            batch.refresh_from_db()
            self.assertEqual(batch.tracking_number,value)

    def test_staff_get_tracking_page_and_prefilled_edit_title(self):
        response = self.client.get(self.tracking_url())
        self.assertEqual(response.status_code,200)
        for text in ("填写快递单号","150222","Tracking Hospital","2026-09-28"):
            self.assertContains(response,text)
        ShipmentBatch.objects.filter(pk=self.batches[0].pk).update(tracking_number="PRE-FILLED")
        response = self.client.get(self.tracking_url())
        self.assertContains(response,"修改快递单号")
        self.assertContains(response,'value="PRE-FILLED"')

    def test_post_strips_value_and_redirects_to_factory_with_message(self):
        response = self.client.post(self.tracking_url(),{"tracking_number":"  1Z999AA10123456784  "})
        self.assertRedirects(response,reverse("portal:factory_list"))
        self.batches[0].refresh_from_db()
        self.assertEqual(self.batches[0].tracking_number,"1Z999AA10123456784")
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)],["快递单号已保存。"])

    def test_batch_two_edit_does_not_change_batch_one(self):
        response = self.client.post(self.tracking_url(1),{"tracking_number":"B2-only"})
        self.assertEqual(response.status_code,302)
        for batch in self.batches:
            batch.refresh_from_db()
        self.assertEqual([batch.tracking_number for batch in self.batches],["","B2-only",""])

    def test_post_modify_and_clear(self):
        for value in ("FIRST","SECOND",""):
            self.assertEqual(self.client.post(self.tracking_url(),{"tracking_number":value}).status_code,302)
            self.batches[0].refresh_from_db()
            self.assertEqual(self.batches[0].tracking_number,value)

    def test_max_length_validation_rejects_overlong_value(self):
        response = self.client.post(self.tracking_url(),{"tracking_number":"x"*201})
        self.assertEqual(response.status_code,400)
        self.batches[0].refresh_from_db()
        self.assertEqual(self.batches[0].tracking_number,"")
        self.assertTrue(response.context["form"].errors)

    def test_tracking_form_exposes_only_tracking_number(self):
        form=ShipmentTrackingNumberForm(data={"tracking_number":"  A-B 123 / X  ","status":"complete"},instance=self.batches[0])
        self.assertEqual(list(form.fields),["tracking_number"])
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["tracking_number"],"A-B 123 / X")

    def test_safe_next_cancel_and_post_redirect(self):
        target=reverse("portal:factory_list")+"?q=150222"
        response=self.client.get(self.tracking_url(),{"next":target})
        self.assertEqual(response.context["back_url"],target)
        self.assertContains(response,f'href="{target}"')
        response=self.client.post(self.tracking_url(),{"tracking_number":"ABC","next":target})
        self.assertEqual(response.url,target)

    def test_external_redirects_rejected_on_get_and_post(self):
        for target in ("https://evil.example/","//evil.example/","http://testserver.evil.example/"):
            response=self.client.get(self.tracking_url(),{"next":target})
            self.assertEqual(response.context["back_url"],reverse("portal:factory_list"))
            response=self.client.post(self.tracking_url(),{"tracking_number":"ABC","next":target})
            self.assertEqual(response.url,reverse("portal:factory_list"))

    def test_unknown_batch_returns_404(self):
        url=reverse("portal:shipment_tracking_edit",args=[999999])
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(url,{"tracking_number":"X"}).status_code,404)

    def test_anonymous_and_nonstaff_cannot_get_or_save(self):
        self.client.logout()
        for method in (self.client.get,self.client.post):
            self.assertEqual(method(self.tracking_url()).status_code,302)
        nonstaff=get_user_model().objects.create_user("tracking-nonstaff")
        self.client.force_login(nonstaff)
        self.assertEqual(self.client.get(self.tracking_url()).status_code,302)
        self.assertEqual(self.client.post(self.tracking_url(),{"tracking_number":"X"}).status_code,302)
        self.batches[0].refresh_from_db()
        self.assertEqual(self.batches[0].tracking_number,"")

    def test_csrf_required_for_tracking_save(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.tracking_url(),{"tracking_number":"X"}).status_code,403)

    def test_get_is_read_only_and_only_get_post_allowed(self):
        self.assertEqual(self.client.put(self.tracking_url()).status_code,405)
        before=list(ShipmentBatch.objects.values())
        self.client.get(self.tracking_url())
        self.assertEqual(before,list(ShipmentBatch.objects.values()))

    def test_factory_add_tracking_action_and_real_batch_workflow_link(self):
        for index, confirmation in enumerate(self.confirmations):
            row=self.row_for(confirmation)
            self.assertEqual(row["tracking_url"],self.tracking_url(index))
            self.assertEqual(str(row["tracking_label"]),"填写快递单号")
            self.assertEqual(row["workflow_detail_url"],reverse("portal:workflow_detail",args=[self.items[index].pk]))
            self.assertFalse(row["show_next_hint"])
        response=self.client.get(reverse("portal:factory_list"))
        self.assertContains(response,"填写快递单号",count=3)
        for item in self.items:
            self.assertContains(response,f'href="{reverse("portal:workflow_detail",args=[item.pk])}"')

    def test_factory_edit_tracking_label_and_raw_value(self):
        ShipmentBatch.objects.filter(pk=self.batches[0].pk).update(tracking_number="1Z999AA10123456784")
        row=self.row_for(self.confirmations[0])
        self.assertEqual(str(row["tracking_label"]),"修改快递单号")
        response=self.client.get(reverse("portal:factory_list"))
        self.assertContains(response,"修改快递单号")
        self.assertContains(response,"1Z999AA10123456784")

    def test_missing_batch_workflow_never_links_other_batch(self):
        self.items[1].delete()
        confirmation=FactoryConfirmation.objects.get(pk=self.confirmations[1].pk)
        self.assertIsNone(get_batch_workflow_item(confirmation))
        self.assertEqual(get_workflow_item(confirmation).pk,self.items[2].pk)
        row=self.row_for(confirmation)
        self.assertIsNone(row["workflow_detail_url"])
        self.assertEqual(row["tracking_url"],self.tracking_url(1))
        self.assertEqual(str(row["next_text"]),"同步后续流程")
        self.assertTrue(row["show_next_hint"])

    def test_no_batch_preserves_sync_hint_without_tracking_or_wrong_link(self):
        confirmation=FactoryConfirmation.objects.create(order=self.order,created_by=self.user,extraction_status="success")
        row=self.row_for(confirmation)
        self.assertIsNone(row["tracking_url"])
        self.assertIsNone(row["workflow_detail_url"])
        self.assertEqual(str(row["next_text"]),"同步后续流程")

    def test_extraction_failed_without_batch_preserves_check_error(self):
        confirmation=FactoryConfirmation.objects.create(order=self.order,created_by=self.user,extraction_status="failed")
        row=self.row_for(confirmation)
        self.assertEqual(str(row["next_text"]),"检查错误")
        self.assertIsNone(row["tracking_url"])
        self.assertTrue(row["show_next_hint"])

    def test_pending_extraction_hint_preserved(self):
        confirmation=FactoryConfirmation.objects.create(order=self.order,created_by=self.user)
        self.assertEqual(str(self.row_for(confirmation)["next_text"]),"执行提取")

    def test_tracking_edit_changes_no_downstream_or_batch_history_fields(self):
        batch_before=ShipmentBatch.objects.values().get(pk=self.batches[0].pk)
        orders_before=list(Order.objects.values())
        workflows_before=list(DocumentWorkflowItem.objects.values())
        confirmations_before=list(FactoryConfirmation.objects.values())
        self.client.post(self.tracking_url(),{"tracking_number":"TRACK","status":"complete","batch_date":"2040-01-01"})
        batch_after=ShipmentBatch.objects.values().get(pk=self.batches[0].pk)
        batch_before["tracking_number"]="TRACK"
        self.assertEqual(batch_before,batch_after)
        self.assertEqual(orders_before,list(Order.objects.values()))
        self.assertEqual(workflows_before,list(DocumentWorkflowItem.objects.values()))
        self.assertEqual(confirmations_before,list(FactoryConfirmation.objects.values()))

    def test_strict_batch_workflows_load_in_one_related_query(self):
        with self.assertNumQueries(1):
            confirmations=list(FactoryConfirmation.objects.select_related("shipment_batch__document_workflow_item"))
            self.assertEqual([get_batch_workflow_item(item).pk for item in confirmations],[
                self.items[2].pk,self.items[1].pk,self.items[0].pk,
            ])

    def test_each_list_calls_shared_ordinal_helper_once(self):
        for module, builder in (
            ("portal.services.factory_portal_service",self.factory),
            ("portal.services.workflow_portal_service",self.workflow),
        ):
            with patch(module+".get_global_numeric_bon_ordinals",wraps=get_global_numeric_bon_ordinals) as helper:
                builder()
            helper.assert_called_once()

    def test_three_languages_translate_lists_tracking_form_and_links(self):
        ShipmentBatch.objects.filter(pk=self.batches[0].pk).update(tracking_number="Business-RAW-123")
        for code, add, edit, label in (
            ("zh-hans","填写快递单号","修改快递单号","快递单号"),
            ("en","Add Tracking Number","Edit Tracking Number","Tracking Number"),
            ("fr","Ajouter le numéro de suivi","Modifier le numéro de suivi","Numéro de suivi"),
        ):
            with self.subTest(code=code):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME]=code
                factory=self.client.get(reverse("portal:factory_list"))
                self.assertContains(factory,add)
                self.assertContains(factory,edit)
                self.assertContains(factory,"Business-RAW-123")
                page=self.client.get(self.tracking_url())
                self.assertContains(page,label)
                self.assertContains(page,edit)
                workflow=self.client.get(reverse("portal:workflow_list"))
                with translation.override(code):
                    for response in (factory,workflow):
                        self.assertContains(response,translation.gettext("BON 序号"))
                        self.assertContains(response,translation.gettext("发货日期"))
                    self.assertContains(factory,translation.gettext("查看工作流"))
                if code!="zh-hans":
                    for response in (factory,workflow,page):
                        self.assertNotRegex(response.content.decode().replace("中文",""),r"[\u4e00-\u9fff]")

    def test_empty_table_colspans(self):
        DocumentWorkflowItem.objects.all().delete()
        FactoryConfirmation.objects.all().delete()
        self.assertContains(self.client.get(reverse("portal:workflow_list")),'colspan="9"')
        self.assertContains(self.client.get(reverse("portal:factory_list")),'colspan="8"')


class TrackingDocumentGenerationRegressionTests(generation_tests.NormalDocumentGenerationTransactionTests):
    """Run the existing Invoice/PO scenarios with a recorded tracking number."""

    def setUp(self):
        super().setUp()
        self.batch.tracking_number="1Z999AA10123456784"
        self.batch.save(update_fields=["tracking_number"])

