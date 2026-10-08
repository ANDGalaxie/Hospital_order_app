"""Real PDF integration, filter parity, SVG safety and privacy tests."""
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import fitz
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from finance.test_operating_finance import OperatingFixture
from finance.services.finance_pdf_charts import line_chart_svg
from finance.services.finance_pdf_export_service import (
    ORDER_LIMIT, RISK_LIMIT, FinancePDFError, build_operating_report_context,
    build_settlement_report_context, deny_resource_fetch, render_report_pdf,
)
from finance.services.operating_finance_service import build_operating_finance_context
from finance.services.settlement_finance_service import build_settlement_finance_dashboard_data
from finance.views import parse_finance_filters
from administrative_expenses.models import AdministrativeExpense as Expense, AdministrativeExpenseAttachment as Attachment
from documents.models import GeneratedDocument
from orders.models import Order
from settlements.models import SettlementAccount as Account, PaymentTransaction as Payment
from portal.role_access import HOSPITAL_GROUP, INTERNAL_GROUP

ACTUAL = "portal:finance:settlement_dashboard_export_pdf"
OPERATING = "portal:finance:operating_dashboard_export_pdf"
SVG = "{http://www.w3.org/2000/svg}"


class FinanceSVGTests(SimpleTestCase):
    def test_negative_zero_single_and_empty_series_are_renderable(self):
        for labels, values in [(["2026-09","2026-10"], [-10,20]), (["2026-09","2026-10"], [0,0]),
                               (["2026-09"], [5]), ([], [])]:
            svg = ET.fromstring(line_chart_svg(labels, [{"label":"Ventes €","values":values}], "Trend"))
            self.assertEqual(svg.tag, SVG+"svg")
            if labels:
                self.assertEqual(len(svg.findall(SVG+"circle")), len(labels))
                self.assertTrue(svg.findall(SVG+"polyline"))
            else:
                self.assertIn(translation.gettext("No trend data for this selection."), ET.tostring(svg, encoding="unicode"))

    def test_labels_are_escaped_not_interpreted_as_svg(self):
        label = '<image href="http://169.254.169.254/">'
        svg = ET.fromstring(line_chart_svg([label], [{"label":label, "values":[1]}], label))
        self.assertEqual(svg.findall(SVG+"image"), [])
        self.assertIn(label, "".join(svg.itertext()))

    def test_invalid_lengths_and_nonfinite_coordinates_fail(self):
        for values in ([], [float("nan")], [float("inf")]):
            with self.assertRaises(ValueError):
                line_chart_svg(["2026-09"], [{"label":"Sales","values":values}], "Trend")

    def test_fetcher_rejects_network_file_and_data_urls(self):
        for url in ("https://example.test/a", "http://169.254.169.254/", "file:///etc/passwd", "data:image/svg+xml,test"):
            with self.assertRaises(ValueError):
                deny_resource_fetch(url)

    def test_renderer_failure_is_explicit_and_invalid_pdf_rejected(self):
        with patch("weasyprint.HTML.write_pdf", side_effect=RuntimeError("test")):
            with self.assertRaises(FinancePDFError):
                render_report_pdf("finance/pdf/base.html", {"report_title":"Test"})
        with patch("weasyprint.HTML.write_pdf", return_value=b"%PDF-broken"):
            with self.assertRaises(FinancePDFError):
                render_report_pdf("finance/pdf/base.html", {"report_title":"Test"})


class FinancePDFTests(OperatingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.boss)

    def populated(self):
        invoice = self.account("1000")
        po = self.account("600", kind="factory_po")
        self.payment(invoice, "1000")
        self.payment(po, "600")
        expense = self.expense("100", paid_at=date(2026,10,3))
        self.attachments(expense)
        self.expense("999", void=True)

    def pdf(self, route, query=None):
        response = self.client.get(reverse(route), query or {})
        self.assertEqual(response.status_code,200,response.content[:500])
        self.assertEqual(response["Content-Type"],"application/pdf")
        self.assertEqual(response["Cache-Control"],"private, no-store")
        self.assertEqual(response["X-Content-Type-Options"],"nosniff")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertTrue(response.content.startswith(b"%PDF-"))
        document = fitz.open(stream=response.content,filetype="pdf")
        self.addCleanup(document.close)
        self.assertGreater(document.page_count,0)
        self.assertAlmostEqual(document[0].rect.width,841.89,delta=1)
        self.assertAlmostEqual(document[0].rect.height,595.28,delta=1)
        return response,document," ".join(page.get_text() for page in document)

    def test_actual_pdf_has_kpis_trends_risks_orders_and_no_admin(self):
        self.populated()
        response,pdf,text = self.pdf(ACTUAL)
        self.assertRegex(response["Content-Disposition"],r"Acoeurs_Financial_Actuals_[0-9]{8}\.pdf")
        for label in ("财务实绩报告","医院开票销售额","预计毛利润","逾期应收","订单财务摘要","OPERATING-SYNTHETIC-001"):
            self.assertIn(label,text)
        for amount in ("1 000.00 €","600.00 €","400.00 €","40.00%"):
            self.assertIn(amount,text)
        for private in ("PRIVATE-EMPLOYEE","PRIVATE-WAGE","PRIVATE-PAYSLIP","administrative_expenses/","行政支出"):
            self.assertNotIn(private,text)
        colors = {tuple(round(c,2) for c in drawing["color"]) for page in pdf for drawing in page.get_drawings() if drawing["color"]}
        self.assertIn((0.15,0.39,0.92),colors)  # Actual SVG blue line, not a blank placeholder.
        self.assertIn((0.92,0.35,0.05),colors)

    def test_operating_pdf_has_month_cross_month_cash_and_aggregate_privacy(self):
        self.populated()
        response,pdf,text = self.pdf(OPERATING,{"month":"2026-10"})
        self.assertIn("Acoeurs_Operating_Analysis_2026-10.pdf",response["Content-Disposition"])
        for label in ("公司经营分析报告","内部管理报告","2025-11","2026-10","员工工资","员工福利","交通补助","差旅","财务口径说明"):
            self.assertIn(label,text)
        for amount in ("1 000.00 €","600.00 €","100.00 €","300.00 €"):
            self.assertIn(amount,text)
        self.assertNotIn("999.00 €",text)
        self.assertIn("—",text)
        for private in ("PRIVATE-EMPLOYEE","PRIVATE-WAGE","PRIVATE-PAYSLIP","administrative_expenses/","/portal/files/"):
            self.assertNotIn(private,text)
        chart_pages = [page for page in pdf if "趋势" in page.get_text()]
        self.assertGreaterEqual(len(chart_pages),2)
        self.assertGreater(sum(len(page.get_drawings()) for page in chart_pages),50)

    def test_report_context_matches_existing_service_kpis_and_filters(self):
        self.populated()
        from django.test import RequestFactory
        request = RequestFactory().get("/",{"date_from":"2026-09-01","date_to":"2026-09-30",
            "hospital":"Synthetic","factory":"","order":"OPERATING","status":"paid"})
        state = parse_finance_filters(request)
        expected = build_settlement_finance_dashboard_data(reporting_currency="EUR",**state["parsed_filters"])
        report = build_settlement_report_context(filter_values=state["filter_values"],parsed_filters=state["parsed_filters"])
        self.assertEqual([card["value"] for card in report["kpi_cards"]],[card["value"] for card in expected["kpi_cards"]])
        expected = build_operating_finance_context(month="2026-10")
        report = build_operating_report_context(month="2026-10")
        self.assertEqual(report["month"],expected["month"])
        self.assertEqual([card["value"] for group in report["kpi_groups"] for card in group["cards"]],
                         [card["value"] for group in expected["kpi_groups"] for card in group["cards"]])
        self.assertEqual([row["month"] for row in report["charts"][0]["rows"]],[row["month"] for row in expected["trend_rows"]])
        self.assertNotIn("url",json.dumps(report,default=str))

    def test_empty_reports_and_negative_operating_balance_render(self):
        _,_,text = self.pdf(ACTUAL)
        self.assertIn("暂无趋势数据",text)
        _,_,text = self.pdf(OPERATING,{"month":"2026-09"})
        self.assertIn("0.00 €",text)
        self.assertIn("—",text)
        self.expense("50")
        _,_,text = self.pdf(OPERATING,{"month":"2026-09"})
        self.assertIn("-50.00 €",text)

    def test_chinese_english_french_pdf_fonts_and_euro(self):
        self.account("1234.56")
        for code,title in (("zh-hans","财务实绩报告"),("en","Financial Actuals Report"),("fr","Rapport financier")):
            self.client.cookies["django_language"] = code
            _,document,text = self.pdf(ACTUAL)
            self.assertIn(title,text)
            self.assertIn("1 234.56 €",text)
            self.assertNotIn("\ufffd",text)
            self.assertTrue(any(page.get_fonts() for page in document))
        self.client.cookies["django_language"] = "fr"
        _,_,text = self.pdf(OPERATING,{"month":"2026-09"})
        self.assertIn("Analyse de l’activité de l’entreprise",text)
        self.assertIn("Confidentiel",text)

    def test_invalid_month_empty_duplicate_and_boundaries_do_not_render(self):
        for suffix in ("?month=","?month=bad","?month=2026-13","?month=2026-9","?month=0001-01",
                       "?month=9999-12","?month=2026-09&month=2026-10"):
            with patch("finance.services.finance_pdf_export_service.render_report_pdf") as render:
                self.assertEqual(self.client.get(reverse(OPERATING)+suffix).status_code,400)
                render.assert_not_called()

    def test_invalid_financial_filters_do_not_render(self):
        for query in ({"date_from":"bad"},{"date_from":"2026-02-30"},{"date_from":"2026-10-01","date_to":"2026-09-01"},{"status":"bad"}):
            with patch("finance.views.build_settlement_pdf") as render:
                self.assertEqual(self.client.get(reverse(ACTUAL),query).status_code,400)
                render.assert_not_called()
        self.assertEqual(self.client.get(reverse(ACTUAL)+"?order=A&order=B").status_code,400)

    def test_all_financial_filters_and_month_pass_unchanged(self):
        query={"date_from":"2026-09-01","date_to":"2026-09-30","hospital":"Synthetic",
               "factory":"Factory","order":"OPERATING","status":"paid"}
        with patch("finance.views.build_settlement_pdf",return_value={"content":b"%PDF-test","filename":"test.pdf"}) as build:
            self.assertEqual(self.client.get(reverse(ACTUAL),query).status_code,200)
            self.assertEqual(build.call_args.kwargs["filter_values"],query)
            parsed=build.call_args.kwargs["parsed_filters"]
            self.assertEqual(parsed["date_from"],date(2026,9,1))
            self.assertEqual(parsed["hospital_query"],"Synthetic")
            self.assertEqual(parsed["factory_query"],"Factory")
        with patch("finance.views.build_operating_pdf",return_value={"content":b"%PDF-test","filename":"test.pdf"}) as build:
            self.assertEqual(self.client.get(reverse(OPERATING),{"month":"2026-09"}).status_code,200)
            build.assert_called_once_with(month="2026-09")

    def test_buttons_retain_every_filter_and_month_and_keep_xlsx(self):
        query={"date_from":"2026-09-01","date_to":"2026-09-30","hospital":"Synthetic",
               "factory":"Factory","order":"OPERATING","status":"paid"}
        from urllib.parse import urlsplit,parse_qs
        response=self.client.get("/portal/finance/",query)
        soup=BeautifulSoup(response.content,"html.parser")
        link=soup.select_one('a[href^="/portal/finance/export.pdf"]')
        self.assertEqual(parse_qs(urlsplit(link["href"]).query),{key:[value] for key,value in query.items()})
        self.assertIsNotNone(soup.select_one('a[href^="export.xlsx"]'))
        response=self.client.get("/portal/finance/operations/",{"month":"2026-09"})
        self.assertContains(response,reverse(OPERATING)+"?month=2026-09")
        response=self.client.get("/portal/finance/operations/",{"month":"bad"})
        self.assertNotContains(response,reverse(OPERATING),status_code=400)

    def test_download_get_does_not_write_or_create_files(self):
        self.populated()
        models=[Order,GeneratedDocument,Account,Payment,Expense,Attachment]
        before={model:list(model.objects.order_by("pk").values()) for model in models}
        with CaptureQueriesContext(connection) as queries:
            self.pdf(OPERATING,{"month":"2026-10"})
            self.pdf(ACTUAL)
        self.assertEqual([query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT","UPDATE","DELETE"))],[])
        self.assertEqual(before,{model:list(model.objects.order_by("pk").values()) for model in models})
        self.assertEqual(list(Path(self.media.name).rglob("*")),[])

    def test_renderer_errors_return_safe_503_not_broken_pdf(self):
        for route,builder in ((ACTUAL,"build_settlement_pdf"),(OPERATING,"build_operating_pdf")):
            with patch("finance.views."+builder,side_effect=FinancePDFError("PRIVATE-PATH")):
                response=self.client.get(reverse(route),{"month":"2026-09"})
                self.assertEqual(response.status_code,503)
                self.assertEqual(response["Cache-Control"],"private, no-store")
                self.assertNotIn(b"PRIVATE-PATH",response.content)
                self.assertNotEqual(response["Content-Type"],"application/pdf")

    def test_filter_changes_report_and_preserves_original_cash_scope(self):
        self.populated()
        other=self.account("222",issued=date(2026,10,1))
        _,_,text=self.pdf(ACTUAL,{"date_from":"2026-09-01","date_to":"2026-09-30","order":"OPERATING"})
        self.assertIn("1 000.00 €",text)
        self.assertNotIn("1 222.00 €",text)
        # Account selection uses September issue dates, but its posted October receipts remain included.
        self.assertIn("2026-10",text)
        _,_,text=self.pdf(ACTUAL,{"date_from":"2026-10-01","date_to":"2026-10-31"})
        self.assertIn("222.00 €",text)
        self.assertNotIn("1 000.00 €",text)

    def test_risk_lists_use_existing_overdue_and_thirty_day_rules(self):
        with patch("django.utils.timezone.localdate",return_value=date(2026,10,8)):
            for due in (date(2026,9,10),date(2026,10,20),date(2026,11,25)):
                account=self.account("100")
                Account.objects.filter(pk=account.pk).update(due_date=due)
            report=build_settlement_report_context(filter_values={},parsed_filters={})
        self.assertEqual(len(report["risks"][0]["rows"]),1)
        self.assertEqual(len(report["risks"][1]["rows"]),1)
        self.assertEqual(report["risks"][0]["rows"][0]["due_date"],date(2026,9,10))

    def test_order_and_risk_limits_are_explicit_without_limiting_kpis(self):
        for index in range(ORDER_LIMIT+5):
            self.order=Order.objects.create(bon_de_commande=f"LONG-{index:03d}",hospital_name="Synthetic hospital",
                hospital_order_pdf="hospital_orders/fixture.pdf",created_by=self.boss)
            account=self.account("10")
            Account.objects.filter(pk=account.pk).update(due_date=date(2026,9,10))
        with patch("django.utils.timezone.localdate",return_value=date(2026,10,8)):
            report=build_settlement_report_context(filter_values={},parsed_filters={})
            _,document,text=self.pdf(ACTUAL)
        self.assertEqual(report["order_count"],ORDER_LIMIT+5)
        self.assertEqual(len(report["order_rows"]),ORDER_LIMIT)
        self.assertIn(str(ORDER_LIMIT+5),report["order_scope"])
        self.assertEqual(len(report["risks"][0]["rows"]),RISK_LIMIT)
        self.assertIn("1 050.00 €",text)
        self.assertIn("LONG-104",text)
        self.assertNotIn("LONG-000",text[text.index("订单财务摘要"):])
        self.assertGreater(document.page_count,5)
        self.assertGreater(sum("订单号" in page.get_text() for page in document),2)

    def test_long_document_references_are_visibly_abbreviated(self):
        self.account("10")
        data=build_settlement_finance_dashboard_data()
        data["order_rows"][0]["invoice_number_display"]="INV-"+("long-reference-"*30)
        with patch("finance.services.finance_pdf_export_service.build_settlement_finance_dashboard_data",return_value=data):
            report=build_settlement_report_context(filter_values={},parsed_filters={})
        self.assertTrue(report["order_rows"][0]["invoice_number_display"].endswith("[...]"))
        self.assertEqual(data["order_rows"][0]["invoice_number_display"],"INV-"+("long-reference-"*30))


class FinancePDFPermissionTests(OperatingFixture,TestCase):
    def setUp(self):
        super().setUp()
        self.staff=get_user_model().objects.create_user(username="pdf-staff",is_staff=True)
        self.cynthia=get_user_model().objects.create_user(username="Cynthia",is_staff=True)
        self.cynthia.groups.add(Group.objects.get_or_create(name=INTERNAL_GROUP)[0])
        self.claire=get_user_model().objects.create_user(username="Claire")
        self.claire.groups.add(Group.objects.get_or_create(name=HOSPITAL_GROUP)[0])
        self.admin=get_user_model().objects.create_user(username="OtherAdmin",is_staff=True,is_superuser=True)
        self.similar=get_user_model().objects.create_user(username="acoeurs",is_staff=True,is_superuser=True)

    def test_actual_pdf_follows_existing_staff_page_permissions(self):
        for user in (None,self.staff,self.cynthia,self.claire,self.admin,self.similar):
            client=Client()
            if user:
                client.force_login(user)
            page=client.get("/portal/finance/")
            with patch("finance.views.build_settlement_pdf",return_value={"content":b"%PDF-test","filename":"test.pdf"}) as build:
                response=client.get(reverse(ACTUAL))
                self.assertEqual(response.status_code,page.status_code)
                if page.status_code!=200:
                    build.assert_not_called()

    def test_operating_pdf_exact_boss_only_before_rendering(self):
        for user in (None,self.staff,self.cynthia,self.claire,self.admin,self.similar):
            client=Client()
            if user:
                client.force_login(user)
            for query in ({},{"month":"2026-09"},{"month":"bad"}):
                with patch("finance.views.build_operating_pdf") as build:
                    self.assertEqual(client.get(reverse(OPERATING),query).status_code,403)
                    build.assert_not_called()
        self.client.force_login(self.boss)
        with patch("finance.views.build_operating_pdf",return_value={"content":b"%PDF-test","filename":"test.pdf"}) as build:
            self.assertEqual(self.client.get(reverse(OPERATING),{"month":"2026-09"}).status_code,200)
            build.assert_called_once()

    def test_inactive_and_demo_accounts_cannot_export(self):
        self.boss.is_active=False
        self.boss.save(update_fields=["is_active"])
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(reverse(OPERATING)).status_code,403)
        self.assertEqual(self.client.get(reverse(ACTUAL)).status_code,302)
        self.boss.is_active=True
        self.boss.save(update_fields=["is_active"])
        self.boss.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(self.boss)
        for route in (ACTUAL,OPERATING):
            self.assertEqual(self.client.get(reverse(route)).status_code,403)

    def test_exports_are_get_or_head_only(self):
        self.client.force_login(self.boss)
        for route,builder in ((ACTUAL,"build_settlement_pdf"),(OPERATING,"build_operating_pdf")):
            with patch("finance.views."+builder,return_value={"content":b"%PDF-test","filename":"test.pdf"}) as build:
                self.assertEqual(self.client.head(reverse(route)).status_code,200)
                build.assert_called_once()
            self.assertEqual(self.client.post(reverse(route)).status_code,405)
