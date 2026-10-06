from django.db import migrations


def initialize_engagements(apps, schema_editor):
    Hospital = apps.get_model("hospitals", "Hospital")
    Engagement = apps.get_model("hospital_engagements", "HospitalEngagement")
    alias = schema_editor.connection.alias
    for hospital_id in Hospital.objects.using(alias).values_list("pk", flat=True).iterator():
        Engagement.objects.using(alias).get_or_create(
            hospital_id=hospital_id,
            defaults={"stage": "stage_1", "priority": "B", "owner_id": None},
        )


class Migration(migrations.Migration):
    dependencies = [("hospital_engagements", "0001_initial")]
    operations = [migrations.RunPython(initialize_engagements, migrations.RunPython.noop)]
