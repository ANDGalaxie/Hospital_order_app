from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404

from hospitals.models import Hospital


def build_incomplete_hospital_query():
    """
    第一版资料完整度定义。

    只要缺少以下任一内容，就视为资料不完整：
    - 账单地址
    - 默认收货地址
    - 联系方式全部为空
    """
    missing_billing = Q(billing_address="")
    missing_shipping = Q(default_shipping_address="")

    missing_all_contact = (
        Q(contact_name="")
        & Q(phone="")
        & Q(email="")
    )

    return (
        missing_billing
        | missing_shipping
        | missing_all_contact
    )


def build_hospital_list_context(request):
    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "active").strip()
    completeness = (
        request.GET.get("completeness")
        or "all"
    ).strip()

    hospitals = Hospital.objects.all()

    if query:
        hospitals = hospitals.filter(
            Q(name__icontains=query)
            | Q(normalized_name__icontains=query)
            | Q(billing_address__icontains=query)
            | Q(default_shipping_address__icontains=query)
            | Q(contact_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
        )

    if status == "active":
        hospitals = hospitals.filter(is_active=True)
    elif status == "inactive":
        hospitals = hospitals.filter(is_active=False)

    incomplete_query = build_incomplete_hospital_query()

    if completeness == "complete":
        hospitals = hospitals.exclude(incomplete_query)
    elif completeness == "incomplete":
        hospitals = hospitals.filter(incomplete_query)

    hospitals = hospitals.order_by("name")

    paginator = Paginator(hospitals, 25)
    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    query_params = request.GET.copy()
    query_params.pop("page", None)

    all_hospitals = Hospital.objects.all()

    return {
        "page_obj": page_obj,
        "query": query,
        "status": status,
        "completeness": completeness,
        "query_without_page": query_params.urlencode(),

        "total_count": all_hospitals.count(),
        "active_count": all_hospitals.filter(
            is_active=True
        ).count(),
        "inactive_count": all_hospitals.filter(
            is_active=False
        ).count(),
        "missing_billing_count": all_hospitals.filter(
            billing_address=""
        ).count(),
        "incomplete_count": all_hospitals.filter(
            incomplete_query
        ).count(),

        "can_add_hospital": request.user.has_perm(
            "hospitals.add_hospital"
        ),
    }


def build_hospital_detail_context(request, hospital_id):
    hospital = get_object_or_404(
        Hospital,
        id=hospital_id,
    )

    missing_fields = []

    if not hospital.billing_address.strip():
        missing_fields.append("账单地址")

    if not hospital.default_shipping_address.strip():
        missing_fields.append("默认收货地址")

    if not any(
        [
            hospital.contact_name.strip(),
            hospital.phone.strip(),
            hospital.email.strip(),
        ]
    ):
        missing_fields.append("联系方式")

    return {
        "hospital": hospital,
        "missing_fields": missing_fields,
        "can_change_hospital": request.user.has_perm(
            "hospitals.change_hospital"
        ),
    }
