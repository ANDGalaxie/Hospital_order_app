"""One display policy shared by homepage, middleware, views and file endpoints."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden


DEMO_GROUP = "Hospital Demo"
SHOWCASE_PERMISSION = "commercial_pos.view_commercial_showcase"
SAFE_METHODS = {"GET", "HEAD"}
DEMO_ROUTES = {
    "portal:home", "portal:commercial_purchase_orders", "portal:commercial_operations",
    "portal:commercial_finance", "portal:commercial_batch", "portal:commercial_document_file",
    "portal:commercial_purchase_detail", "portal:commercial_operations_detail",
    "portal:showcase_login", "portal:showcase_logout", "set_language",
}
DEMO_POST_ROUTES = {"portal:showcase_login", "portal:showcase_logout", "set_language"}


def is_demo(user):
    if not user.is_authenticated:
        return False
    # Never depend on has_perm / superuser shortcuts for this boundary.
    return user.groups.filter(name=DEMO_GROUP).exists()


def can_view_showcase(user):
    from portal.role_access import portal_role
    if portal_role(user) in {"internal", "conflict"}:
        return False
    return user.is_authenticated and user.is_active and (is_demo(user) or user.has_perm(SHOWCASE_PERMISSION))


def document_matches_batch(document):
    if not document.shipment_batch_id or document.order_id != document.shipment_batch.order_id:
        return False
    if document.document_type == "hospital_invoice":
        source = document.source_data
        if isinstance(source, dict):
            payload = source.get("invoice_data")
            debug = payload.get("debug") if isinstance(payload, dict) else None
            for data in (source, debug):
                if isinstance(data, dict):
                    for key, expected in (("shipment_batch_id", document.shipment_batch_id), ("order_id", document.order_id)):
                        if key in data and data[key] != expected:
                            return False
    return True


def showcase_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url="/portal/login/")
        if not can_view_showcase(request.user):
            return HttpResponseForbidden("Access denied")
        return view(request, *args, **kwargs)
    return wrapped


def home_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        from portal.role_access import hospital_browser, portal_role
        user = request.user
        if is_demo(user):
            if not user.is_active:
                return HttpResponseForbidden("Access denied")
        elif portal_role(user) == "conflict":
            return HttpResponseForbidden("Access denied")
        elif hospital_browser(user):
            if not user.is_active:
                return HttpResponseForbidden("Access denied")
        elif not (user.is_active and user.is_staff):
            return redirect_to_login(request.get_full_path(), login_url="/portal/login/")
        return view(request, *args, **kwargs)
    return wrapped


def showcase_flags(request):
    demo = is_demo(request.user)
    # A display context never needs permission maps or internal navigation data.
    from portal.role_access import portal_navigation, portal_role
    role = portal_role(request.user)
    return {"portal_admin_visible": portal_navigation(request.user, role=role)["show_admin"],
            "is_hospital_demo": demo, "is_hospital_browser": role == "hospital",
            "is_showcase_page": request.path_info.startswith("/portal/commercial/")}
