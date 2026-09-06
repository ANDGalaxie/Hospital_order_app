from django.test import SimpleTestCase

from factory_confirmations.services.factory_confirmation_extraction_service import (
    extract_bon_de_commande_from_text,
)
from legacy_services.factory_confirmation_extractor import extract_factory_header


class FactoryConfirmationBonParserTests(SimpleTestCase):
    def test_order_plain_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text("Order: 147465"),
            "147465",
        )

    def test_order_n_degree_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text("Order: N° 147465"),
            "147465",
        )

    def test_order_newline_n_degree_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text("Order:\nN° 147465"),
            "147465",
        )

    def test_order_n_ordinal_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text("Order: Nº 147465"),
            "147465",
        )

    def test_order_no_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text("Order: No 147465"),
            "147465",
        )

    def test_bon_de_commande_n_degree_number(self):
        self.assertEqual(
            extract_bon_de_commande_from_text(
                "BON DE COMMANDE N° 147465"
            ),
            "147465",
        )

    def test_remaining_supported_standalone_and_order_markers(self):
        for text in (
            "147465",
            "N° 147465",
            "Nº 147465",
            "No 147465",
            "NO. 147465",
            "N 147465",
            "# 147465",
            "Order: NO. 147465",
            "order: # 147465",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    extract_bon_de_commande_from_text(text),
                    "147465",
                )

    def test_numeric_distractors_are_not_order_number(self):
        text = """
        WH/OUT/00290
        Shipping Date: 07/27/2026 10:41:45
        BMA-5.0015
        085427120926F0995001
        085427120926F0995002
        HS Code 90219011
        """
        self.assertIsNone(extract_bon_de_commande_from_text(text))

    def test_labeled_order_is_selected_amid_numeric_distractors(self):
        text = """
        WH/OUT/00290
        Order:
        N° 147465
        Shipping Date: 07/27/2026 10:41:45
        BMA-5.0015 085427120926F0995001
        BMA-5.0015 085427120926F0995002
        HS Code 90219011
        """
        self.assertEqual(
            extract_bon_de_commande_from_text(text),
            "147465",
        )
    def test_factory_header_uses_shared_order_parser(self):
        header = extract_factory_header(
            "Order:\nN° 147465\nShipping Date:\n"
            "07/27/2026 10:41:45"
        )

        self.assertEqual(header["bon_de_commande"], "147465")
        self.assertEqual(header["shipping_date_only_iso"], "2026-07-27")
