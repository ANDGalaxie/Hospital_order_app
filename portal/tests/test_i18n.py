"""Portal language state, UI coverage and document/data isolation contracts."""
from datetime import date
from decimal import Decimal
from html import unescape
from pathlib import Path
import gettext
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.template.loader import get_template
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import translation

from documents.models import GeneratedDocument
from documents.services.document_generation_service import render_invoice_html, render_po_html
from documents.services.factory_order_request_service import render_html as render_request_html
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation
from hospitals.models import Hospital
from orders.models import Order, OrderItem
from portal.forms.hospital_library_forms import HospitalPortalForm
from portal.i18n import display_validation_message
from portal.services.common import get_portal_lang
from portal.services.order_portal_service import get_order_next_action, order_combined_status
from products.models import Product
from settlements.forms import PaymentEntryForm
from settlements.models import SettlementAccount
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class PortalI18nTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('portal-i18n', 'i18n@example.com', 'test')
        cls.factory = Factory.objects.create(name='Sino Medical Sciences Technology Inc')
        cls.hospital = Hospital.objects.create(
            name='Clinique Louis Pasteur', billing_address='1 rue de Paris',
            default_shipping_address='2 rue de Paris', notes='人工 notes 原样保留',
        )
        cls.product = Product.objects.create(code='BMA-2.5010', description='原始产品描述', factory=cls.factory)
        cls.order = Order.objects.create(
            bon_de_commande='156264', hospital=cls.hospital, hospital_name=cls.hospital.name,
            factory=cls.factory, created_by=cls.user, order_date=date(2026, 9, 9),
            hospital_order_pdf='hospital_orders/i18n.pdf',
            extraction_status='success', extracted_order_data={'raw': 'OCR 原文'},
            document_validation_status='validated',
            document_validation_data={'source': 'portal_order_basic_validation', 'errors': [], 'warnings': []},
            shipping_address_data={'lines': ['2 rue de Paris']},
            billing_address_data={'lines': ['1 rue de Paris']},
        )
        cls.item = OrderItem.objects.create(
            order=cls.order, product=cls.product, product_code=cls.product.code,
            description=cls.product.description, requested_quantity=2,
            hospital_unit_price=Decimal('720.00'), is_manually_confirmed=True,
            product_match_status='manually_confirmed',
        )
        cls.batch = ShipmentBatch.objects.create(order=cls.order, batch_number=1, month_key='2026-09', batch_date=date(2026, 9, 9), source_type='manual')
        cls.confirmation = FactoryConfirmation.objects.create(
            order=cls.order, factory=cls.factory, confirmation_pdf='factory_confirmations/i18n.pdf',
            created_by=cls.user, extracted_confirmation_data={'django': {}, 'warnings': []},
        )
        cls.workflow = DocumentWorkflowItem.objects.create(order=cls.order, shipment_batch=cls.batch)
        cls.documents = []
        for kind, number, key in [
            ('hospital_invoice', 'Invoice 20260409', 'invoice_data'),
            ('factory_po', 'DELAHK0409S', 'po_data'),
        ]:
            cls.documents.append(GeneratedDocument.objects.create(
                order=cls.order, shipment_batch=cls.batch, document_type=kind,
                document_number=number, generated_by=cls.user,
                source_data={key: {'totals': {'total_raw': '1440.00'}}},
            ))

        cls.account, _ = SettlementAccount.objects.get_or_create(
            document=cls.documents[0], defaults={
                'direction': 'receivable', 'counterparty_name': cls.hospital.name,
                'issue_date': date(2026, 9, 9), 'original_amount': Decimal('1440.00'),
                'currency': 'EUR',
            },
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.addCleanup(translation.deactivate)

    def switch(self, language, path=None):
        return self.client.post(reverse('set_language'), {
            'language': language, 'next': path or reverse('portal:home'),
        })

    def test_default_is_chinese_even_with_browser_preference_and_old_session(self):
        session = self.client.session
        session['portal_lang'] = 'fr'
        session.save()
        response = self.client.get('/portal/?lang=en', HTTP_ACCEPT_LANGUAGE='en-US,en;q=0.9')
        self.assertContains(response, '<h1>工作台</h1>', html=True)
        self.assertEqual(response.headers['Content-Language'], 'zh-hans')

    def test_home_language_persists_and_html_and_active_button_agree(self):
        for code, title, nav, html_lang in [
            ('zh-hans', '工作台', '医院订单', 'zh-CN'),
            ('en', 'Dashboard', 'Hospital Orders', 'en'),
            ('fr', 'Tableau de bord', 'Commandes hospitalières', 'fr'),
        ]:
            with self.subTest(language=code):
                switched = self.switch(code)
                self.assertEqual(switched.status_code, 302)
                self.assertEqual(switched.cookies[settings.LANGUAGE_COOKIE_NAME].value, code)
                for _ in range(2):
                    response = self.client.get('/portal/')
                    self.assertContains(response, f'<h1>{title}</h1>', html=True)
                    self.assertContains(response, nav)
                    self.assertContains(response, f'<html lang="{html_lang}">')
                    self.assertContains(response, 'class="lang-switch-trigger" type="button" aria-expanded="false"')
                    self.assertContains(response, f'value="{code}" class="lang-switch-option is-active" aria-current="true"')
                    for inactive_code in {'zh-hans', 'en', 'fr'} - {code}:
                        self.assertContains(response, f'value="{inactive_code}" class="lang-switch-option"')
                        self.assertNotContains(response, f'value="{inactive_code}" class="lang-switch-option is-active"')

    def test_switch_preserves_detail_and_query_parameters(self):
        for path in [
            reverse('portal:order_detail', args=[self.order.pk]),
            '/portal/settlements/comparison/?q=150222&page=2',
            '/portal/orders/?q=156264&status=ready&page=2',
        ]:
            for code in ['en', 'fr']:
                with self.subTest(path=path, language=code):
                    self.assertEqual(self.switch(code, path).url, path)
                    response = self.client.get(path)
                    self.assertIn(f'name="next" value="{path}"', unescape(response.content.decode()))
                    self.assertContains(response, f'value="{code}" class="lang-switch-option is-active" aria-current="true"')

    def test_external_and_protocol_relative_redirects_are_rejected(self):
        for path in ['https://evil.example/steal', '//evil.example/steal', 'http://testserver.evil.example/']:
            self.assertEqual(self.switch('fr', path).url, '/')

    def test_switch_requires_csrf_and_post_to_change_language(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('set_language'), {'language': 'fr'}).status_code, 403)
        response = client.get(reverse('set_language'), {'language': 'fr', 'next': '/portal/'})
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)
        page = client.get('/portal/')
        token = client.cookies['csrftoken'].value
        response = client.post(reverse('set_language'), {'language': 'fr', 'next': '/portal/', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, 'fr')

    def test_helper_only_reads_active_language(self):
        request = RequestFactory().get('/portal/?lang=fr')
        request.session = {'portal_lang': 'fr'}
        for language, legacy in [('zh','zh'), ('zh-cn','zh'), ('zh-hans','zh'), ('zh-hant','zh'), ('en-gb','en'), ('fr-ca','fr')]:
            with translation.override(language):
                self.assertEqual(get_portal_lang(request), legacy)
        self.assertEqual(request.session, {'portal_lang': 'fr'})

    def test_representative_pages_have_translated_ui_without_chinese_leaks(self):
        routes = [
            ('home', []), ('order_list', []), ('order_detail', [self.order.pk]),
            ('factory_list', []), ('factory_detail', [self.confirmation.pk]),
            ('workflow_list', []), ('workflow_detail', [self.workflow.pk]),
            ('library_home', []), ('library_products', []), ('library_hospitals', []),
            ('library_factories', []), ('library_prices', []),
            ('library_hospital_add', []), ('library_factory_add', []),
            ('library_hospital_detail', [self.hospital.pk]),
            ('library_factory_detail', [self.factory.pk]),
            ('library_product_detail', [self.product.pk]),
            ('library_price_policy_add', []), ('library_price_policy_simulator', []),
            ('document_detail', [self.documents[0].pk]),
            ('settlement_account_detail', [self.account.pk]),
            ('shipment_list', []), ('shipment_detail', [self.batch.pk]),
            ('backorder_list', []), ('document_center', []), ('document_list', []),
            ('settlement_home', []), ('settlement_comparison', []),
            ('settlement_receivables', []), ('settlement_payables', []), ('settlement_transactions', []),
        ]
        for language in ['en','fr']:
            self.switch(language)
            for name, args in routes:
                with self.subTest(language=language, page=name):
                    response = self.client.get(reverse('portal:'+name, args=args))
                    self.assertEqual(response.status_code, 200)
                    body = re.sub(r'<!--.*?-->', '', response.content.decode(), flags=re.S)
                    for retained in ['中文', '原始产品描述', '人工 notes 原样保留', 'OCR 原文']:
                        body = body.replace(retained, '')
                    self.assertEqual(re.findall(r'[^<>\n]*[\u4e00-\u9fff][^<>\n]*', body), [])
            response = self.client.get('/portal/finance/')
            self.assertEqual(response.status_code, 200)
            self.assertNotRegex(response.content.decode().replace('中文',''), r'[\u4e00-\u9fff]')

    def test_dynamic_order_labels_are_translated_without_changing_categories(self):
        for code, waiting, review in [
            ('zh-hans','等待工厂确认','产品编码待核对'),
            ('en','Waiting for factory confirmation','Product codes to verify'),
            ('fr','En attente de confirmation de l’usine','Références produits à vérifier'),
        ]:
            with translation.override(code):
                self.assertEqual(get_order_next_action(self.order, 0)[0], waiting)
                self.item.is_manually_confirmed = False
                self.item.product_match_status = 'needs_review'
                self.item.save()
                label, css, category = order_combined_status(self.order)
                self.assertEqual(label, review)
                self.assertEqual(category, 'product_review')
                self.item.is_manually_confirmed = True
                self.item.product_match_status = 'manually_confirmed'
                self.item.save()

    def test_messages_follow_active_language(self):
        for language in ['en', 'fr']:
            self.switch(language)
            # Exercise real warning and success message paths without OCR.
            response = self.client.post(reverse('portal:order_upload'), {})
            messages = [str(message) for message in get_messages(response.wsgi_request)]
            expected = 'Select a hospital order PDF file.' if language == 'en' else 'Sélectionnez un fichier PDF de commande hospitalière.'
            self.assertIn(expected, messages)
            response = self.client.post(reverse('portal:library_hospital_toggle_active', args=[self.hospital.pk]))
            messages = ' '.join(str(message) for message in get_messages(response.wsgi_request))
            self.assertIn(self.hospital.name, messages)
            self.assertNotRegex(messages, r'[\u4e00-\u9fff]')

    def test_forms_and_choices_follow_active_language(self):
        for code, label, method in [('en','Official hospital name','Bank transfer'), ('fr','Nom officiel de l’hôpital','Virement bancaire')]:
            with translation.override(code):
                self.assertEqual(str(HospitalPortalForm().fields['name'].label), label)
                form = PaymentEntryForm(data={'amount':'-1'})
                self.assertIn(method, str(form['method']))
                self.assertFalse(form.is_valid())
                self.assertNotRegex(str(form.errors), r'[\u4e00-\u9fff]')

    def test_known_validation_messages_translate_without_translating_raw_text(self):
        original = '产品 BMA-2.5010 缺少有效医院单价。'
        with translation.override('fr'):
            self.assertEqual(display_validation_message(original), 'Le produit BMA-2.5010 n’a pas de prix unitaire hospitalier valide.')
            raw = 'OCR 原文：产品无法识别'
            self.assertEqual(display_validation_message(raw), raw)
        self.assertEqual(original, '产品 BMA-2.5010 缺少有效医院单价。')

    def test_business_values_statuses_and_amount_format_are_language_independent(self):
        before = Order.objects.values().get(pk=self.order.pk)
        source = [doc.source_data for doc in self.documents]
        for language in ['zh-hans', 'en', 'fr']:
            self.switch(language)
            response = self.client.get(reverse('portal:order_detail', args=[self.order.pk]))
            for value in ['156264', self.hospital.name, self.factory.name, 'BMA-2.5010', '原始产品描述', '2 rue de Paris']:
                self.assertContains(response, value)
            comparison = self.client.get(reverse('portal:settlement_comparison'))
            for value in ['156264', 'Invoice 20260409', 'DELAHK0409S', '€1,440.00']:
                self.assertContains(comparison, value)
            self.assertEqual(Order.objects.values().get(pk=self.order.pk), before)
            for doc, expected in zip(self.documents, source):
                doc.refresh_from_db()
                self.assertEqual(doc.source_data, expected)

    def test_all_portal_templates_compile(self):
        for path in (settings.BASE_DIR/'portal/templates').rglob('*.html'):
            get_template(str(path.relative_to(settings.BASE_DIR/'portal/templates')))

    def test_catalogs_are_compiled_and_have_no_untranslated_chinese_entries(self):
        for code in ['en','fr']:
            with (settings.BASE_DIR/f'locale/{code}/LC_MESSAGES/django.mo').open('rb') as handle:
                catalog = gettext.GNUTranslations(handle)
            for msgid, value in catalog._catalog.items():
                if isinstance(msgid,str) and re.search(r'[\u4e00-\u9fff]',msgid):
                    self.assertTrue(value)
                    self.assertNotRegex(value, r'[\u4e00-\u9fff]')

    def test_formal_documents_render_identically_in_all_portal_languages(self):
        common = {
            'company': {'po_company': {'address': []}, 'bank': {}},
            'factory': {'factory_address': []}, 'shipping_address': [],
            'addresses': {'shipping_address': [], 'invoice_address': []},
            'items': [], 'serial_items': [],
            'totals': {'total':'1,440.00 €', 'total_units':'2'},
            'po': {'po_number':'DELAHK0409S', 'source':'BON DE COMMANDE N° 156264'},
            'invoice': {'invoice_number':'Invoice 20260409'},
            'document': {'document_number':'Factory Order Request 156264'},
        }
        results=[]
        for code in ['zh-hans','en','fr']:
            with translation.override(code):
                po=render_po_html(common, settings.BASE_DIR/'templates/factory_purchase_order.html')
                invoice=render_invoice_html(common, settings.BASE_DIR/'templates','hospital_invoice.html')
                request=render_request_html(common)
                for text in ['Purchase Order', 'Supplier', 'Buyer', 'Source', 'Order Date', 'Expected Arrival', 'Total Units', 'Total']:
                    self.assertIn(text,po)
                for text in ['Invoice Date','Shipping Address','Total','Serial Number']:
                    self.assertIn(text,invoice)
                results.append((po,invoice,request))
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[1],results[2])
