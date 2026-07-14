from django.db import migrations


def backfill_threshold(apps, schema_editor):
    OrderItem = apps.get_model(
        "orders",
        "OrderItem",
    )

    items = (
        OrderItem.objects.filter(
            price_policy_id__isnull=False,
            expiration_threshold_days__isnull=True,
        )
        .select_related("price_policy")
        .iterator()
    )

    for item in items:
        threshold_days = getattr(
            item.price_policy,
            "expiration_threshold_days",
            365,
        )

        item.expiration_threshold_days = (
            threshold_days or 365
        )

        item.save(
            update_fields=[
                "expiration_threshold_days",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0010_orderitem_expiration_threshold_days'),
    ]

    operations = [
        migrations.RunPython(
            backfill_threshold,
            migrations.RunPython.noop,
        ),
    ]
