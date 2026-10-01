import io
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from orders.models import Order


class MediaIntegrityCommandTests(TestCase):
    def test_command_reports_files_without_modifying_records(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ):
            user = get_user_model().objects.create_user(username="integrity-user")
            readable = Path(media_root, "hospital_orders", "readable.pdf")
            readable.parent.mkdir(parents=True)
            readable.write_bytes(b"%PDF-1.7\nsynthetic")

            Order.objects.create(
                bon_de_commande="INTEGRITY-READABLE",
                hospital_order_pdf="hospital_orders/readable.pdf",
                created_by=user,
            )
            Order.objects.create(
                bon_de_commande="INTEGRITY-MISSING",
                hospital_order_pdf="hospital_orders/missing.pdf",
                created_by=user,
            )
            before = list(Order.objects.values_list("pk", "hospital_order_pdf"))

            output = io.StringIO()
            call_command("check_media_integrity", stdout=output)

            self.assertIn("readable=1", output.getvalue())
            self.assertIn("MISSING=1", output.getvalue())
            self.assertEqual(
                list(Order.objects.values_list("pk", "hospital_order_pdf")), before
            )
