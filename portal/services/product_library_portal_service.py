from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse

from products.models import Product, ProductCategory


def get_descendant_category_ids(node):
    """
    获取某个 ProductCategory 节点下面的全部后代节点 ID。

    这样以后即使分类树多一层，也不需要重写产品数量统计。
    """
    descendant_ids = []
    current_level_ids = [node.id]

    while current_level_ids:
        child_ids = list(
            ProductCategory.objects.filter(
                parent_id__in=current_level_ids,
                is_active=True,
            ).values_list("id", flat=True)
        )

        if not child_ids:
            break

        descendant_ids.extend(child_ids)
        current_level_ids = child_ids

    return descendant_ids


def count_active_products_under_node(node):
    """
    统计一个分类节点及其所有子节点下的启用产品数量。
    """
    category_ids = [node.id]
    category_ids.extend(get_descendant_category_ids(node))

    return Product.objects.filter(
        category_id__in=category_ids,
        is_active=True,
    ).count()


def build_product_library_home_context(request):
    """
    产品库首页：

    - 显示所有一级科室
    - 显示每个科室的工厂、分类和产品数量
    - 支持全局搜索产品编号、描述、工厂和分类
    """
    departments = ProductCategory.objects.filter(
        node_type=ProductCategory.NodeType.DEPARTMENT,
        parent__isnull=True,
        is_active=True,
    ).order_by("sort_order", "name")

    department_cards = []

    for department in departments:
        factory_nodes = department.children.filter(
            node_type=ProductCategory.NodeType.FACTORY,
            is_active=True,
        )

        descendant_ids = get_descendant_category_ids(department)

        category_count = ProductCategory.objects.filter(
            id__in=descendant_ids,
            node_type=ProductCategory.NodeType.CATEGORY,
            is_active=True,
        ).count()

        department_cards.append(
            {
                "id": department.id,
                "name": department.name,
                "notes": department.notes,
                "factory_count": factory_nodes.count(),
                "category_count": category_count,
                "product_count": count_active_products_under_node(department),
                "url": reverse(
                    "portal:library_product_department",
                    args=[department.id],
                ),
            }
        )

    query = (request.GET.get("q") or "").strip()
    search_results = []

    if query:
        search_results = list(
            Product.objects.filter(
                Q(code__icontains=query)
                | Q(description__icontains=query)
                | Q(factory__name__icontains=query)
                | Q(factory__short_name__icontains=query)
                | Q(category__name__icontains=query),
                is_active=True,
            )
            .select_related(
                "category",
                "category__parent",
                "category__parent__parent",
                "factory",
            )
            .order_by("code")[:100]
        )

    return {
        "departments": department_cards,
        "query": query,
        "search_results": search_results,
        "can_add_category": request.user.has_perm(
            "products.add_productcategory"
        ),
    }


def build_product_department_context(request, department_id):
    """
    科室页：显示这个科室下的全部工厂入口。
    """
    department = get_object_or_404(
        ProductCategory,
        id=department_id,
        node_type=ProductCategory.NodeType.DEPARTMENT,
        is_active=True,
    )

    factory_nodes = (
        ProductCategory.objects.filter(
            parent=department,
            node_type=ProductCategory.NodeType.FACTORY,
            is_active=True,
        )
        .select_related("factory")
        .order_by("sort_order", "name")
    )

    factory_cards = []

    for node in factory_nodes:
        factory_cards.append(
            {
                "id": node.id,
                "name": node.name,
                "factory": node.factory,
                "category_count": node.children.filter(
                    node_type=ProductCategory.NodeType.CATEGORY,
                    is_active=True,
                ).count(),
                "product_count": count_active_products_under_node(node),
                "url": reverse(
                    "portal:library_product_factory",
                    args=[node.id],
                ),
            }
        )

    return {
        "department": department,
        "factory_cards": factory_cards,
        "can_add_category": request.user.has_perm(
            "products.add_productcategory"
        ),
    }


def build_product_factory_context(request, factory_node_id):
    """
    工厂页：显示该工厂在当前科室下的全部产品分类。
    """
    factory_node = get_object_or_404(
        ProductCategory.objects.select_related(
            "parent",
            "factory",
        ),
        id=factory_node_id,
        node_type=ProductCategory.NodeType.FACTORY,
        is_active=True,
    )

    categories = ProductCategory.objects.filter(
        parent=factory_node,
        node_type=ProductCategory.NodeType.CATEGORY,
        is_active=True,
    ).order_by("sort_order", "name")

    category_cards = []

    for category in categories:
        category_cards.append(
            {
                "id": category.id,
                "name": category.name,
                "notes": category.notes,
                "product_count": count_active_products_under_node(category),
                "url": reverse(
                    "portal:library_product_category",
                    args=[category.id],
                ),
            }
        )

    return {
        "department": factory_node.parent,
        "factory_node": factory_node,
        "category_cards": category_cards,
        "can_add_category": request.user.has_perm(
            "products.add_productcategory"
        ),
    }


def build_product_category_context(request, category_id):
    """
    产品分类页：显示该分类下的产品编号列表。
    """
    category = get_object_or_404(
        ProductCategory.objects.select_related(
            "parent",
            "parent__parent",
            "parent__factory",
        ),
        id=category_id,
        node_type=ProductCategory.NodeType.CATEGORY,
        is_active=True,
    )

    query = (request.GET.get("q") or "").strip()

    products = Product.objects.filter(
        category=category,
        is_active=True,
    ).select_related(
        "factory",
        "category",
    )

    if query:
        products = products.filter(
            Q(code__icontains=query)
            | Q(description__icontains=query)
        )

    products = products.order_by("code")

    return {
        "category": category,
        "factory_node": category.parent,
        "department": (
            category.parent.parent
            if category.parent
            else None
        ),
        "query": query,
        "products": products,
        "product_count": products.count(),
        "can_add_product": request.user.has_perm(
            "products.add_product"
        ),
    }


def build_product_detail_context(request, product_id):
    """
    单个产品详情页。
    """
    product = get_object_or_404(
        Product.objects.select_related(
            "category",
            "category__parent",
            "category__parent__parent",
            "category__parent__factory",
            "factory",
        ),
        id=product_id,
    )

    category = product.category
    factory_node = category.parent if category else None
    department = (
        factory_node.parent
        if factory_node
        else None
    )

    return {
        "product": product,
        "category": category,
        "factory_node": factory_node,
        "department": department,
    }
