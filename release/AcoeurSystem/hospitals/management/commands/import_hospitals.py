import re
import unicodedata

import pandas as pd
from django.core.management.base import BaseCommand, CommandError

from hospitals.models import Hospital


def normalize_header(value):
    """
    标准化 Excel 表头。
    """
    value = str(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value


def normalize_match_text(value):
    """
    用于医院名称模糊匹配的标准化文本。

    例如：
    Clinique Louis Pasteur -> clinique louis pasteur
    """
    if value is None:
        return ""

    text = str(value).strip().lower()

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )

    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def find_column(df, possible_names, required=True):
    normalized_columns = {
        normalize_header(col): col
        for col in df.columns
    }

    for name in possible_names:
        key = normalize_header(name)
        if key in normalized_columns:
            return normalized_columns[key]

    if required:
        available = ", ".join(str(col) for col in df.columns)
        raise CommandError(
            f"Cannot find required column. Expected one of: {possible_names}. "
            f"Available columns: {available}"
        )

    return None


def clean_text(value):
    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.lower() in ["nan", "none", "null"]:
        return ""

    return text


class Command(BaseCommand):
    help = "Import hospitals from an Excel file."

    def add_arguments(self, parser):
        parser.add_argument(
            "xlsx_path",
            type=str,
            help="Path to hospital_database.xlsx",
        )

    def handle(self, *args, **options):
        xlsx_path = options["xlsx_path"]

        try:
            df = pd.read_excel(xlsx_path)
        except Exception as exc:
            raise CommandError(f"Cannot read Excel file: {exc}")

        if df.empty:
            raise CommandError("Excel file is empty.")

        name_col = find_column(
            df,
            [
                "name",
                "hospital_name",
                "hospital",
                "hopital",
                "hôpital",
                "nom",
                "nom_hopital",
                "nom_hôpital",
            ],
            required=True,
        )

        billing_address_col = find_column(
            df,
            [
                "billing_address",
                "invoice_address",
                "address",
                "adresse",
                "adresse_facturation",
                "facturation",
            ],
            required=False,
        )

        shipping_address_col = find_column(
            df,
            [
                "default_shipping_address",
                "shipping_address",
                "delivery_address",
                "adresse_livraison",
                "livraison",
            ],
            required=False,
        )

        contact_col = find_column(
            df,
            [
                "contact",
                "contact_name",
                "correspondant",
                "personne_contact",
            ],
            required=False,
        )

        phone_col = find_column(
            df,
            [
                "phone",
                "telephone",
                "téléphone",
                "tel",
                "tél",
            ],
            required=False,
        )

        fax_col = find_column(
            df,
            [
                "fax",
            ],
            required=False,
        )

        email_col = find_column(
            df,
            [
                "email",
                "mail",
                "e_mail",
            ],
            required=False,
        )

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for _, row in df.iterrows():
            name = clean_text(row.get(name_col))

            if not name:
                skipped_count += 1
                continue

            billing_address = ""
            if billing_address_col:
                billing_address = clean_text(row.get(billing_address_col))

            default_shipping_address = ""
            if shipping_address_col:
                default_shipping_address = clean_text(row.get(shipping_address_col))

            contact_name = ""
            if contact_col:
                contact_name = clean_text(row.get(contact_col))

            phone = ""
            if phone_col:
                phone = clean_text(row.get(phone_col))

            fax = ""
            if fax_col:
                fax = clean_text(row.get(fax_col))

            email = ""
            if email_col:
                email = clean_text(row.get(email_col))

            hospital, created = Hospital.objects.update_or_create(
                name=name,
                defaults={
                    "normalized_name": normalize_match_text(name),
                    "billing_address": billing_address,
                    "default_shipping_address": default_shipping_address,
                    "contact_name": contact_name,
                    "phone": phone,
                    "fax": fax,
                    "email": email,
                    "is_active": True,
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Hospitals import completed. "
                f"Created: {created_count}, "
                f"Updated: {updated_count}, "
                f"Skipped: {skipped_count}"
            )
        )
