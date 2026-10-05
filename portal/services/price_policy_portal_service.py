from django.utils.translation import gettext as _
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from factories.models import Factory
from portal.forms.price_policy_forms import (
    PricePolicySimulationForm,
)
from pricing.models import PricePolicy
from pricing.services.price_policy_service import (
    resolve_price_policy_for_product,
)
from products.models import Product, ProductCategory


MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.01")


def parse_reference_date(value):
    if not value:
        return timezone.localdate()

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()
    except (TypeError, ValueError):
        return timezone.localdate()


def get_policy_portal_status(
    policy,
    reference_date,
):
    if not policy.is_active:
        return {
            "code": "inactive",
            "label": _("已停用"),
            "css_class": "neutral",
        }

    if (
        policy.start_date
        and policy.start_date > reference_date
    ):
        return {
            "code": "future",
            "label": _("未来生效"),
            "css_class": "warning",
        }

    if (
        policy.end_date
        and policy.end_date < reference_date
    ):
        return {
            "code": "expired",
            "label": _("已过期"),
            "css_class": "danger",
        }

    return {
        "code": "current",
        "label": _("生效中"),
        "css_class": "success",
    }


def get_descendant_category_ids(category):
    ids = [category.id]
    current_ids = [category.id]

    while current_ids:
        child_ids = list(
            ProductCategory.objects.filter(
                parent_id__in=current_ids,
            ).values_list(
                "id",
                flat=True,
            )
        )

        if not child_ids:
            break

        ids.extend(child_ids)
        current_ids = child_ids

    return ids


def get_policy_affected_products(policy):
    products = Product.objects.filter(
        is_active=True,
    )

    if policy.category_id:
        category_ids = (
            get_descendant_category_ids(
                policy.category
            )
        )

        products = products.filter(
            category_id__in=category_ids,
        )

    elif policy.factory_id:
        products = products.filter(
            factory=policy.factory,
        )

    return products.select_related(
        "factory",
        "category",
    ).order_by("code")


def decorate_policy_for_portal(
    policy,
    reference_date,
):
    status = get_policy_portal_status(
        policy,
        reference_date,
    )

    policy.portal_status_code = status["code"]
    policy.portal_status_label = status["label"]
    policy.portal_status_class = status[
        "css_class"
    ]

    policy.portal_factory_label = (
        policy.factory.short_name
        or policy.factory.name
        if policy.factory
        else _("全部工厂")
    )

    policy.portal_category_label = (
        policy.category.get_full_path()
        if policy.category
        else _("全部产品分类")
    )

    policy.discount_percent_display = (
        (
            policy.expiration_discount_rate
            or Decimal("0")
        )
        * Decimal("100")
    ).quantize(
        PERCENT_QUANT,
        rounding=ROUND_HALF_UP,
    )

    policy.affected_product_count = (
        get_policy_affected_products(
            policy
        ).count()
    )

    return policy


def build_price_policy_list_context(request):
    query = (
        request.GET.get("q")
        or ""
    ).strip()

    status_filter = (
        request.GET.get("status")
        or "all"
    ).strip()

    factory_id = (
        request.GET.get("factory")
        or ""
    ).strip()

    category_id = (
        request.GET.get("category")
        or ""
    ).strip()

    reference_date = parse_reference_date(
        request.GET.get("on_date")
    )

    policies = (
        PricePolicy.objects.select_related(
            "factory",
            "category",
            "category__parent",
            "category__parent__parent",
        )
        .all()
    )

    if query:
        policies = policies.filter(
            Q(name__icontains=query)
            | Q(factory__name__icontains=query)
            | Q(factory__short_name__icontains=query)
            | Q(category__name__icontains=query)
            | Q(notes__icontains=query)
        )

    if factory_id.isdigit():
        policies = policies.filter(
            factory_id=int(factory_id)
        )

    if category_id.isdigit():
        policies = policies.filter(
            category_id=int(category_id)
        )

    if status_filter == "current":
        policies = (
            policies.filter(is_active=True)
            .filter(
                Q(start_date__isnull=True)
                | Q(
                    start_date__lte=reference_date
                )
            )
            .filter(
                Q(end_date__isnull=True)
                | Q(
                    end_date__gte=reference_date
                )
            )
        )

    elif status_filter == "future":
        policies = policies.filter(
            is_active=True,
            start_date__gt=reference_date,
        )

    elif status_filter == "expired":
        policies = policies.filter(
            is_active=True,
            end_date__lt=reference_date,
        )

    elif status_filter == "inactive":
        policies = policies.filter(
            is_active=False,
        )

    policies = policies.order_by(
        "factory__name",
        "category__name",
        "-start_date",
        "-id",
    )

    paginator = Paginator(
        policies,
        25,
    )

    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    for policy in page_obj.object_list:
        decorate_policy_for_portal(
            policy,
            reference_date,
        )

    all_policies = list(
        PricePolicy.objects.select_related(
            "factory",
            "category",
        )
    )

    status_counts = {
        "current": 0,
        "future": 0,
        "expired": 0,
        "inactive": 0,
    }

    for policy in all_policies:
        status = get_policy_portal_status(
            policy,
            reference_date,
        )

        status_counts[status["code"]] += 1

    query_params = request.GET.copy()
    query_params.pop("page", None)

    categories = (
        ProductCategory.objects.filter(
            node_type=(
                ProductCategory.NodeType.CATEGORY
            ),
            is_active=True,
        )
        .select_related(
            "parent",
            "parent__parent",
        )
        .order_by(
            "parent__parent__name",
            "parent__name",
            "name",
        )
    )

    return {
        "page_obj": page_obj,
        "query": query,
        "status_filter": status_filter,
        "selected_factory_id": factory_id,
        "selected_category_id": category_id,
        "reference_date": reference_date,
        "reference_date_input": (
            reference_date.isoformat()
        ),
        "query_without_page": (
            query_params.urlencode()
        ),

        "total_count": len(all_policies),
        "current_count": status_counts[
            "current"
        ],
        "future_count": status_counts[
            "future"
        ],
        "expired_count": status_counts[
            "expired"
        ],
        "inactive_count": status_counts[
            "inactive"
        ],

        "factories": (
            Factory.objects.filter(
                is_active=True,
            )
            .order_by(
                "short_name",
                "name",
            )
        ),
        "categories": categories,

        "can_add_price_policy": (
            request.user.has_perm(
                "pricing.add_pricepolicy"
            )
        ),
    }


def build_price_policy_detail_context(
    request,
    policy_id,
):
    policy = get_object_or_404(
        PricePolicy.objects.select_related(
            "factory",
            "category",
            "category__parent",
            "category__parent__parent",
        ),
        id=policy_id,
    )

    reference_date = timezone.localdate()

    decorate_policy_for_portal(
        policy,
        reference_date,
    )

    hospital_price = (
        policy.hospital_unit_price
        or Decimal("0")
    )

    factory_price = (
        policy.factory_unit_price
        or Decimal("0")
    )

    discount_rate = (
        policy.expiration_discount_rate
        or Decimal("0")
    )

    base_margin = (
        hospital_price - factory_price
    ).quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )

    if hospital_price > 0:
        base_margin_rate = (
            base_margin
            / hospital_price
            * Decimal("100")
        ).quantize(
            PERCENT_QUANT,
            rounding=ROUND_HALF_UP,
        )
    else:
        base_margin_rate = Decimal("0.00")

    discounted_factory_price = (
        factory_price
        * (
            Decimal("1.00")
            - discount_rate
        )
    ).quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )

    discounted_margin = (
        hospital_price
        - discounted_factory_price
    ).quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )

    affected_products = (
        get_policy_affected_products(policy)
    )

    return {
        "policy": policy,
        "reference_date": reference_date,
        "base_margin": base_margin,
        "base_margin_rate": base_margin_rate,
        "discounted_factory_price": (
            discounted_factory_price
        ),
        "discounted_margin": (
            discounted_margin
        ),
        "affected_product_count": (
            affected_products.count()
        ),
        "affected_products": (
            affected_products[:20]
        ),
        "can_change_price_policy": (
            request.user.has_perm(
                "pricing.change_pricepolicy"
            )
        ),
    }


def build_price_policy_simulator_context(
    request,
):
    today = timezone.localdate()

    form = PricePolicySimulationForm(
        request.GET or None,
        initial_date=today,
    )

    simulation = None

    if form.is_valid():
        product = form.cleaned_data["product"]
        target_date = form.cleaned_data[
            "target_date"
        ]

        resolved = (
            resolve_price_policy_for_product(
                product=product,
                target_date=target_date,
            )
        )

        policy = resolved["policy"]

        simulation = {
            "product": product,
            "target_date": target_date,
            "policy": policy,
            "scope": resolved["scope"],
            "message": resolved["message"],
            "warnings": resolved["warnings"],
        }

        if policy:
            simulation[
                "discount_percent"
            ] = (
                (
                    policy.expiration_discount_rate
                    or Decimal("0")
                )
                * Decimal("100")
            ).quantize(
                PERCENT_QUANT,
                rounding=ROUND_HALF_UP,
            )

            simulation[
                "base_margin"
            ] = (
                policy.hospital_unit_price
                - policy.factory_unit_price
            ).quantize(
                MONEY_QUANT,
                rounding=ROUND_HALF_UP,
            )

    return {
        "form": form,
        "simulation": simulation,
    }
