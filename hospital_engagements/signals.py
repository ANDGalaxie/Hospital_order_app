from django.db.models.signals import post_save
from django.dispatch import receiver

from hospitals.models import Hospital
from .models import HospitalEngagement


@receiver(post_save, sender=Hospital, dispatch_uid="hospital_engagements.ensure_new_hospital")
def ensure_new_hospital_engagement(sender, instance, created, raw=False, using="default", **kwargs):
    if created and not raw:
        HospitalEngagement.objects.using(using).get_or_create(hospital_id=instance.pk)
