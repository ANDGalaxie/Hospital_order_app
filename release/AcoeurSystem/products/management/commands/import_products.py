from decimal import Decimal, InvalidOperation
import re

import pandas as pd
from django.core.management.base import BaseCommand, CommandError

from products.models import Product


def normalize_header(value):
    """
    把 Excel 表头变成统一格式，方便匹配。

    例如：
    Product Code -> product_code
    product code -> product_code
    Référence -> reference
    """
    value = str(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value


def find_column(df, possible_names, required=True):
    """
    在 Excel 表中寻找某一列。

    possible_names 是可能的列名列表。
    如果找不到必需列，就报错。
    """
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
    """
    清理文本。
    """
    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.lower() in ["nan", "none", "null"]:
        return ""

    return text


def parse_decimal(value, default):
    """
    把 Excel 里的价格转成 Decimal。

    如果为空，就使用默认值。
    """
    if pd.isna(value):
        return Decimal(default)

    text = str(value).strip()

    if not text:
        return Decimal(default)

    text = text.replace("€", "")
    text = text.replace(",", ".")
    text = text.strip()

    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(default)


class Command(BaseCommand):
    help = "Import products from an Excel file."

    def add_arguments(self, parser):
        parser.add_argument(
            "xlsx_path",
            type=str,
            help="Path to product_database.xlsx",
        )

    def handle(self, *args, **options):
        xlsx_path = options["xlsx_path"]

        try:
            df = pd.read_excel(xlsx_path)
        except Exception as exc:
            raise CommandError(f"Cannot read Excel file: {exc}")

        if df.empty:
            raise CommandError("Excel file is empty.")

        code_col = find_column(
            df,
            [
                "product_code",
                "code",
                "reference",
                "ref",
                "product number",
                "product_number",
                "numero produit",
                "numéro produit",
            ],
            required=True,
        )

        description_col = find_column(
            df,
            [
                "description",
                "product_description",
                "designation",
                "désignation",
                "desc",
            ],
            required=False,
        )

        hospital_price_col = find_column(
            df,
            [
                "hospital_unit_price",
                "hospital_price",
                "selling_price",
                "sale_price",
                "price",
                "prix",
                "unit_price",
            ],
            required=False,
        )

        factory_price_col = find_column(
            df,
            [
                "factory_unit_price",
                "factory_price",
                "purchase_price",
                "buying_price",
                "supplier_price",
            ],
            required=False,
        )

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for _, row in df.iterrows():
            code = clean_text(row.get(code_col))

            if not code:
                skipped_count += 1
                continue

            description = ""
            if description_col:
                description = clean_text(row.get(description_col))

            hospital_unit_price = Decimal("270.00")
            if hospital_price_col:
                hospital_unit_price = parse_decimal(
                    row.get(hospital_price_col),
                    "270.00",
                )

            factory_unit_price = Decimal("120.00")
            if factory_price_col:
                factory_unit_price = parse_decimal(
                    row.get(factory_price_col),
                    "120.00",
                )

            product, created = Product.objects.update_or_create(
                code=code,
                defaults={
                    "description": description,
                    "hospital_unit_price": hospital_unit_price,
                    "factory_unit_price": factory_unit_price,
                    "is_active": True,
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Products import completed. "
                f"Created: {created_count}, "
                f"Updated: {updated_count}, "
                f"Skipped: {skipped_count}"
            )
        )
