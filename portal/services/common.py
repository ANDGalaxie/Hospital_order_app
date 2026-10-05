from django.utils.translation import get_language
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def get_portal_lang(request):
    """Return the legacy presentation code without maintaining language state."""
    language = (get_language() or "zh-hans").lower()
    if language.startswith("en"):
        return "en"
    if language.startswith("fr"):
        return "fr"
    return "zh"


def get_user_display_name(user):
    return user.get_full_name() or user.username


def safe_count(app_label, model_name):
    try:
        model = apps.get_model(app_label, model_name)
        return model.objects.count()
    except Exception:
        return None


def document_url(document):
    if not document:
        return None

    pdf_file = getattr(document, "pdf_file", None)

    if pdf_file:
        try:
            return pdf_file.url
        except Exception:
            return None

    return None


def file_url_safe(file_field):
    if not file_field:
        return None

    try:
        return file_field.url
    except Exception:
        return None


def json_to_lines(data):
    if not data:
        return []

    if isinstance(data, str):
        return [line.strip() for line in data.splitlines() if line.strip()]

    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]

    if isinstance(data, dict):
        for key in ["lines", "manual_lines", "address_lines"]:
            value = data.get(key)

            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]

            if isinstance(value, str):
                return [line.strip() for line in value.splitlines() if line.strip()]

        raw_text = data.get("raw_text") or data.get("manual_text")

        if raw_text:
            return [line.strip() for line in str(raw_text).splitlines() if line.strip()]

        preferred_keys = [
            "name",
            "hospital_name",
            "line1",
            "line2",
            "line3",
            "street",
            "address",
            "postal_code",
            "city",
            "country",
            "phone",
            "contact",
        ]

        lines = []

        for key in preferred_keys:
            value = data.get(key)
            if value:
                lines.append(str(value))

        if lines:
            return lines

        return [f"{key}: {value}" for key, value in data.items() if value]

    return [str(data)]


def address_data_from_text(text):
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]

    return {
        "source": "portal_manual_edit",
        "status": "manual",
        "lines": lines,
        "raw_text": "\n".join(lines),
    }


def parse_int_value(value, default=0):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def parse_decimal_value(value, default=None):
    value = str(value or "").strip()

    if not value:
        return default

    value = value.replace(",", ".")

    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return default


def get_safe_next_url(request, fallback_url):
    next_url = request.POST.get("next") or request.GET.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url

    return fallback_url


def safe_redirect_after_action(request, fallback_url):
    return redirect(get_safe_next_url(request, fallback_url))
