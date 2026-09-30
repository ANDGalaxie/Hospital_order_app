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


class FactoryOrderReferenceTests(SimpleTestCase):
    def test_suffix_formats_and_base_bon(self):
        from factory_confirmations.services.bon_de_commande_parser import parse_factory_order_reference
        for text, batch in [
            ("Order:\nN°155141-B2", 2), ("155141-B3", 3),
            ("155141-b2", 2), ("155141 - B2", 2),
            ("155141-B 2", 2), ("N°155141-B2", 2),
            ("Order: N°155141-B2", 2), ("155141-B1", 1),
            ("155141-B1000000", 1000000),
        ]:
            with self.subTest(text=text):
                result = parse_factory_order_reference(text)
                self.assertEqual(result["bon_de_commande"], "155141")
                self.assertEqual(result["batch_number"], batch)
                self.assertTrue(result["has_explicit_batch"])
                self.assertEqual(extract_bon_de_commande_from_text(text), "155141")
                self.assertNotEqual(result["bon_de_commande"], "1551412")

    def test_legacy_reference_does_not_claim_batch_one(self):
        from factory_confirmations.services.bon_de_commande_parser import parse_factory_order_reference
        result = parse_factory_order_reference("N°155141")
        self.assertIsNone(result["batch_number"])
        self.assertFalse(result["has_explicit_batch"])

    def test_invalid_suffix_is_not_treated_as_legacy(self):
        from factory_confirmations.services.bon_de_commande_parser import parse_factory_order_reference
        for suffix in ("B0", "B-2", "B2abc", "B", "B2.5"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_factory_order_reference("Order: 155141-" + suffix)

    def test_header_preserves_suffix_for_ocr_and_text_extraction(self):
        header = extract_factory_header("Order:\nN°155141-B2\nShipping Date:\n09/22/2026 14:41:56")
        self.assertEqual(header["factory_order_reference"]["batch_number"], 2)
        self.assertEqual(header["bon_de_commande"], "155141")
