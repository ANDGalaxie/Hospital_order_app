"""The same frozen Factory PO is authoritative for every non-price field."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import hashlib

from bs4 import BeautifulSoup
from django.conf import settings
from django.test import TransactionTestCase

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.factory_payload_service import (
    PRICE_FIELD_ALLOWLIST, non_price_html, non_price_payload,
)
from commercial_pos.services.generation_service import (
    build_commercial_po_data, build_commercial_snapshot, ensure_commercial_po_for_batch,
    render_commercial_files,
)
from commercial_pos.services.price_service import CommercialPOError
from commercial_pos.services.snapshot_service import validated_snapshot
from documents.models import GeneratedDocument
from documents.services.document_generation_service import render_po_html, write_po_html_and_pdf
from finance.services.finance_analysis_service import build_finance_dashboard_data
from finance.services.settlement_finance_service import build_settlement_finance_dashboard_data
from settlements.models import SettlementAccount, PaymentTransaction
from .fixtures import FixtureMixin, make_factory_po, fake_render


class FrozenFactoryCopyTests(FixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.factory_po = make_factory_po(self.user, self.batch)
        p = self.factory_po.source_data['po_data']
        p['factory'].update(factory_name='Frozen supplier', factory_address=['Frozen supplier street'], buyer='Frozen Buyer')
        p['shipping_address'] = ['Frozen destination, independent of live hospital data']
        first = p['items'][0]
        first['discount_note'] = '30% expiration discount'
        second = deepcopy(first)
        second.update(quantity_raw=75.0, quantity='75.00', batch_quantity='75',
                      discount_rate='0.00', discount_rate_raw=0.0, discount='0.00%', discount_note='',
                      factory_net_unit_price='120.00', final_unit_price_raw=120.0,
                      amount_raw=9000.0, amount='9,000.00 €', line_total='9000.00',
                      serial_numbers=['FROZEN-SECOND-ROW'], expiration_dates=['2028-01-01'])
        # Same product remains in two rows. Live shipment has only one unit.
        p['items'].append(second)
        p['totals'].update(total_units_raw=76.0, total_units='76', total_raw=9084.0, total='9,084.00 €')
        self.factory_po.save(update_fields=['source_data'])
        self.original = deepcopy(p)
        self.template = Path(settings.BASE_DIR) / 'templates/factory_purchase_order.html'
        self.original_html = render_po_html(po_data=p, template_path=self.template)
        Path(self.factory_po.html_file.path).write_text(self.original_html, encoding='utf-8')

    def snapshot(self):
        return build_commercial_snapshot(self.batch)

    def payload(self):
        return build_commercial_po_data(self.snapshot())

    def test_source_is_same_batch_formal_factory_po(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot['source_factory_po_document_id'], self.factory_po.pk)
        self.assertEqual(snapshot['source_factory_po_document_number'], self.factory_po.document_number)

    def test_missing_factory_po_fails_closed_without_order_fallback(self):
        # Move the fixture to another batch; no order-level/latest fallback.
        from factory_confirmations.models import FactoryConfirmation
        from shipments.models import ShipmentBatch
        confirmation = FactoryConfirmation.objects.create(order=self.order, factory=self.order.factory,
            shipping_date=self.batch.batch_date, created_by=self.user)
        second = ShipmentBatch.objects.create(order=self.order, factory_confirmation=confirmation,
            batch_number=2, batch_date=self.batch.batch_date, month_key='2026-04')
        self.assertTrue(GeneratedDocument.objects.filter(order=self.order, document_type='factory_po').exists())
        with self.assertRaisesMessage(CommercialPOError, 'no generated Factory PO'):
            build_commercial_snapshot(second)
        with self.assertRaises(CommercialPOError):
            ensure_commercial_po_for_batch(second.pk, self.user)
        self.assertFalse(GeneratedDocument.objects.filter(shipment_batch=second).exists())

    def test_ambiguous_same_batch_factory_po_fails_closed(self):
        GeneratedDocument.objects.create(order=self.order, shipment_batch=self.batch, document_type='factory_po',
            document_number='SECOND-FACTORY', source_data=self.factory_po.source_data, generated_by=self.user)
        with self.assertRaisesMessage(CommercialPOError, 'multiple Factory POs'):
            self.snapshot()

    def test_full_non_price_deep_equality_uses_explicit_allowlist(self):
        self.assertEqual(non_price_payload(self.payload()), non_price_payload(self.original))
        self.assertNotIn('quantity', PRICE_FIELD_ALLOWLIST['items[*]'])
        self.assertNotIn('total_units', PRICE_FIELD_ALLOWLIST['totals'])
        changed = self.payload()
        changed['items'][0]['quantity'] = '77.00'
        self.assertNotEqual(non_price_payload(changed), non_price_payload(self.original))

    def test_supplier_deep_equal(self):
        self.assertEqual(self.payload()['factory'], self.original['factory'])

    def test_buyer_equal(self):
        self.assertEqual(self.payload()['factory']['buyer'], self.original['factory']['buyer'])

    def test_shipping_address_equal(self):
        self.assertEqual(self.payload()['shipping_address'], self.original['shipping_address'])

    def test_source_equal(self):
        self.assertEqual(self.payload()['po']['source'], self.original['po']['source'])

    def test_order_date_equal_and_distinct_from_price_date(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot['shipping_date'], '2026-04-22')
        self.assertEqual(snapshot['po_data']['po']['order_date'], '13/04/2026')
        self.assertEqual(snapshot['po_data']['po'], self.original['po'])

    def test_expected_arrival_equal(self):
        self.assertEqual(self.payload()['po']['expected_arrival'], self.original['po']['expected_arrival'])

    def test_internal_number_is_independent_of_visible_po_number(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot['document_number'], 'CPO-147891-B1')
        self.assertEqual(snapshot['po_data']['po']['po_number'], self.original['po']['po_number'])
        self.assertNotEqual(snapshot['document_number'], snapshot['po_data']['po']['po_number'])

    def test_product_row_count_and_order_equal_without_merging_discount_groups(self):
        rows = self.payload()['items']
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['product_code'] for r in rows], [r['product_code'] for r in self.original['items']])
        self.assertEqual([r['quantity'] for r in rows], ['1.00', '75.00'])

    def test_descriptions_equal(self):
        self.assertEqual([r['description'] for r in self.payload()['items']], [r['description'] for r in self.original['items']])

    def test_quantities_are_frozen_factory_quantities_not_live_shipment_quantities(self):
        self.assertEqual(self.batch_item.shipped_quantity, 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot['total_units'], '76')
        self.assertEqual([r['quantity_raw'] for r in snapshot['po_data']['items']], [1.0, 75.0])

    def test_serial_expiration_metadata_deep_equal(self):
        for original, commercial in zip(self.original['items'], self.payload()['items']):
            for key in ('serial_numbers', 'expiration_dates', 'expiration_threshold_days'):
                self.assertEqual(original[key], commercial[key])

    def test_company_header_footer_registration_deep_equal(self):
        self.assertEqual(self.payload()['company'], self.original['company'])

    def test_live_non_price_changes_do_not_change_render_payload(self):
        self.order.hospital_name = 'Changed live hospital'
        self.order.shipping_address_data = {'street': 'Changed live shipping'}
        self.order.order_date = date(2025, 1, 1)
        self.order.save()
        self.order.factory.name = 'Changed live supplier'
        self.order.factory.save()
        self.order_item.product.description = 'Changed live description'
        self.order_item.product.save()
        self.assertEqual(non_price_payload(self.payload()), non_price_payload(self.original))

    def test_deepcopy_does_not_mutate_source_or_return_shared_dicts(self):
        before = GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get()
        payload = self.payload()
        payload['factory']['factory_name'] = 'Only detached clone changed'
        self.factory_po.refresh_from_db()
        self.assertEqual(self.factory_po.source_data['po_data'], self.original)
        self.assertEqual(GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get(), before)
        self.assertEqual(self.payload()['factory']['factory_name'], 'Frozen supplier')

    def test_165_before_april_20(self):
        self.batch.batch_date = self.batch.factory_confirmation.shipping_date = date(2026, 4, 19)
        self.assertEqual(self.snapshot()['unit_price'], '165.00')

    def test_160_on_april_20(self):
        self.batch.batch_date = self.batch.factory_confirmation.shipping_date = date(2026, 4, 20)
        self.assertEqual(self.snapshot()['unit_price'], '160.00')

    def test_160_on_april_22(self):
        self.assertEqual(self.snapshot()['unit_price'], '160.00')

    def test_discount_is_zero_and_net_price_is_commercial_price(self):
        for row in self.payload()['items']:
            self.assertEqual(row['discount'], '0.00%')
            self.assertEqual(Decimal(str(row['discount_rate_raw'])), 0)
            self.assertEqual(Decimal(row['factory_net_unit_price']), Decimal('160.00'))
            self.assertEqual(row['discount_note'], '0% expiration discount' if row['quantity']=='1.00' else '')

    def test_amounts_derive_only_from_original_row_quantities(self):
        for row in self.payload()['items']:
            self.assertEqual(Decimal(row['amount_raw']), Decimal(str(row['quantity_raw'])) * Decimal('160.00'))

    def test_total_units_are_identical(self):
        self.assertEqual(self.payload()['totals']['total_units'], self.original['totals']['total_units'])
        self.assertEqual(self.payload()['totals']['total_units_raw'], self.original['totals']['total_units_raw'])

    def test_total_is_sum_of_commercial_rows_and_snapshot_is_consistent(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot['total_amount'], '12160.00')
        self.assertEqual(Decimal(snapshot['po_data']['totals']['total_raw']), sum(Decimal(r['amount_raw']) for r in snapshot['po_data']['items']))
        with patch('commercial_pos.services.generation_service.render_commercial_files', side_effect=fake_render):
            document = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        self.assertEqual(validated_snapshot(document), document.source_data)

    def test_tampered_non_price_payload_is_rejected(self):
        snapshot = self.snapshot()
        snapshot['po_data']['factory']['buyer'] = 'Wrong buyer'
        with self.assertRaises(CommercialPOError):
            build_commercial_po_data(snapshot)

    def test_original_factory_invoice_finance_settlements_and_files_unchanged(self):
        invoice = GeneratedDocument.objects.create(order=self.order, shipment_batch=self.batch,
            document_type='hospital_invoice', document_number='INVOICE-COPY', generated_by=self.user,
            source_data={'invoice_data': {'totals': {'total_raw': 270}}},
            pdf_file=self.factory_po.pdf_file.name, html_file=self.factory_po.html_file.name)
        documents = list(GeneratedDocument.objects.exclude(document_type='commercial_po').order_by('pk').values())
        finance = build_finance_dashboard_data()
        sf = build_settlement_finance_dashboard_data(); sf.pop('generated_at', None)
        accounts, payments = list(SettlementAccount.objects.values()), list(PaymentTransaction.objects.values())
        paths = [Path(self.factory_po.pdf_file.path), Path(self.factory_po.html_file.path)]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with patch('commercial_pos.services.generation_service.render_commercial_files', side_effect=fake_render):
            doc = ensure_commercial_po_for_batch(self.batch.pk, self.user)
            regenerated = ensure_commercial_po_for_batch(self.batch.pk, self.user, regenerate_existing=True)
        self.assertEqual(regenerated.pk, doc.pk)
        self.assertEqual(regenerated.document_number, doc.document_number)
        self.assertEqual(list(GeneratedDocument.objects.exclude(document_type='commercial_po').order_by('pk').values()), documents)
        self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for p in paths], hashes)
        self.assertEqual(build_finance_dashboard_data(), finance)
        after_sf = build_settlement_finance_dashboard_data(); after_sf.pop('generated_at', None)
        self.assertEqual(after_sf, sf)
        self.assertEqual(list(SettlementAccount.objects.values()), accounts)
        self.assertEqual(list(PaymentTransaction.objects.values()), payments)

    def test_html_and_pdf_non_price_text_match_and_no_commercial_labels(self):
        import fitz
        # Generate the synthetic reference once; Commercial never rewrites it.
        write_po_html_and_pdf(html_content=self.original_html, html_path=Path(self.factory_po.html_file.path),
            pdf_path=Path(self.factory_po.pdf_file.path), project_root=Path(settings.BASE_DIR))
        root = Path(self.media.name) / 'copy-render'; root.mkdir()
        snapshot = self.snapshot()
        render_commercial_files(snapshot, root)
        commercial_html = (root / 'document.html').read_text()
        self.assertEqual(non_price_html(self.original_html), non_price_html(commercial_html))
        original_soup = BeautifulSoup(self.original_html, 'html.parser')
        commercial_soup = BeautifulSoup(commercial_html, 'html.parser')
        original_text = '\n'.join(p.get_text() for p in fitz.open(self.factory_po.pdf_file.path))
        commercial_text = '\n'.join(p.get_text() for p in fitz.open(root / 'document.pdf'))
        # Remove only exact price node text (and the price discount note).
        def without_prices(text, soup):
            prices = [td.get_text(strip=True) for row in soup.select('.po-items-table tbody tr') for td in row.find_all('td', recursive=False)[2:]]
            prices += [n.get_text(strip=True) for n in soup.select('.total-row.amount .total-value')]
            import re
            text = re.sub(r'\d+(?:\.\d+)?%', 'PRICE', text)
            for value in sorted(set(prices), key=len, reverse=True):
                if value: text = text.replace(value, 'PRICE')
            return text.replace('PRICE', '').split()
        self.assertEqual(without_prices(original_text, original_soup), without_prices(commercial_text, commercial_soup))
        for forbidden in ('commercial presentation only', 'commercial purchase order', 'not a factory payment',
                          'not applicable to this commercial display', 'for hospital commercial presentation only'):
            self.assertNotIn(forbidden, commercial_text.casefold())
            self.assertNotIn(forbidden, commercial_html.casefold())
        self.assertIn(self.original['po']['po_number'], commercial_text)
        self.assertNotIn('CPO-147891-B1', commercial_text)

    def test_historical_missing_non_price_fields_are_not_invented(self):
        source = self.factory_po.source_data['po_data']
        source['po'].pop('source')
        source['totals'].pop('total_units'); source['totals'].pop('total_units_raw')
        self.factory_po.save(update_fields=['source_data'])
        snapshot = self.snapshot()
        self.assertEqual(snapshot['total_units'], '76')
        self.assertNotIn('source', snapshot['po_data']['po'])
        self.assertNotIn('total_units', snapshot['po_data']['totals'])
        self.assertEqual(non_price_payload(snapshot['po_data']), non_price_payload(source))

    def test_historical_html_is_reused_without_inventing_new_non_price_regions(self):
        old = self.original_html.replace('<div class="total-label">Total Units</div>', '')
        Path(self.factory_po.html_file.path).write_text(old, encoding='utf-8')
        before = GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get()
        document = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        html = Path(document.html_file.path).read_text()
        self.assertEqual(non_price_html(html), non_price_html(old))
        self.assertEqual(document.source_data['rendering']['template'], 'source_factory_po.html_file')
        self.assertEqual(document.source_data['rendering']['mode'], 'frozen_factory_html')
        self.assertEqual(GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get(), before)
        self.assertEqual(Path(self.factory_po.html_file.path).read_text(), old)

    def test_historical_html_with_wrong_batch_content_is_rejected(self):
        old = self.original_html.replace('Frozen Buyer', 'Wrong Buyer')
        Path(self.factory_po.html_file.path).write_text(old, encoding='utf-8')
        with patch('commercial_pos.services.generation_service.render_commercial_files', side_effect=fake_render):
            existing = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        before = GeneratedDocument.objects.filter(pk=existing.pk).values().get()
        with self.assertRaisesMessage(CommercialPOError, 'HTML identity conflicts'):
            ensure_commercial_po_for_batch(self.batch.pk, self.user, regenerate_existing=True)
        self.assertEqual(GeneratedDocument.objects.filter(pk=existing.pk).values().get(), before)
        self.assertTrue(Path(existing.pdf_file.path).is_file())

    def test_historical_total_without_row_wrappers_keeps_original_layout(self):
        soup = BeautifulSoup(self.original_html, 'html.parser')
        legacy_total = BeautifulSoup(
            '<div class="total-section"><div class="total-box"><div class="total-label">Total</div>'
            '<div class="total-value">9,084.00 €</div></div></div>', 'html.parser')
        soup.select_one('.total-section').replace_with(legacy_total.div)
        old = str(soup)
        Path(self.factory_po.html_file.path).write_text(old, encoding='utf-8')
        document = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        html = Path(document.html_file.path).read_text()
        self.assertEqual(non_price_html(html), non_price_html(old))
        self.assertNotIn('Total Units', BeautifulSoup(html, 'html.parser').get_text())
        self.assertIn('12,160.00 €', html)
        self.assertEqual(Path(self.factory_po.html_file.path).read_text(), old)

    def test_discount_note_keeps_expiration_and_threshold_text(self):
        note = '20% discount applied. Earliest expiration: 2027-03-05 Threshold: 365 days.'
        self.factory_po.source_data['po_data']['items'][0]['discount_note'] = note
        self.factory_po.save(update_fields=['source_data'])
        self.assertEqual(self.payload()['items'][0]['discount_note'], '0%' + note[3:])

    def test_missing_damaged_or_conflicting_factory_snapshot_fails_closed(self):
        for mutate in (lambda p: p.pop('items'), lambda p: p['items'][0].update(quantity_raw=2),
                       lambda p: p['totals'].update(total_units=77), lambda p: p['debug'].update(shipment_batch_id=999)):
            with self.subTest(mutation=mutate):
                data = deepcopy(self.original); mutate(data)
                self.factory_po.source_data['po_data'] = data
                self.factory_po.save(update_fields=['source_data'])
                with self.assertRaises(CommercialPOError): self.snapshot()

    def test_no_matching_or_multiple_commercial_rules_fail_closed(self):
        CommercialPOPricePolicy.objects.update(is_active=False)
        with self.assertRaises(CommercialPOError): self.snapshot()
        CommercialPOPricePolicy.objects.update(is_active=True)
        CommercialPOPricePolicy.objects.create(name='Overlapping', unit_price=Decimal('160.00'))
        with self.assertRaises(CommercialPOError): self.snapshot()
