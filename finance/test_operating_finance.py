"""Isolated operating-analysis statistics and permission regressions."""
import json
import tempfile
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from administrative_expenses.categories import CATEGORIES, SUBCATEGORIES
from administrative_expenses.models import AdministrativeExpense as Expense, AdministrativeExpenseAttachment as Attachment
from documents.models import GeneratedDocument
from finance.services.operating_finance_service import build_operating_finance_context as build_context, operating_period
from finance.services.settlement_finance_service import build_settlement_finance_dashboard_data
from orders.models import Order
from settlements.models import SettlementAccount as Account, PaymentTransaction as Payment
from portal.role_access import HOSPITAL_GROUP, INTERNAL_GROUP

OPERATING = "portal:finance:operating_dashboard"


class OperatingPeriodTests(SimpleTestCase):
    def test_default_and_twelve_month_year_boundary(self):
        with patch("administrative_expenses.services.timezone.localdate", return_value=date(2026, 1, 7)):
            start, end, first, months = operating_period()
        self.assertEqual((start, end, first), (date(2026, 1, 1), date(2026, 2, 1), date(2025, 2, 1)))
        self.assertEqual(len(months), 12)
        self.assertEqual(months, sorted(months))

    def test_invalid_months_are_validation_errors(self):
        for month in ("", "bad", "2026-13", "2026-00", "2026-9", "2026-09-01", " 2026-09", "2026-09 ", "0000-01", "0001-01", "9999-12"):
            with self.subTest(month=month), self.assertRaises(ValidationError):
                operating_period(month)


class OperatingFixture:
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory(prefix="operating-finance-test-")
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.boss = get_user_model().objects.create_user(username="Acoeurs", is_staff=True)
        self.order = Order.objects.create(bon_de_commande="OPERATING-SYNTHETIC-001", hospital_name="Synthetic hospital",
                                         hospital_order_pdf="hospital_orders/synthetic.pdf", created_by=self.boss)
        self.serial = 0

    def account(self, amount, kind="hospital_invoice", issued=date(2026, 9, 1), currency="EUR"):
        self.serial += 1
        document = GeneratedDocument.objects.create(order=self.order, document_type=kind,
            document_number=f"OPERATING-{self.serial}", generated_by=self.boss,
            source_data={"totals": {"amount": "99999999.99"}})
        account = Account(document=document, direction="receivable" if kind == "hospital_invoice" else "payable",
                          issue_date=issued, original_amount=Decimal(amount), currency=currency)
        if kind not in {"hospital_invoice", "factory_po"}:
            Account.objects.bulk_create([account])  # Synthetic malformed legacy account.
        else:
            account.save()
        return account

    def payment(self, account, amount, when=date(2026, 10, 3), reversed=False):
        payment = Payment.objects.create(account=account, amount=Decimal(amount), payment_date=when, created_by=self.boss)
        if reversed:
            payment.reverse(user=self.boss, reason="Synthetic reversal")
        return payment

    def expense(self, amount, when=date(2026, 9, 1), category="salary", subcategory="", paid_at=None, void=False):
        return Expense.objects.create(category=category, subcategory=subcategory, expense_date=when,
            employee_or_payee="PRIVATE-EMPLOYEE-SENTINEL", description="PRIVATE-WAGE-SENTINEL",
            amount=Decimal(amount), payment_status="paid" if paid_at else "pending", paid_at=paid_at,
            is_void=void, created_by=self.boss, updated_by=self.boss)

    def attachments(self, expense):
        for index in range(3):
            Attachment.objects.create(expense=expense, file=f"administrative_expenses/{expense.pk}/{index + 1:032x}.pdf",
                original_filename=f"PRIVATE-PAYSLIP-{index}.pdf", document_type="payslip",
                file_size=123, uploaded_by=self.boss)


class OperatingStatisticsTests(OperatingFixture, TestCase):
    def test_frozen_official_amounts_and_profit(self):
        self.account("1000.00")
        self.account("600.00", kind="factory_po")
        self.expense("100.00")
        summary = build_context(month="2026-09")["summary"]
        for field, expected in {"invoiced_sales": "1000", "factory_purchases": "600", "gross_profit": "400",
                "gross_margin": "0.4", "administrative_expenses_total": "100",
                "estimated_operating_balance": "300", "estimated_operating_margin": "0.3"}.items():
            self.assertEqual(summary[field], Decimal(expected), field)

    def test_cross_month_document_receipts_and_administrative_payments(self):
        self.payment(self.account("1000.00"), "1000.00")
        self.payment(self.account("600.00", kind="factory_po"), "600.00")
        self.expense("100.00", paid_at=date(2026, 10, 3))
        september = build_context(month="2026-09")["summary"]
        october = build_context(month="2026-10")
        self.assertEqual(september["estimated_operating_balance"], Decimal("300.00"))
        for field in ("hospital_receipts", "factory_payments", "paid_administrative_expenses"):
            self.assertEqual(september[field], Decimal("0.00"))
        for field, expected in {"administrative_expenses_total": "0", "estimated_operating_balance": "0",
                "hospital_receipts": "1000", "factory_payments": "600", "paid_administrative_expenses": "100",
                "estimated_net_cash_inflow": "300"}.items():
            self.assertEqual(october["summary"][field], Decimal(expected), field)
        rows = {row["month"]: row for row in october["trend_rows"]}
        self.assertEqual(rows["2026-09"]["administrative_expenses_total"], Decimal("100"))
        self.assertEqual(rows["2026-10"]["administrative_expenses_total"], Decimal("0"))
        self.assertEqual(rows["2026-10"]["estimated_net_cash_inflow"], Decimal("300"))

    def test_payments_from_accounts_outside_issue_date_window(self):
        self.payment(self.account("40", issued=date(2020, 1, 1)), "40")
        self.payment(self.account("60", issued=date(2026, 11, 1)), "60")
        summary = build_context(month="2026-10")["summary"]
        self.assertEqual(summary["invoiced_sales"], Decimal("0"))
        self.assertEqual(summary["hospital_receipts"], Decimal("100"))

    def test_commercial_other_document_types_and_mismatched_directions_excluded(self):
        self.account("1000")
        for kind in ("commercial_po", "factory_order_request", "factory_confirmation"):
            account = self.account("999", kind=kind, issued=date(2026, 10, 1))
            Payment.objects.bulk_create([Payment(account=account, amount=Decimal("10"),
                payment_date=date(2026, 10, 3), created_by=self.boss)])
        for kind, wrong_direction in (("hospital_invoice", "payable"), ("factory_po", "receivable")):
            account = self.account("50", kind=kind)
            self.payment(account, "50")
            Account.objects.filter(pk=account.pk).update(direction=wrong_direction)
        september = build_context(month="2026-09")["summary"]
        october = build_context(month="2026-10")["summary"]
        self.assertEqual(september["invoiced_sales"], Decimal("1000"))
        self.assertEqual(september["factory_purchases"], Decimal("0"))
        for field in ("invoiced_sales", "factory_purchases", "hospital_receipts", "factory_payments"):
            self.assertEqual(october[field], Decimal("0"))

    def test_cancelled_and_non_eur_accounts_and_payments_excluded(self):
        for kind in ("hospital_invoice", "factory_po"):
            cancelled = self.account("100", kind=kind)
            self.payment(cancelled, "30")
            Account.objects.filter(pk=cancelled.pk).update(status="cancelled")
            self.payment(self.account("200", kind=kind, currency="USD"), "40")
        self.account("500")
        self.account("250", kind="factory_po")
        self.assertEqual(build_context(month="2026-09")["summary"]["gross_profit"], Decimal("250"))
        october = build_context(month="2026-10")["summary"]
        self.assertEqual(october["hospital_receipts"], Decimal("0"))
        self.assertEqual(october["factory_payments"], Decimal("0"))

    def test_reversed_transactions_excluded(self):
        invoice, po = self.account("1000"), self.account("600", kind="factory_po")
        self.payment(invoice, "200", reversed=True)
        self.payment(po, "100", reversed=True)
        self.payment(invoice, "150")
        self.payment(po, "50")
        summary = build_context(month="2026-10")["summary"]
        self.assertEqual(summary["hospital_receipts"], Decimal("150"))
        self.assertEqual(summary["factory_payments"], Decimal("50"))
        self.assertEqual(summary["estimated_net_cash_inflow"], Decimal("100"))

    def test_pending_expense_is_cost_but_not_cash(self):
        self.expense("25")
        summary = build_context(month="2026-09")["summary"]
        self.assertEqual(summary["administrative_expenses_total"], Decimal("25"))
        self.assertEqual(summary["paid_administrative_expenses"], Decimal("0"))
        self.assertEqual(summary["estimated_operating_balance"], Decimal("-25"))

    def test_many_attachments_do_not_multiply_cost_or_cash(self):
        expense = self.expense("25", paid_at=date(2026, 9, 2))
        self.attachments(expense)
        summary = build_context(month="2026-09")["summary"]
        self.assertEqual(summary["administrative_expenses_total"], Decimal("25"))
        self.assertEqual(summary["paid_administrative_expenses"], Decimal("25"))

    def test_voided_expenses_excluded_from_every_month_and_category(self):
        self.expense("999", paid_at=date(2026, 10, 3), void=True)
        context = build_context(month="2026-10")
        for row in context["trend_rows"]:
            self.assertEqual(row["administrative_expenses_total"], Decimal("0"))
            self.assertEqual(row["paid_administrative_expenses"], Decimal("0"))
        self.assertTrue(all(row["amount"] == Decimal("0") for row in context["categories"]))

    def test_category_subcategory_totals_shares_and_month_links(self):
        for category in CATEGORIES:
            for subcategory in SUBCATEGORIES.get(category, {"": ""}):
                self.expense("10", category=category, subcategory=subcategory)
        context = build_context(month="2026-09")
        self.assertEqual(context["summary"]["administrative_expenses_total"], Decimal("120"))
        rows = {row["code"]: row for row in context["categories"]}
        self.assertEqual(rows["benefits"]["amount"], Decimal("60"))
        self.assertEqual(rows["reimbursement"]["amount"], Decimal("30"))
        self.assertEqual(rows["benefits"]["share"], Decimal("0.5"))
        for category, row in rows.items():
            self.assertEqual(row["url"], reverse("portal:administrative_expenses:category", args=[category]) + "?month=2026-09")
            for child in row["children"]:
                self.assertEqual(child["amount"], Decimal("10"))
                self.assertEqual(child["url"], reverse("portal:administrative_expenses:list", args=[category, child["code"]]) + "?month=2026-09")

    def test_decimal_cents_all_the_way_to_chart_serialization(self):
        self.account("0.30")
        self.account("0.10", kind="factory_po")
        self.expense("0.01")
        context = build_context(month="2026-09")
        self.assertEqual(context["summary"]["gross_profit"], Decimal("0.20"))
        self.assertEqual(context["summary"]["estimated_operating_balance"], Decimal("0.19"))
        self.assertTrue(all(isinstance(value, Decimal) for value in context["summary"].values()))
        for chart in context["chart_data"].values():
            self.assertTrue(all(isinstance(value, Decimal) for series in chart["series"] for value in series["values"]))

    def test_zero_sales_empty_categories_and_negative_balance(self):
        empty = build_context(month="2026-09")
        self.assertIsNone(empty["summary"]["estimated_operating_margin"])
        self.assertIsNone(empty["summary"]["gross_margin"])
        cards = {card["key"]: card for group in empty["kpi_groups"] for card in group["cards"]}
        self.assertEqual(cards["estimated_operating_margin"]["value"], "—")
        self.assertTrue(all(row["amount"] == Decimal("0") for row in empty["categories"]))
        self.account("10", kind="factory_po")
        self.expense("5")
        summary = build_context(month="2026-09")["summary"]
        self.assertEqual(summary["estimated_operating_balance"], Decimal("-15"))
        self.assertIsNone(summary["estimated_operating_margin"])
        self.account("5")
        self.assertEqual(build_context(month="2026-09")["summary"]["estimated_operating_margin"], Decimal("-2"))

    def test_twelve_months_zero_filled_with_exclusive_boundaries(self):
        for issued, amount in ((date(2025, 10, 31), "10"), (date(2025, 11, 1), "20"),
                               (date(2026, 10, 31), "30"), (date(2026, 11, 1), "40")):
            self.account(amount, issued=issued)
        self.expense("1", when=date(2025, 11, 1))
        self.expense("2", when=date(2026, 11, 1))
        context = build_context(month="2026-10")
        rows = context["trend_rows"]
        expected = ["2025-11", "2025-12"] + [f"2026-{index:02d}" for index in range(1, 11)]
        self.assertEqual([row["month"] for row in rows], expected)
        self.assertEqual(rows[0]["invoiced_sales"], Decimal("20"))
        self.assertEqual(rows[-1]["invoiced_sales"], Decimal("30"))
        self.assertTrue(all(row["invoiced_sales"] == Decimal("0") for row in rows[1:-1]))
        self.assertEqual(context["chart_data"]["cash"]["labels"], expected)
        self.assertEqual(context["summary"]["administrative_expenses_total"], Decimal("0"))

    def test_cash_date_boundaries_and_expenses_older_than_trend_window(self):
        account = self.account("1000", issued=date(2020, 1, 1))
        for when, amount in ((date(2025, 10, 31), "10"), (date(2025, 11, 1), "20"),
                             (date(2026, 10, 31), "30"), (date(2026, 11, 1), "40")):
            self.payment(account, amount, when=when)
            self.expense(amount, when=date(2025, 1, 1), paid_at=when)
        context = build_context(month="2026-10")
        self.assertEqual(context["summary"]["hospital_receipts"], Decimal("30"))
        self.assertEqual(context["summary"]["paid_administrative_expenses"], Decimal("30"))
        self.assertEqual(context["trend_rows"][0]["hospital_receipts"], Decimal("20"))
        self.assertEqual(context["trend_rows"][0]["paid_administrative_expenses"], Decimal("20"))

    def test_four_read_queries_without_employee_or_attachment_fields(self):
        self.expense("123.45")
        with CaptureQueriesContext(connection) as queries:
            context = build_context(month="2026-09")
        self.assertEqual(len(queries), 4)
        self.assertEqual(context["summary"]["administrative_expenses_total"], Decimal("123.45"))
        for query in queries:
            self.assertNotIn("employee_or_payee", query["sql"])
            self.assertNotIn("administrativeexpenseattachment", query["sql"])


class OperatingAccessTests(OperatingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse(OPERATING)
        self.staff = get_user_model().objects.create_user(username="finance-staff", is_staff=True)
        self.cynthia = get_user_model().objects.create_user(username="Cynthia", is_staff=True)
        self.claire = get_user_model().objects.create_user(username="Claire")
        self.other_admin = get_user_model().objects.create_user(username="other-superuser", is_staff=True, is_superuser=True)
        self.similar = get_user_model().objects.create_user(username="acoeurs", is_staff=True, is_superuser=True)
        self.cynthia.groups.add(Group.objects.get_or_create(name=INTERNAL_GROUP)[0])
        group = Group.objects.get_or_create(name=HOSPITAL_GROUP)[0]
        group.permissions.add(*Permission.objects.filter(content_type__app_label__in=["orders", "hospitals", "products", "commercial_pos"], codename__startswith="view"))
        self.claire.groups.add(group)
        self.account("1000")
        self.account("600", kind="factory_po")
        expense = self.expense("98765.43")
        self.attachments(expense)

    def test_boss_access_readonly_and_cache_headers(self):
        self.client.force_login(self.boss)
        response = self.client.get(self.url, {"month": "2026-09"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(sum(len(group["cards"]) for group in response.context["kpi_groups"]), 11)
        self.assertContains(response, "预计经营结余")
        self.assertEqual(self.client.head(self.url, {"month": "2026-09"}).status_code, 200)
        self.assertEqual(self.client.post(self.url, {"month": "2026-09"}).status_code, 405)

    def test_nonboss_denied_before_aggregation_for_every_query(self):
        for user in (None, self.cynthia, self.claire, self.staff, self.other_admin, self.similar):
            client = Client()
            if user:
                client.force_login(user)
            for query in ({}, {"month": "2026-09"}, {"month": "bad"}, {"month": "2026-10", "hospital": "Synthetic"}):
                with self.subTest(user=user, query=query), patch("finance.views.build_operating_finance_context") as builder:
                    self.assertEqual(client.get(self.url, query).status_code, 403)
                    self.assertEqual(client.post(self.url, query).status_code, 403)
                    builder.assert_not_called()

    def test_inactive_and_demo_boss_denied(self):
        self.boss.is_active = False
        self.boss.save(update_fields=["is_active"])
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.boss.is_active = True
        self.boss.save(update_fields=["is_active"])
        self.boss.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_entry_only_for_boss_and_original_finance_access_unchanged(self):
        for user in (self.boss, self.staff, self.cynthia, self.other_admin, self.similar):
            self.client.force_login(user)
            response = self.client.get("/portal/finance/")
            self.assertEqual(response.status_code, 200)
            if user == self.boss:
                self.assertContains(response, self.url)
            else:
                self.assertNotContains(response, self.url)
        self.client.force_login(self.claire)
        self.assertEqual(self.client.get("/portal/finance/").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/portal/finance/").status_code, 302)

    def test_bad_or_duplicate_month_returns_clear_400(self):
        self.client.force_login(self.boss)
        for month in ("", "bad", "2026-13", "2026-9", "0001-01", "9999-12"):
            with self.subTest(month=month):
                response = self.client.get(self.url, {"month": month})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context["filter_errors"])
                self.assertNotContains(response, "operating-chart-data", status_code=400)
        self.assertEqual(self.client.get(self.url + "?month=2026-09&month=2026-10").status_code, 400)

    def test_company_scope_does_not_apply_legacy_party_or_status_filters(self):
        self.client.force_login(self.boss)
        response = self.client.get(self.url, {"month": "2026-09", "hospital": "non-matching",
            "factory": "non-matching", "order": "non-matching", "status": "cancelled", "date_from": "2026-10-01"})
        summary = response.context["summary"]
        self.assertEqual(summary["invoiced_sales"], Decimal("1000"))
        self.assertEqual(summary["factory_purchases"], Decimal("600"))
        self.assertEqual(summary["administrative_expenses_total"], Decimal("98765.43"))
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual([item.get("name") for item in soup.select(".operating-dashboard input")], ["month"])

    def test_aggregate_page_and_json_have_no_payroll_names_or_attachment_links(self):
        self.client.force_login(self.boss)
        response = self.client.get(self.url, {"month": "2026-09"})
        for private in ("PRIVATE-EMPLOYEE-SENTINEL", "PRIVATE-WAGE-SENTINEL", "PRIVATE-PAYSLIP", "/attachments/", "/portal/files/"):
            self.assertNotContains(response, private)
        soup = BeautifulSoup(response.content, "html.parser")
        chart = json.loads(soup.select_one("#operating-chart-data").string)
        self.assertEqual(chart["accrual"]["series"][3]["values"][-1], "98765.43")
        self.assertEqual(chart["accrual"]["labels"][-1], "2026-09")
        self.assertTrue(all("month=2026-09" in link["href"] for link in soup.select(".operating-categories a")))
        self.assertTrue(all(form.get("method") == "get" for form in soup.select(".operating-dashboard form")))

    def test_original_kpis_rows_charts_and_html_do_not_include_admin_amounts(self):
        fixed = datetime(2026, 10, 8, tzinfo=dt_timezone.utc)
        with patch("finance.services.settlement_finance_service.timezone.now", return_value=fixed):
            before = build_settlement_finance_dashboard_data()
            self.expense("54321.09", category="premises")
            after = build_settlement_finance_dashboard_data()
        self.assertEqual(before, after)
        for user in (self.boss, self.staff, self.cynthia, self.other_admin):
            self.client.force_login(user)
            response = self.client.get("/portal/finance/")
            for field in ("administrative_expenses_total", "estimated_operating_balance", "paid_administrative_expenses"):
                self.assertNotIn(field, response.context["summary"])
                self.assertNotContains(response, field)
            for private in ("98 765.43", "54 321.09", "PRIVATE-EMPLOYEE-SENTINEL", "PRIVATE-WAGE-SENTINEL"):
                self.assertNotContains(response, private)

    def test_original_xlsx_unchanged_with_no_extra_sheets_or_private_values(self):
        from openpyxl import load_workbook
        self.client.force_login(self.staff)
        def workbook_values():
            response = self.client.get("/portal/finance/export.xlsx")
            self.assertEqual(response.status_code, 200)
            workbook = load_workbook(BytesIO(response.content), data_only=False)
            return {sheet.title: list(sheet.values) for sheet in workbook}
        fixed = datetime(2026, 10, 8, tzinfo=dt_timezone.utc)
        with patch("finance.services.settlement_finance_service.timezone.now", return_value=fixed):
            before = workbook_values()
            self.expense("54321.09", category="premises")
            after = workbook_values()
        self.assertEqual(before, after)
        self.assertEqual(list(after), ["摘要", "订单明细", "应收应付", "收付款流水"])
        for private in ("98765.43", "54321.09", "PRIVATE-EMPLOYEE", "PRIVATE-WAGE"):
            self.assertNotIn(private, str(after))

    def test_get_does_not_write_business_data(self):
        self.payment(Account.objects.filter(direction="receivable").first(), "20")
        self.client.force_login(self.boss)
        models = [Order, GeneratedDocument, Account, Payment, Expense, Attachment]
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"month": "2026-09"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))], [])
        self.assertEqual(before, {model: list(model.objects.order_by("pk").values()) for model in models})

    def test_all_three_languages_include_estimate_notes_and_entry(self):
        self.client.force_login(self.boss)
        for code, title, balance, note in [
            ("zh-hans", "公司经营分析", "预计经营结余", "会计期间调整"),
            ("en", "Operating Analysis", "Estimated operating balance", "accounting period adjustments"),
            ("fr", "Analyse de l’activité", "Solde d’activité estimé", "ajustements de période"),
        ]:
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = code
            response = self.client.get(self.url, {"month": "2026-09"})
            for label in (title, balance, note):
                self.assertContains(response, label)
            self.assertContains(self.client.get("/portal/finance/"), title)

    def test_lowest_complete_window_uses_padded_month_labels(self):
        self.client.force_login(self.boss)
        response = self.client.get(self.url, {"month": "0001-12"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["trend_start"], "0001-01")
        self.assertEqual(response.context["month"], "0001-12")

    def test_commercial_demo_page_and_json_are_unchanged_when_admin_expenses_change(self):
        demo = get_user_model().objects.create_user(username="operating-demo", is_staff=True, is_superuser=True)
        demo.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(demo)
        def rendered_finance():
            response = self.client.get("/portal/commercial/finance/")
            self.assertEqual(response.status_code, 200)
            for private in ("98 765.43", "54 321.09", "PRIVATE-EMPLOYEE", "PRIVATE-WAGE"):
                self.assertNotContains(response, private)
            soup = BeautifulSoup(response.content, "html.parser")
            return (soup.get_text(" ", strip=True),
                    [json.loads(script.string) for script in soup.select('script[type="application/json"]')])
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 8, tzinfo=dt_timezone.utc)):
            before = rendered_finance()
            self.expense("54321.09", paid_at=date(2026, 10, 3))
            after = rendered_finance()
        self.assertEqual(before, after)
        self.assertEqual(self.client.get(self.url, {"month": "2026-09"}).status_code, 403)
