"""Shared browsing endpoints using only hospital-safe projections."""
from django.shortcuts import render
from portal.services.hospital_browse_service import build_shared_read_context


def shared_read_page(request, **kwargs):
    return render(request, "portal/hospital_browse/page.html", build_shared_read_context(request, **kwargs))
