from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order
from portal.services.factory_portal_service import (
    build_factory_list_context, factory_combined_status, get_workflow_item, next_action_label,
)
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class FactoryListUITests(TestCase):
    def setUp(self):
        language = translation.override("zh-hans")
        language.__enter__()
        self.addCleanup(language.__exit__, None, None, None)
        self.user=get_user_model().objects.create_user("factory-ui-staff",is_staff=True)
        self.client.force_login(self.user)
        self.north=Factory.objects.create(name="North Medical Science and Technology Supplies")
        self.other=Factory.objects.create(name="Other Supplies")
        for bon in ("143211","145342","156264"):
            Order.objects.create(bon_de_commande=bon,created_by=self.user,hospital_name="Other")
        self.order=Order.objects.create(bon_de_commande="150222",created_by=self.user,hospital_name="UI Hospital")
        self.pending=self.create(status="not_started")
        self.legacy_pending=self.create(status="pending")
        self.processing=self.create(status="processing")
        self.failed=self.create(status="failed")
        self.unmatched=self.create(matched=False)
        self.missing_workflow=self.create(batch=True)
        self.missing_batch=self.create()
        self.entered=self.create(batch=True,workflow=True)
        self.tracked=self.create(batch=True,workflow=True,tracking="1Z999AA10123456784",factory=self.other)
        self.unmatched_pending=self.create(status="not_started",matched=False,factory=self.other)
        self.expected={"all":10,"pending":4,"needs_action":5,"workflow":2}

    def create(self, *, status="success",matched=True,batch=False,workflow=False,tracking="",factory=None):
        confirmation=FactoryConfirmation.objects.create(
            order=self.order if matched else None, factory=factory or self.north,
            created_by=self.user,extraction_status=status,
            extracted_confirmation_data={"django":{"detected_bon_de_commande":"150222"}} if not matched else {},
        )
        if batch:
            shipment=ShipmentBatch.objects.create(
                order=self.order,factory_confirmation=confirmation,
                batch_number=confirmation.pk,batch_date=date(2026,9,28),month_key="2026-09",tracking_number=tracking,
            )
            if workflow:
                DocumentWorkflowItem.objects.create(order=self.order,shipment_batch=shipment)
        return confirmation

    def context(self, **params):
        request=RequestFactory().get(reverse("portal:factory_list"),params)
        request.user=self.user
        return build_factory_list_context(request)

    def response(self, **params):
        return self.client.get(reverse("portal:factory_list"),params)

    def test_global_database_kpi_counts(self):
        context=self.context()
        self.assertEqual(context["stats"],self.expected)
        self.assertEqual(context["total_count"],FactoryConfirmation.objects.count())
        self.assertEqual([card["count"] for card in context["cards"]],[10,4,5,2])

    def test_pending_states_exclude_failed(self):
        ids={row["id"] for row in self.context(status="pending")["rows"]}
        self.assertEqual(ids,{self.pending.pk,self.legacy_pending.pk,self.processing.pk,self.unmatched_pending.pk})
        self.assertNotIn(self.failed.pk,ids)

    def test_needs_action_includes_failure_unmatched_and_missing_batch_workflow(self):
        self.assertEqual({row["id"] for row in self.context(status="needs_action")["rows"]},{
            self.failed.pk,self.unmatched.pk,self.missing_workflow.pk,self.missing_batch.pk,self.unmatched_pending.pk,
        })

    def test_workflow_filter_and_kpi_use_exact_batch_relation(self):
        # Legacy fallback sees the same order's other workflow. The KPI/filter must not.
        confirmation=FactoryConfirmation.objects.get(pk=self.missing_workflow.pk)
        self.assertIsNotNone(get_workflow_item(confirmation))
        context=self.context(status="workflow")
        self.assertEqual({row["id"] for row in context["rows"]},{self.entered.pk,self.tracked.pk})
        self.assertEqual(context["stats"]["workflow"],2)
        self.assertNotIn(self.missing_workflow.pk,{row["id"] for row in context["rows"]})

    def test_pending_and_unmatched_facts_can_overlap_without_losing_either(self):
        row=next(row for row in self.context()["rows"] if row["id"]==self.unmatched_pending.pk)
        self.assertEqual(row["row_status"],frozenset({"pending","needs_action"}))

    def test_search_bon_and_unmatched_detected_bon(self):
        context=self.context(q="150222")
        self.assertEqual(len(context["rows"]),10)
        self.assertIn(self.unmatched.pk,{row["id"] for row in context["rows"]})
        self.assertIsNone(next(row["bon_ordinal"] for row in context["rows"] if row["id"]==self.unmatched.pk))

    def test_search_factory_name_case_insensitive_and_partial(self):
        context=self.context(q="  NORTH MED  ")
        self.assertEqual(context["query"],"NORTH MED")
        self.assertEqual(len(context["rows"]),8)
        self.assertTrue(all(row["factory_name"]==self.north.name for row in context["rows"]))

    def test_status_and_search_combine(self):
        context=self.context(status="workflow",q="north")
        self.assertEqual([row["id"] for row in context["rows"]],[self.entered.pk])
        self.assertEqual(context["stats"],self.expected)

    def test_counts_remain_global_when_search_has_no_results(self):
        context=self.context(q="no-match",status="workflow")
        self.assertEqual(context["rows"],[])
        self.assertEqual(context["stats"],self.expected)

    def test_cards_keep_query_and_search_keeps_status(self):
        query="North & Medical"
        context=self.context(status="workflow",q=query)
        for card in context["cards"]:
            params=parse_qs(urlsplit(card["url"]).query)
            self.assertEqual(params["q"],[query])
            self.assertIn(params["status"][0],("all","pending","needs_action","workflow"))
        response=self.response(status="workflow",q="north")
        self.assertContains(response,'name="status" value="workflow"')
        self.assertContains(response,'href="?status=workflow"')
        self.assertEqual(self.response(status="workflow").context["stats"],self.expected)

    def test_unknown_status_safely_defaults_to_all(self):
        context=self.context(status="invalid")
        self.assertEqual(context["status_filter"],"all")
        self.assertEqual(len(context["rows"]),10)

    def test_kpi_search_header_and_standalone_table_structure(self):
        response=self.response()
        self.assertContains(response,'class="stat-card',count=4)
        self.assertContains(response,'class="portal-list-filters portal-panel factory-filters"')
        self.assertContains(response,'id="factory-search"')
        self.assertContains(response,'placeholder="输入订单号或工厂名称"')
        self.assertNotContains(response,"采购文件列表")
        self.assertNotContains(response,'class="panel-header"')
        self.assertNotContains(response,'<span>/</span>')
        self.assertContains(response,'<section class="portal-table-wrap factory-table-wrap">')
        self.assertEqual(response.content.decode().count("<th"),9) # includes <thead>
        self.assertContains(response,"BON 序号")

    def test_header_has_upload_home_and_secondary_admin_actions(self):
        response=self.response()
        self.assertContains(response,'class="portal-header-actions"')
        self.assertContains(response,f'href="{reverse("portal:factory_upload")}"')
        self.assertContains(response,f'<a class="portal-button" href="{reverse("portal:home")}">返回首页</a>',html=True)
        self.assertContains(response,'<a class="portal-button" href="/admin/factory_confirmations/factoryconfirmation/">Admin</a>',html=True)

    def test_tracking_actions_and_strict_workflow_links_preserved(self):
        context=self.context()
        rows={row["id"]:row for row in context["rows"]}
        for confirmation in (self.entered,self.tracked):
            batch=ShipmentBatch.objects.get(factory_confirmation=confirmation)
            item=DocumentWorkflowItem.objects.get(shipment_batch=batch)
            row=rows[confirmation.pk]
            self.assertEqual(row["bon_ordinal"],3)
            self.assertEqual(row["tracking_url"],reverse("portal:shipment_tracking_edit",args=[batch.pk]))
            self.assertEqual(row["workflow_detail_url"],reverse("portal:workflow_detail",args=[item.pk]))
            self.assertContains(self.response(),f'href="{row["workflow_detail_url"]}"')
        self.assertIsNone(rows[self.missing_workflow.pk]["workflow_detail_url"])
        response=self.response()
        for text in ("填写快递单号","修改快递单号","1Z999AA10123456784"):
            self.assertContains(response,text)

    def test_long_factory_name_has_complete_tooltip(self):
        self.assertContains(self.response(),f'title="{self.north.name}"')

    def test_factory_sort_stays_created_at_then_id_desc(self):
        future=timezone.now()+timedelta(days=30)
        FactoryConfirmation.objects.filter(pk=self.pending.pk).update(created_at=future)
        ids=[row["id"] for row in self.context()["rows"]]
        self.assertEqual(ids[0],self.pending.pk)
        self.assertEqual(ids[1:],list(FactoryConfirmation.objects.exclude(pk=self.pending.pk).order_by("-created_at","-id").values_list("pk",flat=True)))

    def test_serial_aggregation_preserves_distinct_nonempty_product_count(self):
        for index, code in enumerate(("A","A","A","","B")):
            SerialItem.objects.create(factory_confirmation=self.entered,order=self.order,product_code=code,serial_number=f"S{index}")
        row=next(row for row in self.context()["rows"] if row["id"]==self.entered.pk)
        self.assertEqual((row["product_count"],row["serial_count"]),(2,5))
        missing=next(row for row in self.context()["rows"] if row["id"]==self.pending.pk)
        self.assertEqual((missing["product_count"],missing["serial_count"]),(0,0))

    def test_list_queries_do_not_grow_with_rows_or_missing_workflows(self):
        with CaptureQueriesContext(connection) as small:
            self.context()
        for _ in range(20):
            self.create(batch=True,workflow=False)
        with CaptureQueriesContext(connection) as large:
            self.context()
        self.assertEqual(len(small),3)
        self.assertEqual(len(large),3)

    def test_row_status_text_and_legacy_detail_helpers_remain_compatible(self):
        self.assertEqual(factory_combined_status(self.failed),("提取失败","danger"))
        self.assertEqual(factory_combined_status(self.unmatched),("待匹配","warning"))
        self.assertEqual(factory_combined_status(self.entered),("已就绪","success"))
        confirmation=FactoryConfirmation.objects.get(pk=self.missing_batch.pk)
        self.assertEqual(next_action_label(confirmation),("查看工作流","success"))
        row=next(row for row in self.context()["rows"] if row["id"]==confirmation.pk)
        self.assertEqual(row["next_text"],"同步后续流程")

    def test_empty_filtered_table_keeps_eight_column_span(self):
        response=self.response(q="nothing-at-all")
        self.assertContains(response,'colspan="8"')
        self.assertContains(response,"没有找到符合条件的工厂采购文件。")

    def test_new_ui_three_languages_and_business_data_preserved(self):
        for language,title,all_label,search in (
            ("zh-hans","工厂采购","全部采购文件","输入订单号或工厂名称"),
            ("en","Factory Purchasing","All Purchase Files","Enter an order number or factory name"),
            ("fr","Achats fournisseurs","Tous les documents d’achat","Saisir un numéro de commande ou un nom d’usine"),
        ):
            with self.subTest(language=language):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME]=language
                response=self.response()
                self.assertContains(response,all_label)
                self.assertContains(response,search)
                self.assertContains(response,self.north.name)
                self.assertContains(response,"1Z999AA10123456784")
                if language!="zh-hans":
                    self.assertNotRegex(response.content.decode().replace("中文",""),r"[\u4e00-\u9fff]")

    def test_list_get_and_filters_do_not_write_business_data(self):
        models=(FactoryConfirmation,ShipmentBatch,DocumentWorkflowItem,SerialItem,Order)
        before={model:list(model.objects.order_by("pk").values()) for model in models}
        self.response(status="workflow",q="150222")
        for model in models:
            self.assertEqual(before[model],list(model.objects.order_by("pk").values()))

