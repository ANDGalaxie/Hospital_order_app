from datetime import date
from decimal import Decimal

from django.db import migrations


def seed(apps, schema_editor):
    db = schema_editor.connection.alias
    Policy = apps.get_model("commercial_pos", "CommercialPOPricePolicy")
    Policy.objects.using(db).create(name="Commercial PO before 20 April 2026", start_date=None,
                                   end_date=date(2026, 4, 19), unit_price=Decimal("165.00"), currency="EUR")
    Policy.objects.using(db).create(name="Commercial PO from 20 April 2026", start_date=date(2026, 4, 20),
                                   end_date=None, unit_price=Decimal("160.00"), currency="EUR")
    # No user creation or username-specific grants.
    apps.get_model("auth", "Group").objects.using(db).get_or_create(name="Hospital Demo")


class Migration(migrations.Migration):
    dependencies = [("commercial_pos", "0001_initial"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
