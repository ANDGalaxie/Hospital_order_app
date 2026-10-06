from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from hospitals.models import Hospital
from .models import HospitalContact, HospitalEngagement, HospitalFollowUp


@transaction.atomic
def change_hospital_engagement_stage(engagement, new_stage, user):
    if new_stage not in HospitalEngagement.Stage.values:
        raise ValidationError(_("无效的阶段。"))
    locked = HospitalEngagement.objects.select_for_update().get(pk=engagement.pk)
    old_stage = locked.stage
    if old_stage != new_stage:
        locked.stage = new_stage
        locked.save(update_fields=["stage", "updated_at"])
        # Store machine stage values; the Portal translates labels when rendering.
        HospitalFollowUp.objects.create(
            engagement=locked, activity_type=HospitalFollowUp.ActivityType.STAGE_CHANGE,
            stage_from=old_stage, stage_to=new_stage, created_by=user,
            occurred_at=timezone.now(),
        )
    return locked


@transaction.atomic
def save_contact(contact):
    # Lock the parent even when there are no contacts yet.
    Hospital.objects.select_for_update().get(pk=contact.hospital_id)
    contact.full_clean(validate_constraints=False)
    if not contact.is_active:
        contact.is_primary = False
    if contact.is_primary:
        HospitalContact.objects.filter(
            hospital_id=contact.hospital_id, is_active=True, is_primary=True,
        ).exclude(pk=contact.pk).update(is_primary=False, updated_at=timezone.now())
    contact.save()
    return contact


@transaction.atomic
def add_communication(follow_up, user):
    locked = HospitalEngagement.objects.select_for_update().get(pk=follow_up.engagement_id)
    follow_up.engagement = locked
    follow_up.activity_type = HospitalFollowUp.ActivityType.COMMUNICATION
    follow_up.created_by = user
    follow_up.full_clean()
    follow_up.save()
    changed = []
    for field in ("next_action", "next_follow_up_date"):
        value = getattr(follow_up, field)
        if value:
            setattr(locked, field, value)
            changed.append(field)
    if changed:
        locked.save(update_fields=changed + ["updated_at"])
    return follow_up
