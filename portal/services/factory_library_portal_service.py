from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse

from factories.models import Factory
from products.models import ProductCategory


def build_incomplete_factory_query():
    """
    第一版工厂资料完整度标准。

    缺少以下任一字段时，视为资料不完整：
    - 工厂地址
    - Buyer
    - 自动匹配关键词
    """

    return (
        Q(address="")
        | Q(buyer="")
        | Q(match_keywords="")
    )


def build_factory_list_context(request):
    query = (request.GET.get("q") or "").strip()
    status = (
        request.GET.get("status")
        or "active"
    ).strip()
    completeness = (
        request.GET.get("completeness")
        or "all"
    ).strip()

    factories = Factory.objects.annotate(
        active_product_count=Count(
            "products",
            filter=Q(products__is_active=True),
            distinct=True,
        ),
        product_node_count=Count(
            "product_category_nodes",
            filter=Q(
                product_category_nodes__node_type=(
                    ProductCategory.NodeType.FACTORY
                ),
                product_category_nodes__is_active=True,
            ),
            distinct=True,
        ),
    )

    if query:
        factories = factories.filter(
            Q(name__icontains=query)
            | Q(legal_name__icontains=query)
            | Q(short_name__icontains=query)
            | Q(address__icontains=query)
            | Q(buyer__icontains=query)
            | Q(match_keywords__icontains=query)
        )

    if status == "active":
        factories = factories.filter(is_active=True)
    elif status == "inactive":
        factories = factories.filter(is_active=False)

    incomplete_query = build_incomplete_factory_query()

    if completeness == "complete":
        factories = factories.exclude(
            incomplete_query
        )
    elif completeness == "incomplete":
        factories = factories.filter(
            incomplete_query
        )

    factories = factories.order_by(
        "short_name",
        "name",
    )

    paginator = Paginator(factories, 12)
    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    query_params = request.GET.copy()
    query_params.pop("page", None)

    all_factories = Factory.objects.all()

    return {
        "page_obj": page_obj,
        "query": query,
        "status": status,
        "completeness": completeness,
        "query_without_page": query_params.urlencode(),

        "total_count": all_factories.count(),
        "active_count": all_factories.filter(
            is_active=True
        ).count(),
        "with_products_count": all_factories.filter(
            products__isnull=False
        ).distinct().count(),
        "incomplete_count": all_factories.filter(
            incomplete_query
        ).count(),

        "can_add_factory": request.user.has_perm(
            "factories.add_factory"
        ),
    }


def build_factory_detail_context(
    request,
    factory_id,
):
    factory = get_object_or_404(
        Factory,
        id=factory_id,
    )

    factory_nodes = (
        ProductCategory.objects.filter(
            factory=factory,
            node_type=ProductCategory.NodeType.FACTORY,
            is_active=True,
        )
        .select_related("parent")
        .order_by(
            "parent__sort_order",
            "parent__name",
            "sort_order",
            "name",
        )
    )

    structures = []
    department_ids = set()
    category_count = 0

    for node in factory_nodes:
        categories = list(
            ProductCategory.objects.filter(
                parent=node,
                node_type=ProductCategory.NodeType.CATEGORY,
                is_active=True,
            )
            .annotate(
                active_product_count=Count(
                    "products",
                    filter=Q(
                        products__is_active=True
                    ),
                    distinct=True,
                )
            )
            .order_by(
                "sort_order",
                "name",
            )
        )

        if node.parent_id:
            department_ids.add(node.parent_id)

        category_count += len(categories)

        structures.append(
            {
                "node": node,
                "department": node.parent,
                "categories": categories,
                "product_library_url": reverse(
                    "portal:library_product_factory",
                    args=[node.id],
                ),
            }
        )

    active_products = factory.products.filter(
        is_active=True
    )

    missing_fields = []

    if not factory.address.strip():
        missing_fields.append("工厂地址")

    if not factory.buyer.strip():
        missing_fields.append(
            "采购联系人 / Buyer"
        )

    if not factory.match_keywords.strip():
        missing_fields.append(
            "自动匹配关键词"
        )

    return {
        "factory": factory,
        "factory_structures": structures,
        "department_count": len(department_ids),
        "category_count": category_count,
        "active_product_count": active_products.count(),
        "inactive_product_count": (
            factory.products.filter(
                is_active=False
            ).count()
        ),
        "missing_fields": missing_fields,
        "can_change_factory": request.user.has_perm(
            "factories.change_factory"
        ),
    }
