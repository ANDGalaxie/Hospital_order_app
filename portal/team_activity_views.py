from django.shortcuts import render
from django.views.decorators.http import require_GET

from hospital_engagements.boss_access import boss_account_required
from portal.services.team_activity_service import build_team_activity_context


@boss_account_required
@require_GET
def team_activity(request):
    context = build_team_activity_context(request.GET)
    return render(request, "portal/team_activity/overview.html", context,
                  status=200 if context["valid_filters"] else 400)

