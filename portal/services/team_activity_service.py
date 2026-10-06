"""Read-only activity facts; activity actor and current owner are independent."""
from datetime import datetime, time, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from hospital_engagements.models import HospitalEngagement, HospitalFollowUp

STAGE_RANK = {"stage_1": 1, "stage_2": 2, "stage_3": 3}
METRIC_LABELS = (
    ("contacted_hospitals", _("联系医院")),
    ("communications", _("沟通次数")),
    ("advanced_hospitals", _("阶段前进医院")),
    ("entered_stage_2", _("进入第二阶段")),
    ("entered_stage_3", _("进入第三阶段")),
)


def salesperson_candidates():
    # EXISTS avoids a multiplying join between ownership and authored history.
    return get_user_model().objects.filter(is_active=True).annotate(
        owns_engagement=Exists(HospitalEngagement.objects.filter(owner_id=OuterRef("pk"))),
        has_activity=Exists(HospitalFollowUp.objects.filter(created_by_id=OuterRef("pk"))),
    ).filter(Q(owns_engagement=True) | Q(has_activity=True)).order_by("username", "pk")


class ActivityFilterForm(forms.Form):
    period = forms.ChoiceField(label=_("时间范围"), choices=(
        ("today", _("今天")), ("week", _("本周")), ("custom", _("自定义日期")),
    ))
    start = forms.DateField(label=_("开始日期"), required=False,
                           widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    end = forms.DateField(label=_("结束日期"), required=False,
                         widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    salesperson = forms.ModelChoiceField(label=_("业务员"), required=False,
                                        empty_label=_("全部"), queryset=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["salesperson"].queryset = salesperson_candidates()

    def clean(self):
        data = super().clean()
        if data.get("period") == "custom":
            if not data.get("start") or not data.get("end"):
                raise forms.ValidationError(_("请选择开始日期和结束日期。"))
            if data["start"] > data["end"]:
                raise forms.ValidationError(_("开始日期不能晚于结束日期。"))
            if data["end"] == datetime.max.date():
                self.add_error("end", _("结束日期超出支持范围。"))
        return data


def period_bounds(data, now):
    """Local-calendar midnights preserve DST; week-to-date excludes the future."""
    tz = timezone.get_current_timezone()
    today = timezone.localdate(now, timezone=tz)

    def midnight(day):
        return timezone.make_aware(datetime.combine(day, time.min), tz)

    if data["period"] == "week":
        return midnight(today - timedelta(days=today.weekday())), now
    if data["period"] == "custom":
        return midnight(data["start"]), midnight(data["end"] + timedelta(days=1))
    return midnight(today), midnight(today + timedelta(days=1))


def forward_stage_filter():
    # Unknown/blank stages cannot match. Never infer rank from string suffixes.
    pairs = Q(pk__in=[])
    for source, source_rank in STAGE_RANK.items():
        for target, target_rank in STAGE_RANK.items():
            if target_rank > source_rank:
                pairs |= Q(stage_from=source, stage_to=target)
    return Q(activity_type=HospitalFollowUp.ActivityType.STAGE_CHANGE) & pairs


def metric_expressions():
    communication = Q(activity_type=HospitalFollowUp.ActivityType.COMMUNICATION)
    forward = forward_stage_filter()
    return {
        "contacted_hospitals": Count("engagement__hospital_id", filter=communication, distinct=True),
        "communications": Count("pk", filter=communication),
        "advanced_hospitals": Count("engagement__hospital_id", filter=forward, distinct=True),
        "entered_stage_2": Count("engagement__hospital_id", filter=forward & Q(stage_to="stage_2"), distinct=True),
        "entered_stage_3": Count("engagement__hospital_id", filter=forward & Q(stage_to="stage_3"), distinct=True),
    }


def paginated(query, params, key, anchor):
    page = Paginator(query, 30).get_page(params.get(key))

    def link(number):
        updated = params.copy()
        updated[key] = number
        return "?" + updated.urlencode() + "#" + anchor

    return {
        "page": page,
        "previous_url": link(page.previous_page_number()) if page.has_previous() else None,
        "next_url": link(page.next_page_number()) if page.has_next() else None,
    }


def build_team_activity_context(params, *, now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    data = params.copy()
    if "period" not in data:
        data["period"] = "today"
    form = ActivityFilterForm(data)
    context = {"filter_form": form, "valid_filters": form.is_valid(), "snapshot_date": today}
    if not context["valid_filters"]:
        return context

    start, end = period_bounds(form.cleaned_data, now)
    actor = form.cleaned_data["salesperson"]
    activity = HospitalFollowUp.objects.filter(occurred_at__gte=start, occurred_at__lt=end)
    # Historical work belongs to the author, NOT the current engagement owner.
    if actor:
        activity = activity.filter(created_by=actor)
    metrics = activity.aggregate(**metric_expressions())
    context.update({
        "period_start": start, "period_end": end, "metrics": metrics,
        "kpis": [{"label": label, "value": metrics[key]} for key, label in METRIC_LABELS],
        "show_summary": actor is None,
    })
    if actor is None:
        # One grouped query for every actor; preserve inactive/deleted authors' facts.
        summary = activity.filter(
            Q(activity_type=HospitalFollowUp.ActivityType.COMMUNICATION) | forward_stage_filter()
        ).order_by().values(
            "created_by_id", "created_by__username", "created_by__first_name", "created_by__last_name",
        ).annotate(**metric_expressions()).order_by("created_by__username", "created_by_id")
        context["summary"] = [
            dict(row, actor_name=(" ".join(filter(None, [
                row["created_by__first_name"], row["created_by__last_name"],
            ])).strip() or row["created_by__username"] or _("未分配")))
            for row in summary
        ]

    detail = activity.select_related(
        "engagement__hospital", "engagement__owner", "created_by", "contact", "department",
        "contact__department",
    ).order_by("-occurred_at", "-pk")
    context["communications"] = paginated(
        detail.filter(activity_type=HospitalFollowUp.ActivityType.COMMUNICATION),
        params, "communications_page", "communications",
    )
    context["advancements"] = paginated(
        detail.filter(forward_stage_filter()), params, "advancements_page", "advancements",
    )

    # Current responsibility is a separate snapshot, not constrained by the period
    # or by the author of the latest communication.
    overdue = HospitalEngagement.objects.filter(
        hospital__is_active=True, next_follow_up_date__lt=today,
    ).select_related("hospital", "owner")
    if actor:
        overdue = overdue.filter(owner=actor)
    latest = HospitalFollowUp.objects.filter(
        engagement_id=OuterRef("pk"), activity_type=HospitalFollowUp.ActivityType.COMMUNICATION,
    ).order_by("-occurred_at", "-pk")
    overdue = overdue.annotate(
        latest_communication_at=Subquery(latest.values("occurred_at")[:1]),
        latest_communication_summary=Subquery(latest.values("summary")[:1]),
    ).order_by("next_follow_up_date", "hospital__name", "pk")
    context["overdue"] = paginated(overdue, params, "overdue_page", "overdue")
    for item in context["overdue"]["page"]:
        item.overdue_days = (today - item.next_follow_up_date).days
    return context

