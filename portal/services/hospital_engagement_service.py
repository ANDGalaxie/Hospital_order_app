from django.db.models import OuterRef, Prefetch, Q, Subquery
from django.utils.translation import gettext_lazy as _

from hospital_engagements.models import (
    HospitalContact, HospitalDepartment, HospitalEngagement,
    HospitalFollowUp, HospitalProductInterest,
)

STAGES = {
    "stage-1": {"value": "stage_1", "title": _("第一阶段"), "subtitle": _("未接触"), "theme": "first"},
    "stage-2": {"value": "stage_2", "title": _("第二阶段"), "subtitle": _("接触中"), "theme": "second"},
    "stage-3": {"value": "stage_3", "title": _("第三阶段"), "subtitle": _("落地中"), "theme": "third"},
}


def stage_queryset(stage, filters):
    latest = HospitalFollowUp.objects.filter(
        engagement_id=OuterRef("pk"), activity_type=HospitalFollowUp.ActivityType.COMMUNICATION
    ).order_by("-occurred_at", "-pk")
    query = HospitalEngagement.objects.filter(hospital__is_active=True, stage=stage).select_related(
        "hospital", "owner"
    ).annotate(
        latest_communication_at=Subquery(latest.values("occurred_at")[:1]),
        latest_communication_summary=Subquery(latest.values("summary")[:1]),
        latest_communication_channel=Subquery(latest.values("channel")[:1]),
    ).prefetch_related(
        Prefetch("hospital__engagement_departments",
                 queryset=HospitalDepartment.objects.filter(is_active=True), to_attr="active_departments"),
        Prefetch("hospital__engagement_contacts",
                 queryset=HospitalContact.objects.filter(is_active=True).select_related("department"),
                 to_attr="active_contacts"),
        Prefetch("product_interests",
                 queryset=HospitalProductInterest.objects.select_related("product", "department"),
                 to_attr="interest_list"),
    )
    if filters.get("q"):
        term = filters["q"]
        query = query.filter(
            Q(hospital__name__icontains=term)
            | Q(hospital__engagement_contacts__name__icontains=term)
            | Q(product_interests__product__code__icontains=term)
            | Q(product_interests__product_text__icontains=term)
        ).distinct()
    if filters.get("owner"):
        query = query.filter(owner=filters["owner"])
    if filters.get("priority"):
        query = query.filter(priority=filters["priority"])
    return query


def decorate_rows(rows):
    for item in rows:
        hospital = item.hospital
        item.department_names = [d.name for d in hospital.active_departments]
        item.doctor_names = [c.name for c in hospital.active_contacts if c.contact_type == "doctor"]
        item.primary_contact = next((c for c in hospital.active_contacts if c.is_primary), None)
        item.interest_labels = [
            " · ".join(filter(None, [i.product.code if i.product else "", i.product_text]))
            for i in item.interest_list
        ]
        item.latest_channel_label = dict(HospitalFollowUp.Channel.choices).get(item.latest_communication_channel, "")
    return rows
