from datetime import date, datetime

from django.db import migrations, models


SUPPORTED_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
)


def parse_order_date(value):
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value or "").strip()

    if not text:
        return None

    for date_format in SUPPORTED_FORMATS:
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            continue

    return None


def backfill_order_dates(apps, schema_editor):
    Order = apps.get_model("orders", "Order")

    for order in Order.objects.filter(
        order_date__isnull=True
    ).iterator():
        data = order.extracted_order_data or {}

        if not isinstance(data, dict):
            continue

        header = data.get("header") or {}
        summary = data.get("summary") or {}

        raw_date = None

        if isinstance(header, dict):
            raw_date = header.get("order_date")

        if (
            not raw_date
            and isinstance(summary, dict)
        ):
            raw_date = summary.get("order_date")

        parsed = parse_order_date(raw_date)

        if parsed:
            Order.objects.filter(
                pk=order.pk,
                order_date__isnull=True,
            ).update(order_date=parsed)


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0011_backfill_expiration_threshold_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="order_date",
            field=models.DateField(
                blank=True,
                help_text=(
                    "医院原始订单的下单日期。"
                    "仅来自医院订单 OCR 或人工确认，不允许日期回退。"
                ),
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_order_dates,
            migrations.RunPython.noop,
        ),
    ]
