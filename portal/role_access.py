"""Central module policy for opt-in Portal roles; legacy users remain unchanged."""
from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseForbidden
from django.utils.translation import gettext_lazy as _

HOSPITAL_GROUP = "Portal Hospital Operations"
INTERNAL_GROUP = "Portal Internal Operations"
MODULES = {
    "library": {"title": _("资料库"), "route": "portal:library_home", "icon": "library.png", "theme": "purple"},
    "engagement": {"title": _("医院沟通进度"), "route": "portal:engagement_home", "icon": "hospital-orders.png", "theme": "teal"},
    "orders": {"title": _("医院订单"), "route": "portal:order_list", "icon": "hospital-orders.png", "theme": "teal"},
    "purchase": {"title": _("采购订单"), "route": "portal:commercial_purchase_orders", "icon": "factory-purchase.png", "theme": "green"},
    "operations": {"title": _("操作平台"), "route": "portal:commercial_operations", "icon": "workflow.png", "theme": "violet"},
    "finance": {"title": _("数据与财务"), "route": "portal:commercial_finance", "icon": "finance.png", "theme": "orange"},
    "factory": {"title": _("工厂采购"), "route": "portal:factory_list", "icon": "factory-purchase.png", "theme": "green"},
    "workflow": {"title": _("工作流"), "route": "portal:workflow_list", "icon": "workflow.png", "theme": "violet"},
    "documents": {"title": _("文档中心"), "route": "portal:document_center", "icon": "documents.png", "theme": "blue"},
    "settlements": {"title": _("发票与结算"), "route": "portal:settlement_home", "icon": "invoice.png", "theme": "blue"},
}
ROLE_MODULES = {
    "hospital": ("library", "orders", "purchase", "operations", "finance"),
    "internal": ("library", "orders", "factory", "workflow", "documents", "settlements"),
}
# Account exceptions stay in the existing navigation policy. The role match
# keeps Demo precedence, while the account match leaves other group members
# and their current module/access configuration untouched.
_UNSET_ROLE = object()
ACCOUNT_NAVIGATION = {
    ("internal", "Cynthia"): {
        "modules": ("library", "orders", "engagement", "factory", "workflow", "documents", "settlements"),
        "show_admin": False,
    },
}
BROWSE_PERMISSIONS = frozenset({
    "products.view_product", "products.view_productcategory", "hospitals.view_hospital",
    "factories.view_factory", "orders.view_order", "orders.view_orderitem",
    "commercial_pos.view_commercial_showcase",
})
SHARED_ROUTES = {
    "portal:library_home": ("library", "products.view_product"),
    "portal:library_products": ("library", "products.view_product"),
    "portal:library_product_department": ("library", "products.view_productcategory"),
    "portal:library_product_factory": ("library", "products.view_productcategory"),
    "portal:library_product_category": ("library", "products.view_productcategory"),
    "portal:library_product_detail": ("library", "products.view_product"),
    "portal:library_hospitals": ("library", "hospitals.view_hospital"),
    "portal:library_hospital_detail": ("library", "hospitals.view_hospital"),
    "portal:library_factories": ("library", "factories.view_factory"),
    "portal:library_factory_detail": ("library", "factories.view_factory"),
    "portal:order_list": ("orders", "orders.view_order"),
    "portal:order_detail": ("orders", "orders.view_order"),
    "portal:protected_media": ("orders", "orders.view_order"),
    "portal.media_views.protected_media": ("orders", "orders.view_order"),
}
COMMERCIAL_ROUTES = {
    "portal:commercial_purchase_orders": "purchase", "portal:commercial_purchase_detail": "purchase",
    "portal:commercial_operations": "operations", "portal:commercial_operations_detail": "operations",
    "portal:commercial_finance": "finance", "portal:commercial_batch": "purchase",
    "portal:commercial_document_file": "purchase",
}
AUTH_ROUTES = frozenset({"portal:home", "portal:showcase_login", "portal:showcase_logout", "set_language"})
AUTH_POST_ROUTES = frozenset({"portal:showcase_login", "portal:showcase_logout", "set_language"})


def portal_role(user):
    if not user.is_authenticated:
        return None
    groups = set(user.groups.filter(name__in=[HOSPITAL_GROUP, INTERNAL_GROUP, "Hospital Demo"]).values_list("name", flat=True))
    if "Hospital Demo" in groups:
        return None  # Existing Demo isolation always wins, including superusers.
    roles = [role for role, group in (("hospital", HOSPITAL_GROUP), ("internal", INTERNAL_GROUP)) if group in groups]
    return roles[0] if len(roles) == 1 else ("conflict" if roles else None)


def portal_navigation(user, *, role=_UNSET_ROLE):
    if role is _UNSET_ROLE:
        role = portal_role(user)
    key = (role, user.get_username()) if user.is_authenticated else None
    override = ACCOUNT_NAVIGATION.get(key, {})
    return {"modules": override.get("modules", ROLE_MODULES.get(role, ())),
            "show_admin": override.get("show_admin", True)}


def hospital_browser(user):
    return portal_role(user) == "hospital"


def role_route_allowed(user, route, method):
    role = portal_role(user)
    if role is None:
        return True
    if role == "conflict" or not user.is_active:
        return False
    if role == "internal":
        # Keep original business operation permissions; deny showcase and
        # identity/session administration that could bypass these role boundaries,
        # regardless of pre-existing permissions or superuser flags.
        return not route.startswith(("portal:commercial_", "admin:commercial_pos_", "admin:auth_", "admin:sessions_"))
    if route in AUTH_ROUTES:
        return method in ("GET", "HEAD") or (method == "POST" and route in AUTH_POST_ROUTES)
    if method not in ("GET", "HEAD"):
        return False
    if route in SHARED_ROUTES:
        module, permission = SHARED_ROUTES[route]
    elif route in COMMERCIAL_ROUTES:
        module, permission = COMMERCIAL_ROUTES[route], "commercial_pos.view_commercial_showcase"
    else:
        return False
    return module in ROLE_MODULES[role] and user.has_perm(permission)


def shared_browse_required(view):
    """Hospital readers get safe DTOs, never the original staff view/context."""
    staff_view = staff_member_required(view)

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if hospital_browser(request.user):
            route = request.resolver_match.view_name
            if not role_route_allowed(request.user, route, request.method):
                return HttpResponseForbidden("Access denied")
            from portal.hospital_browse_views import shared_read_page
            return shared_read_page(request, **kwargs)
        return staff_view(request, *args, **kwargs)
    return wrapped


def media_browse_required(view):
    staff_view = staff_member_required(view)

    @wraps(view)
    def wrapped(request, path):
        role = portal_role(request.user)
        if role not in {"hospital", "internal"}:
            return staff_view(request, path)
        from pathlib import Path
        from django.conf import settings
        from documents.models import GeneratedDocument
        try:
            media_root = Path(settings.MEDIA_ROOT).resolve()
            candidate = (media_root / path).resolve()
            relative = candidate.relative_to(media_root)
        except (OSError, RuntimeError, ValueError):
            return HttpResponseForbidden("Access denied")
        # Block unpublished/old revisions too, including normalized paths and
        # aliases. A source order PDF must never alias a generated document.
        if relative.parts and relative.parts[0] == "commercial_purchase_orders":
            return HttpResponseForbidden("Access denied")
        documents = GeneratedDocument.objects.all()
        if role == "internal":
            documents = documents.filter(document_type="commercial_po")
        for pdf, html in documents.values_list("pdf_file", "html_file"):
            if any(name and candidate == (media_root / name).resolve() for name in (pdf, html)):
                return HttpResponseForbidden("Access denied")
        if role == "hospital":
            from orders.models import Order
            if (not request.user.is_active or request.method not in ("GET", "HEAD")
                    or not request.user.has_perm("orders.view_order")
                    or candidate.suffix.lower() != ".pdf"
                    or not Order.objects.filter(hospital_order_pdf=path).exists()):
                return HttpResponseForbidden("Access denied")
            return view(request, path)
        return staff_view(request, path)
    return wrapped
