from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from hospital_engagements.models import (
    ACCESS_PERMISSION, HospitalContact, HospitalDepartment, HospitalEngagement,
    HospitalFollowUp, HospitalProductInterest,
)
from hospital_engagements.services import add_communication, change_hospital_engagement_stage, save_contact
from portal.forms.hospital_engagement_forms import (
    BulkStageForm, BusinessForm, CommunicationForm, ContactForm,
    DepartmentForm, FilterForm, InterestForm, StageForm,
)
from portal.services.common import get_safe_next_url
from portal.services.hospital_engagement_service import STAGES, decorate_rows, stage_queryset


def engagement_access(view):
    return staff_member_required(permission_required(ACCESS_PERMISSION, raise_exception=True)(view))


def engagement_for(hospital_id):
    return get_object_or_404(
        HospitalEngagement.objects.select_related("hospital", "owner"), hospital_id=hospital_id
    )


def detail_url(engagement):
    return reverse("portal:engagement_detail", args=[engagement.hospital_id])


def stage_for(slug):
    if slug not in STAGES:
        raise Http404
    return STAGES[slug]


def form_page(request, engagement, form, title, status=200):
    return render(request, "portal/engagements/form.html", {
        "engagement": engagement, "form": form, "title": title,
    }, status=status)


@engagement_access
@require_GET
def home(request):
    counts = dict(HospitalEngagement.objects.filter(hospital__is_active=True).order_by().values(
        "stage"
    ).annotate(total=Count("pk")).values_list("stage", "total"))
    cards = [dict(info, slug=slug, count=counts.get(info["value"], 0)) for slug, info in STAGES.items()]
    return render(request, "portal/engagements/home.html", {"cards": cards})


@engagement_access
@require_GET
def stage_list(request, slug):
    stage = stage_for(slug)
    form = FilterForm(request.GET)
    valid = form.is_valid()
    query = stage_queryset(stage["value"], form.cleaned_data) if valid else HospitalEngagement.objects.none()
    page = Paginator(query, 40).get_page(request.GET.get("page"))
    decorate_rows(page.object_list)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "portal/engagements/stage.html", {
        "stage": stage, "slug": slug, "filter_form": form, "page_obj": page,
        "query_params": params.urlencode(), "stage_choices": HospitalEngagement.Stage.choices,
    }, status=200 if valid else 400)


@engagement_access
@require_GET
def detail(request, hospital_id):
    engagement = engagement_for(hospital_id)
    timeline = engagement.follow_ups.select_related("created_by", "contact", "department")
    return render(request, "portal/engagements/detail.html", {
        "engagement": engagement,
        "business_form": BusinessForm(instance=engagement),
        "stage_form": StageForm(initial={"stage": engagement.stage}),
        "communication_form": CommunicationForm(engagement=engagement),
        "departments": engagement.hospital.engagement_departments.all(),
        "contacts": engagement.hospital.engagement_contacts.select_related("department"),
        "interests": engagement.product_interests.select_related("product", "department"),
        "page_obj": Paginator(timeline, 30).get_page(request.GET.get("page")),
    })


@engagement_access
@require_POST
def change_stage(request, hospital_id):
    engagement = engagement_for(hospital_id)
    form = StageForm(request.POST)
    if not form.is_valid():
        return form_page(request, engagement, form, _("更改阶段"), 400)
    change_hospital_engagement_stage(engagement, form.cleaned_data["stage"], request.user)
    messages.success(request, _("阶段已保存。"))
    return redirect(get_safe_next_url(request, detail_url(engagement)))


@engagement_access
@require_POST
def bulk_move(request, slug):
    stage = stage_for(slug)
    query = HospitalEngagement.objects.filter(stage=stage["value"], hospital__is_active=True)
    form = BulkStageForm(request.POST, queryset=query)
    if not form.is_valid():
        return render(request, "portal/engagements/bulk_error.html", {"form": form, "slug": slug}, status=400)
    with transaction.atomic():
        # Stable lock order prevents two overlapping bulk moves from deadlocking.
        for engagement in form.cleaned_data["engagements"].order_by("pk"):
            change_hospital_engagement_stage(engagement, form.cleaned_data["stage"], request.user)
    messages.success(request, _("阶段已保存。"))
    return redirect(get_safe_next_url(request, reverse("portal:engagement_stage", args=[slug])))


@engagement_access
@require_POST
def save_business(request, hospital_id):
    with transaction.atomic():
        engagement = get_object_or_404(HospitalEngagement.objects.select_for_update(), hospital_id=hospital_id)
        form = BusinessForm(request.POST, instance=engagement)
        if not form.is_valid():
            return form_page(request, engagement, form, _("商务信息"), 400)
        form.save()
    messages.success(request, _("已保存。"))
    return redirect(detail_url(engagement))


RECORDS = {
    "departments": (HospitalDepartment, DepartmentForm, _("科室")),
    "contacts": (HospitalContact, ContactForm, _("联系人")),
    "interests": (HospitalProductInterest, InterestForm, _("产品需求")),
}


def record_for(engagement, kind, record_id=None):
    if kind not in RECORDS:
        raise Http404
    model, form_class, title = RECORDS[kind]
    scope = {"engagement": engagement} if kind == "interests" else {"hospital": engagement.hospital}
    instance = get_object_or_404(model, pk=record_id, **scope) if record_id else model(**scope)
    return instance, form_class, title, scope


@engagement_access
@require_http_methods(["GET", "POST"])
def edit_record(request, hospital_id, kind, record_id=None):
    engagement = engagement_for(hospital_id)
    instance, form_class, title, scope = record_for(engagement, kind, record_id)
    form = form_class(request.POST if request.method == "POST" else None, instance=instance, **scope)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if kind == "contacts":
                    save_contact(form.save(commit=False))
                else:
                    form.save()
        except (ValidationError, IntegrityError):
            form.add_error(None, _("无法保存，请检查重复资料并重试。"))
        else:
            messages.success(request, _("已保存。"))
            return redirect(detail_url(engagement))
    return form_page(request, engagement, form, title, 400 if request.method == "POST" else 200)


@engagement_access
@require_POST
def remove_record(request, hospital_id, kind, record_id):
    engagement = engagement_for(hospital_id)
    instance = record_for(engagement, kind, record_id)[0]
    if kind == "interests":
        instance.delete()
    elif kind == "contacts":
        instance.is_active = False
        save_contact(instance)
    else:
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
    messages.success(request, _("已保存。"))
    return redirect(detail_url(engagement))


@engagement_access
@require_POST
def primary_contact(request, hospital_id, record_id):
    engagement = engagement_for(hospital_id)
    contact = get_object_or_404(HospitalContact, pk=record_id, hospital=engagement.hospital, is_active=True)
    contact.is_primary = True
    save_contact(contact)
    return redirect(detail_url(engagement))


@engagement_access
@require_POST
def add_follow_up(request, hospital_id):
    engagement = engagement_for(hospital_id)
    form = CommunicationForm(request.POST, engagement=engagement)
    if not form.is_valid():
        return form_page(request, engagement, form, _("新增沟通"), 400)
    add_communication(form.save(commit=False), request.user)
    messages.success(request, _("沟通记录已保存。"))
    return redirect(detail_url(engagement))
