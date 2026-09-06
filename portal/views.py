"""
Portal views.

这个文件只负责接收 HTTP 请求、调用 service 层、返回页面或跳转。
业务逻辑尽量放在 portal/services/* 或各 app 的 services/* 里，避免 views.py 变得过重。
"""

from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from portal.forms.factory_library_forms import (
    FactoryPortalForm,
)
from portal.forms.hospital_library_forms import (
    HospitalPortalForm,
)
from portal.forms.price_policy_forms import (
    PricePolicyPortalForm,
)
from portal.forms.product_library_forms import (
    DepartmentCreateForm,
    FactoryNodeCreateForm,
    ProductCategoryCreateForm,
    ProductCreateForm,
)

from portal.services.common import (
    get_safe_next_url,
    safe_redirect_after_action,
)
from portal.services.factory_portal_service import (
    associate_order_and_finalize_factory_confirmation,
    build_factory_detail_context,
    build_factory_list_context,
    build_factory_upload_context,
    create_and_extract_factory_confirmation,
    reextract_factory_confirmation_for_portal,
    safely_delete_factory_confirmation_for_portal,
    save_factory_serial_manual_edit,
)
from portal.services.home_portal_service import build_home_context
from portal.services.order_portal_service import (
    build_order_detail_context,
    build_order_list_context,
    build_order_upload_context,
    create_order_for_upload,
    generate_factory_request_for_order,
    run_order_extraction,
    save_order_manual_edit,
    validate_portal_order_after_extraction,
)
from portal.services.shipment_portal_service import (
    build_shipment_detail_context,
    build_shipment_list_context,
)
from portal.services.backorder_export_service import build_backorder_xlsx
from portal.services.backorder_portal_service import (
    build_backorder_detail_context,
    build_backorder_list_context,
    build_backorder_queryset,
    create_inventory_shipment_for_allocation,
    reserve_inventory_for_backorder,
)
from portal.services.workflow_portal_service import (
    build_workflow_detail_context,
    build_workflow_list_context,
    get_order_number,
)

from portal.services.library_portal_service import build_library_home_context
from portal.services.price_policy_portal_service import (
    build_price_policy_detail_context,
    build_price_policy_list_context,
    build_price_policy_simulator_context,
)
from portal.services.factory_library_portal_service import (
    build_factory_detail_context as build_factory_library_detail_context,
    build_factory_list_context as build_factory_library_list_context,
)
from portal.services.hospital_library_portal_service import (
    build_hospital_detail_context,
    build_hospital_list_context,
)
from portal.services.product_library_portal_service import (
    build_product_category_context,
    build_product_department_context,
    build_product_detail_context,
    build_product_factory_context,
    build_product_library_home_context,
)

# =============================================================================
# 通用小工具
# =============================================================================


def _short_text(value, limit=300):
    """把异常或提示文字裁短，避免 messages 区域过长。"""
    text = str(value or "")
    return text[:limit]


def _get_factory_workflow_info(confirmation):
    """
    从 FactoryConfirmation.extracted_confirmation_data['django'] 中读取 workflow 结果。

    factory_confirmation_extraction_service 会在成功同步 workflow 后写入：
        workflow_item_id
        workflow_validation_result
        workflow_validation_status
        workflow_status

    这里统一做容错，避免 JSON 结构为空或类型不符合预期时报错。
    """
    data = confirmation.extracted_confirmation_data or {}
    django_data = data.get("django") or {}

    workflow_item_id = django_data.get("workflow_item_id")
    validation_result = django_data.get("workflow_validation_result") or {}

    if not isinstance(validation_result, dict):
        validation_result = {}

    errors = validation_result.get("errors") or []
    warnings = validation_result.get("warnings") or []

    return {
        "workflow_item_id": workflow_item_id,
        "errors": errors,
        "warnings": warnings,
        "validation_result": validation_result,
    }


def _redirect_after_factory_processing(request, confirmation, success, message_text=""):
    """
    工厂采购上传/重新提取后的统一跳转逻辑。

    规则：
      1. 如果提取/同步失败：留在工厂采购详情页。
      2. 如果没有进入 workflow：留在工厂采购详情页。
      3. 如果进入 workflow 但有 errors/warnings：留在工厂采购详情页，提示检查。
      4. 如果进入 workflow 且没有 errors/warnings：直接跳到 workflow 详情页。
    """
    workflow_info = _get_factory_workflow_info(confirmation)
    workflow_item_id = workflow_info["workflow_item_id"]
    errors = workflow_info["errors"]
    warnings = workflow_info["warnings"]

    if success and workflow_item_id and not errors and not warnings:
        messages.success(
            request,
            message_text or "工厂采购文件已上传、自动提取并进入工作流。",
        )
        return redirect("portal:workflow_list")

    if success and workflow_item_id:
        messages.warning(
            request,
            (
                "工厂采购文件已进入工作流，但验证发现需要检查的问题："
                f"错误 {len(errors)} 个，提醒 {len(warnings)} 个。"
            ),
        )
        return redirect("portal:factory_detail", confirmation_id=confirmation.id)

    if success:
        messages.warning(
            request,
            message_text or "工厂采购文件已提取成功，但没有成功进入工作流，请检查详情。",
        )
        return redirect("portal:factory_detail", confirmation_id=confirmation.id)

    messages.warning(
        request,
        f"工厂采购文件已上传，但需要检查：{_short_text(message_text)}",
    )
    return redirect("portal:factory_detail", confirmation_id=confirmation.id)


# =============================================================================
# 首页
# =============================================================================


@staff_member_required
def home(request):
    return render(request, "portal/home.html", build_home_context(request))

@staff_member_required
def library_home(request):
    """
    资料库首页。

    页面风格应和 portal 首页保持一致：
    使用同样的卡片布局，只是卡片内容换成资料库模块。
    """
    return render(
        request,
        "portal/library/home.html",
        build_library_home_context(request),
    )


@staff_member_required
def library_products(request):
    return render(
        request,
        "portal/library/products/home.html",
        build_product_library_home_context(request),
    )


@staff_member_required
def library_product_department(request, department_id):
    return render(
        request,
        "portal/library/products/department_detail.html",
        build_product_department_context(
            request,
            department_id,
        ),
    )


@staff_member_required
def library_product_factory(request, factory_node_id):
    return render(
        request,
        "portal/library/products/factory_detail.html",
        build_product_factory_context(
            request,
            factory_node_id,
        ),
    )


@staff_member_required
def library_product_category(request, category_id):
    return render(
        request,
        "portal/library/products/category_detail.html",
        build_product_category_context(
            request,
            category_id,
        ),
    )


@staff_member_required
def library_product_detail(request, product_id):
    return render(
        request,
        "portal/library/products/product_detail.html",
        build_product_detail_context(
            request,
            product_id,
        ),
    )


@staff_member_required
@permission_required(
    "products.add_productcategory",
    raise_exception=True,
)
def library_product_department_add(request):
    form = DepartmentCreateForm(
        request.POST or None,
    )

    if request.method == "POST" and form.is_valid():
        department = form.save()

        messages.success(
            request,
            f"科室“{department.name}”已创建。",
        )

        return redirect(
            "portal:library_product_department",
            department_id=department.id,
        )

    return render(
        request,
        "portal/library/products/form.html",
        {
            "form": form,
            "form_title": "新增科室",
            "form_description": (
                "创建产品库的一级科室入口。"
            ),
            "submit_text": "创建科室",
            "cancel_url": reverse(
                "portal:library_products"
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "产品库",
                    "url": reverse(
                        "portal:library_products"
                    ),
                },
                {
                    "label": "新增科室",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "products.add_productcategory",
    raise_exception=True,
)
def library_product_factory_add(
    request,
    department_id,
):
    from products.models import ProductCategory

    department = get_object_or_404(
        ProductCategory,
        id=department_id,
        node_type=ProductCategory.NodeType.DEPARTMENT,
        is_active=True,
    )

    form = FactoryNodeCreateForm(
        request.POST or None,
        department=department,
        allow_create_factory=request.user.has_perm(
            "factories.add_factory"
        ),
    )

    if request.method == "POST" and form.is_valid():
        factory_node = form.save()

        messages.success(
            request,
            f"工厂“{factory_node.name}”已添加到"
            f"“{department.name}”。",
        )

        return redirect(
            "portal:library_product_factory",
            factory_node_id=factory_node.id,
        )

    return render(
        request,
        "portal/library/products/form.html",
        {
            "form": form,
            "form_title": "新增或关联工厂",
            "form_description": (
                f"将工厂添加到科室“{department.name}”。"
            ),
            "submit_text": "保存工厂",
            "cancel_url": reverse(
                "portal:library_product_department",
                args=[department.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "产品库",
                    "url": reverse(
                        "portal:library_products"
                    ),
                },
                {
                    "label": department.name,
                    "url": reverse(
                        "portal:library_product_department",
                        args=[department.id],
                    ),
                },
                {
                    "label": "新增工厂",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "products.add_productcategory",
    raise_exception=True,
)
def library_product_category_add(
    request,
    factory_node_id,
):
    from products.models import ProductCategory

    factory_node = get_object_or_404(
        ProductCategory.objects.select_related(
            "parent",
            "factory",
        ),
        id=factory_node_id,
        node_type=ProductCategory.NodeType.FACTORY,
        is_active=True,
    )

    form = ProductCategoryCreateForm(
        request.POST or None,
        factory_node=factory_node,
    )

    if request.method == "POST" and form.is_valid():
        category = form.save()

        messages.success(
            request,
            f"产品分类“{category.name}”已创建。",
        )

        return redirect(
            "portal:library_product_category",
            category_id=category.id,
        )

    return render(
        request,
        "portal/library/products/form.html",
        {
            "form": form,
            "form_title": "新增产品分类",
            "form_description": (
                f"为工厂“{factory_node.name}”"
                "创建一个产品分类。"
            ),
            "submit_text": "创建分类",
            "cancel_url": reverse(
                "portal:library_product_factory",
                args=[factory_node.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "产品库",
                    "url": reverse(
                        "portal:library_products"
                    ),
                },
                {
                    "label": factory_node.parent.name,
                    "url": reverse(
                        "portal:library_product_department",
                        args=[factory_node.parent.id],
                    ),
                },
                {
                    "label": factory_node.name,
                    "url": reverse(
                        "portal:library_product_factory",
                        args=[factory_node.id],
                    ),
                },
                {
                    "label": "新增产品分类",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "products.add_product",
    raise_exception=True,
)
def library_product_add(
    request,
    category_id,
):
    from products.models import ProductCategory

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

    form = ProductCreateForm(
        request.POST or None,
        category=category,
    )

    if request.method == "POST" and form.is_valid():
        product = form.save()

        messages.success(
            request,
            f"产品“{product.code}”已创建。",
        )

        return redirect(
            "portal:library_product_detail",
            product_id=product.id,
        )

    return render(
        request,
        "portal/library/products/form.html",
        {
            "form": form,
            "form_title": "新增产品",
            "form_description": (
                f"在分类“{category.name}”中新增产品。"
            ),
            "submit_text": "创建产品",
            "cancel_url": reverse(
                "portal:library_product_category",
                args=[category.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "产品库",
                    "url": reverse(
                        "portal:library_products"
                    ),
                },
                {
                    "label": category.parent.parent.name,
                    "url": reverse(
                        "portal:library_product_department",
                        args=[
                            category.parent.parent.id
                        ],
                    ),
                },
                {
                    "label": category.parent.name,
                    "url": reverse(
                        "portal:library_product_factory",
                        args=[category.parent.id],
                    ),
                },
                {
                    "label": category.name,
                    "url": reverse(
                        "portal:library_product_category",
                        args=[category.id],
                    ),
                },
                {
                    "label": "新增产品",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
def library_hospitals(request):
    return render(
        request,
        "portal/library/hospitals/list.html",
        build_hospital_list_context(request),
    )


@staff_member_required
def library_hospital_detail(request, hospital_id):
    return render(
        request,
        "portal/library/hospitals/detail.html",
        build_hospital_detail_context(
            request,
            hospital_id,
        ),
    )


@staff_member_required
@permission_required(
    "hospitals.add_hospital",
    raise_exception=True,
)
def library_hospital_add(request):
    form = HospitalPortalForm(
        request.POST or None,
    )

    if request.method == "POST" and form.is_valid():
        hospital = form.save()

        messages.success(
            request,
            f"医院“{hospital.name}”已创建。",
        )

        return redirect(
            "portal:library_hospital_detail",
            hospital_id=hospital.id,
        )

    return render(
        request,
        "portal/library/master_data_form.html",
        {
            "form": form,
            "form_title": "新增医院",
            "form_description": (
                "创建新的医院主数据记录。"
            ),
            "submit_text": "创建医院",
            "cancel_url": reverse(
                "portal:library_hospitals"
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "医院库",
                    "url": reverse(
                        "portal:library_hospitals"
                    ),
                },
                {
                    "label": "新增医院",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "hospitals.change_hospital",
    raise_exception=True,
)
def library_hospital_edit(request, hospital_id):
    from hospitals.models import Hospital

    hospital = get_object_or_404(
        Hospital,
        id=hospital_id,
    )

    form = HospitalPortalForm(
        request.POST or None,
        instance=hospital,
    )

    if request.method == "POST" and form.is_valid():
        hospital = form.save()

        messages.success(
            request,
            f"医院“{hospital.name}”已更新。",
        )

        return redirect(
            "portal:library_hospital_detail",
            hospital_id=hospital.id,
        )

    return render(
        request,
        "portal/library/master_data_form.html",
        {
            "form": form,
            "form_title": "编辑医院",
            "form_description": hospital.name,
            "submit_text": "保存修改",
            "cancel_url": reverse(
                "portal:library_hospital_detail",
                args=[hospital.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "医院库",
                    "url": reverse(
                        "portal:library_hospitals"
                    ),
                },
                {
                    "label": hospital.name,
                    "url": reverse(
                        "portal:library_hospital_detail",
                        args=[hospital.id],
                    ),
                },
                {
                    "label": "编辑",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "hospitals.change_hospital",
    raise_exception=True,
)
def library_hospital_toggle_active(
    request,
    hospital_id,
):
    from hospitals.models import Hospital

    hospital = get_object_or_404(
        Hospital,
        id=hospital_id,
    )

    if request.method != "POST":
        return redirect(
            "portal:library_hospital_detail",
            hospital_id=hospital.id,
        )

    hospital.is_active = not hospital.is_active
    hospital.save(
        update_fields=[
            "is_active",
            "updated_at",
        ]
    )

    if hospital.is_active:
        messages.success(
            request,
            f"医院“{hospital.name}”已重新启用。",
        )
    else:
        messages.warning(
            request,
            f"医院“{hospital.name}”已停用。"
            "历史订单不会被删除。",
        )

    return redirect(
        "portal:library_hospital_detail",
        hospital_id=hospital.id,
    )


@staff_member_required
def library_factories(request):
    return render(
        request,
        "portal/library/factories/list.html",
        build_factory_library_list_context(request),
    )


@staff_member_required
def library_factory_detail(request, factory_id):
    return render(
        request,
        "portal/library/factories/detail.html",
        build_factory_library_detail_context(
            request,
            factory_id,
        ),
    )


@staff_member_required
@permission_required(
    "factories.add_factory",
    raise_exception=True,
)
def library_factory_add(request):
    form = FactoryPortalForm(
        request.POST or None,
    )

    if request.method == "POST" and form.is_valid():
        factory = form.save()

        messages.success(
            request,
            f"工厂“{factory}”已创建。",
        )

        return redirect(
            "portal:library_factory_detail",
            factory_id=factory.id,
        )

    return render(
        request,
        "portal/library/master_data_form.html",
        {
            "form": form,
            "form_title": "新增工厂",
            "form_description": (
                "创建新的工厂主数据记录。"
            ),
            "submit_text": "创建工厂",
            "cancel_url": reverse(
                "portal:library_factories"
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "工厂库",
                    "url": reverse(
                        "portal:library_factories"
                    ),
                },
                {
                    "label": "新增工厂",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "factories.change_factory",
    raise_exception=True,
)
def library_factory_edit(request, factory_id):
    from factories.models import Factory

    factory = get_object_or_404(
        Factory,
        id=factory_id,
    )

    form = FactoryPortalForm(
        request.POST or None,
        instance=factory,
    )

    if request.method == "POST" and form.is_valid():
        factory = form.save()

        messages.success(
            request,
            f"工厂“{factory}”已更新。",
        )

        return redirect(
            "portal:library_factory_detail",
            factory_id=factory.id,
        )

    return render(
        request,
        "portal/library/master_data_form.html",
        {
            "form": form,
            "form_title": "编辑工厂",
            "form_description": str(factory),
            "submit_text": "保存修改",
            "cancel_url": reverse(
                "portal:library_factory_detail",
                args=[factory.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse("portal:home"),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "工厂库",
                    "url": reverse(
                        "portal:library_factories"
                    ),
                },
                {
                    "label": str(factory),
                    "url": reverse(
                        "portal:library_factory_detail",
                        args=[factory.id],
                    ),
                },
                {
                    "label": "编辑",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "factories.change_factory",
    raise_exception=True,
)
def library_factory_toggle_active(
    request,
    factory_id,
):
    from factories.models import Factory

    factory = get_object_or_404(
        Factory,
        id=factory_id,
    )

    if request.method != "POST":
        return redirect(
            "portal:library_factory_detail",
            factory_id=factory.id,
        )

    factory.is_active = not factory.is_active
    factory.save(
        update_fields=[
            "is_active",
            "updated_at",
        ]
    )

    if factory.is_active:
        messages.success(
            request,
            f"工厂“{factory}”已重新启用。",
        )
    else:
        messages.warning(
            request,
            f"工厂“{factory}”已停用。"
            "已有产品和历史业务记录不会被删除。",
        )

    return redirect(
        "portal:library_factory_detail",
        factory_id=factory.id,
    )


@staff_member_required
def library_prices(request):
    return render(
        request,
        "portal/library/prices/list.html",
        build_price_policy_list_context(
            request
        ),
    )


@staff_member_required
def library_price_policy_detail(
    request,
    policy_id,
):
    return render(
        request,
        "portal/library/prices/detail.html",
        build_price_policy_detail_context(
            request,
            policy_id,
        ),
    )


@staff_member_required
@permission_required(
    "pricing.add_pricepolicy",
    raise_exception=True,
)
def library_price_policy_add(request):
    form = PricePolicyPortalForm(
        request.POST or None,
    )

    if (
        request.method == "POST"
        and form.is_valid()
    ):
        policy = form.save()

        messages.success(
            request,
            f"价格规则“{policy.name or policy}”已创建。",
        )

        return redirect(
            "portal:library_price_policy_detail",
            policy_id=policy.id,
        )

    return render(
        request,
        "portal/library/prices/form.html",
        {
            "form": form,
            "form_title": "新增价格规则",
            "form_description": (
                "按工厂、产品分类和医院订单日期"
                "定义价格。"
            ),
            "submit_text": "创建规则",
            "cancel_url": reverse(
                "portal:library_prices"
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse(
                        "portal:home"
                    ),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "价格规则",
                    "url": reverse(
                        "portal:library_prices"
                    ),
                },
                {
                    "label": "新增规则",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "pricing.change_pricepolicy",
    raise_exception=True,
)
def library_price_policy_edit(
    request,
    policy_id,
):
    from pricing.models import PricePolicy

    policy = get_object_or_404(
        PricePolicy,
        id=policy_id,
    )

    form = PricePolicyPortalForm(
        request.POST or None,
        instance=policy,
    )

    if (
        request.method == "POST"
        and form.is_valid()
    ):
        policy = form.save()

        messages.success(
            request,
            f"价格规则“{policy.name or policy}”已更新。",
        )

        return redirect(
            "portal:library_price_policy_detail",
            policy_id=policy.id,
        )

    return render(
        request,
        "portal/library/prices/form.html",
        {
            "form": form,
            "form_title": "编辑价格规则",
            "form_description": (
                policy.name or str(policy)
            ),
            "submit_text": "保存修改",
            "cancel_url": reverse(
                "portal:library_price_policy_detail",
                args=[policy.id],
            ),
            "breadcrumbs": [
                {
                    "label": "首页",
                    "url": reverse(
                        "portal:home"
                    ),
                },
                {
                    "label": "资料库",
                    "url": reverse(
                        "portal:library_home"
                    ),
                },
                {
                    "label": "价格规则",
                    "url": reverse(
                        "portal:library_prices"
                    ),
                },
                {
                    "label": (
                        policy.name
                        or f"规则 #{policy.id}"
                    ),
                    "url": reverse(
                        "portal:library_price_policy_detail",
                        args=[policy.id],
                    ),
                },
                {
                    "label": "编辑",
                    "url": "",
                },
            ],
        },
    )


@staff_member_required
@permission_required(
    "pricing.change_pricepolicy",
    raise_exception=True,
)
def library_price_policy_toggle_active(
    request,
    policy_id,
):
    from django.core.exceptions import ValidationError
    from pricing.models import PricePolicy

    policy = get_object_or_404(
        PricePolicy,
        id=policy_id,
    )

    if request.method != "POST":
        return redirect(
            "portal:library_price_policy_detail",
            policy_id=policy.id,
        )

    old_state = policy.is_active
    policy.is_active = not old_state

    try:
        policy.full_clean()

    except ValidationError as exc:
        policy.is_active = old_state

        messages.error(
            request,
            "无法重新启用该规则："
            + "；".join(exc.messages),
        )

        return redirect(
            "portal:library_price_policy_detail",
            policy_id=policy.id,
        )

    policy.save(
        update_fields=[
            "is_active",
            "updated_at",
        ]
    )

    if policy.is_active:
        messages.success(
            request,
            f"价格规则“{policy.name or policy}”已重新启用。",
        )
    else:
        messages.warning(
            request,
            f"价格规则“{policy.name or policy}”已停用。"
            "历史订单价格快照不会改变。",
        )

    return redirect(
        "portal:library_price_policy_detail",
        policy_id=policy.id,
    )


@staff_member_required
def library_price_policy_simulator(
    request,
):
    return render(
        request,
        "portal/library/prices/simulator.html",
        build_price_policy_simulator_context(
            request
        ),
    )


# =============================================================================
# Document Center / 文档中心
# =============================================================================


@staff_member_required
def document_center(request):
    from portal.services.document_center_portal_service import (
        build_document_center_home_context,
    )

    return render(
        request,
        "portal/documents/home.html",
        build_document_center_home_context(
            request
        ),
    )


@staff_member_required
def document_list(request):
    from portal.services.document_center_portal_service import (
        build_document_list_context,
    )

    return render(
        request,
        "portal/documents/list.html",
        build_document_list_context(
            request
        ),
    )


@staff_member_required
def document_invoices(request):
    from documents.models import (
        GeneratedDocument,
    )
    from portal.services.document_center_portal_service import (
        build_document_list_context,
    )

    return render(
        request,
        "portal/documents/list.html",
        build_document_list_context(
            request,
            forced_document_type=(
                GeneratedDocument
                .DocumentType
                .HOSPITAL_INVOICE
            ),
        ),
    )


@staff_member_required
def document_factory_pos(request):
    from documents.models import (
        GeneratedDocument,
    )
    from portal.services.document_center_portal_service import (
        build_document_list_context,
    )

    return render(
        request,
        "portal/documents/list.html",
        build_document_list_context(
            request,
            forced_document_type=(
                GeneratedDocument
                .DocumentType
                .FACTORY_PO
            ),
        ),
    )


@staff_member_required
def document_factory_requests(request):
    from documents.models import (
        GeneratedDocument,
    )
    from portal.services.document_center_portal_service import (
        build_document_list_context,
    )

    return render(
        request,
        "portal/documents/list.html",
        build_document_list_context(
            request,
            forced_document_type=(
                GeneratedDocument
                .DocumentType
                .FACTORY_ORDER_REQUEST
            ),
        ),
    )


@staff_member_required
def document_detail(
    request,
    document_id,
):
    from portal.services.document_center_portal_service import (
        build_document_detail_context,
    )

    return render(
        request,
        "portal/documents/detail.html",
        build_document_detail_context(
            request,
            document_id,
        ),
    )


# =============================================================================
# Workflow / 出单流程
# =============================================================================


@staff_member_required
def workflow_list(request):
    return render(
        request,
        "portal/workflow/list.html",
        build_workflow_list_context(request),
    )


@staff_member_required
def workflow_detail(request, item_id):
    return render(
        request,
        "portal/workflow/detail.html",
        build_workflow_detail_context(request, item_id),
    )


@staff_member_required
def workflow_item_action(request, item_id):
    """Workflow 详情页操作：重新验证、生成 Invoice/PO。"""
    from workflow.models import DocumentWorkflowItem
    from workflow.services.workflow_document_generation_service import (
        generate_documents_for_workflow_items,
    )
    from workflow.services.workflow_validation_service import (
        validate_document_workflow_items,
    )
    from workflow.services.workflow_price_policy_service import (
        reapply_prices_and_validate_workflow_item,
    )

    item = get_object_or_404(
        DocumentWorkflowItem.objects.select_related("order", "shipment_batch"),
        id=item_id,
    )

    next_url = get_safe_next_url(
        request,
        reverse("portal:workflow_list"),
    )

    if request.method != "POST":
        return redirect(next_url)

    action = request.POST.get("action")
    queryset = DocumentWorkflowItem.objects.filter(id=item.id)

    order_number = get_order_number(item)
    batch_number = (item.validation_data or {}).get("batch_number") or getattr(
        item.shipment_batch,
        "batch_number",
        "-",
    )

    if action == "reapply_prices":
        try:
            result = (
                reapply_prices_and_validate_workflow_item(
                    item
                )
            )

            price_result = (
                result.get("price_result")
                or {}
            )

            validation_result = (
                result.get("validation_result")
                or {}
            )

            updated_count = price_result.get(
                "updated_count",
                0,
            )

            price_errors = (
                price_result.get("errors")
                or []
            )

            price_warnings = (
                price_result.get("warnings")
                or []
            )

            validation_errors = (
                validation_result.get("errors")
                or []
            )

            validation_warnings = (
                validation_result.get("warnings")
                or []
            )

            if price_errors:
                messages.error(
                    request,
                    "价格规则重新应用失败："
                    + "；".join(
                        str(error)
                        for error in price_errors
                    ),
                )

            elif validation_errors:
                messages.error(
                    request,
                    f"已更新 {updated_count} 个产品价格，"
                    "但重新验证仍存在错误，"
                    "请检查工作流详情。",
                )

            elif (
                price_warnings
                or validation_warnings
            ):
                messages.warning(
                    request,
                    f"已更新 {updated_count} 个产品价格"
                    "并完成重新验证，"
                    "但仍有需要检查的提醒。",
                )

            else:
                messages.success(
                    request,
                    f"已重新应用价格规则，"
                    f"更新 {updated_count} 个产品价格，"
                    "工作流验证已通过。",
                )

        except Exception as exc:
            messages.error(
                request,
                f"重新应用价格规则失败：{exc}",
            )

        return redirect(next_url)

    if action == "validate":
        try:
            summary = validate_document_workflow_items(queryset)
            result = summary["results"][0] if summary.get("results") else None

            if result and "error" in result:
                messages.error(
                    request,
                    f"Order {order_number} / Batch {batch_number}：比对验证失败：{result['error']}",
                )
            elif result:
                status = result.get("status")
                error_count = result.get("error_count", 0)
                warning_count = result.get("warning_count", 0)

                if status == "ready":
                    messages.success(
                        request,
                        f"Order {order_number} / Batch {batch_number}：比对验证通过。",
                    )
                elif status == "blocked":
                    messages.error(
                        request,
                        (
                            f"Order {order_number} / Batch {batch_number}：比对后发现阻塞问题。"
                            f"errors={error_count}, warnings={warning_count}。"
                        ),
                    )
                else:
                    messages.warning(
                        request,
                        (
                            f"Order {order_number} / Batch {batch_number}：需要人工检查。"
                            f"errors={error_count}, warnings={warning_count}。"
                        ),
                    )
            else:
                messages.warning(
                    request,
                    f"Order {order_number} / Batch {batch_number}：没有返回验证结果。",
                )

        except Exception as exc:
            messages.error(
                request,
                f"Order {order_number} / Batch {batch_number}：比对验证异常：{exc}",
            )

        return redirect(next_url)

    if action == "generate":
        try:
            summary = generate_documents_for_workflow_items(
                queryset=queryset,
                generated_by=request.user,
            )
            result = summary["results"][0] if summary.get("results") else None

            if result and not result.get("success"):
                messages.error(
                    request,
                    (
                        f"Order {order_number} / Batch {batch_number}：生成文件失败："
                        f"{result.get('error')}"
                    ),
                )
            elif result:
                messages.success(
                    request,
                    (
                        f"Order {order_number} / Batch {batch_number}：文件已生成。"
                        f"Invoice={result.get('invoice_number')}，"
                        f"PO={result.get('po_number')}。"
                    ),
                )
            else:
                messages.warning(
                    request,
                    f"Order {order_number} / Batch {batch_number}：没有返回生成结果。",
                )

        except Exception as exc:
            messages.error(
                request,
                f"Order {order_number} / Batch {batch_number}：生成文件异常：{exc}",
            )

        return redirect(next_url)

    messages.warning(request, "未知操作。")
    return redirect(next_url)


@staff_member_required
def shipment_list(request):
    return render(
        request,
        "portal/shipments/list.html",
        build_shipment_list_context(request),
    )


@staff_member_required
def shipment_detail(request, batch_id):
    return render(
        request,
        "portal/shipments/detail.html",
        build_shipment_detail_context(request, batch_id),
    )


@staff_member_required
def backorder_list(request):
    return render(
        request,
        "portal/backorders/list.html",
        build_backorder_list_context(request),
    )


@staff_member_required
def backorder_export_xlsx(request):
    content = build_backorder_xlsx(
        build_backorder_queryset(request.GET)
    )
    export_date = timezone.localdate().isoformat()
    filename = f"待补发库_{export_date}.xlsx"
    fallback_filename = f"backorders_{export_date}.xlsx"

    response = HttpResponse(
        content,
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{fallback_filename}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    return response


@staff_member_required
def backorder_detail(request, backorder_id):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "reserve_inventory":
            try:
                reserve_inventory_for_backorder(
                    backorder_id=backorder_id,
                    quantity_requested=request.POST.get("quantity_requested"),
                    user=request.user,
                )
                messages.success(request, "库存已预留。")
            except Exception as exc:
                messages.error(request, f"库存预留失败：{exc}")

            return redirect("portal:backorder_detail", backorder_id=backorder_id)

        if action == "create_inventory_shipment":
            try:
                create_inventory_shipment_for_allocation(
                    allocation_id=request.POST.get("allocation_id"),
                )
                messages.success(
                    request,
                    "库存补发 ShipmentBatch 已创建，并已进入 Workflow。",
                )
            except Exception as exc:
                messages.error(request, f"创建库存补发批次失败：{exc}")

            return redirect("portal:backorder_detail", backorder_id=backorder_id)

    return render(
        request,
        "portal/backorders/detail.html",
        build_backorder_detail_context(request, backorder_id),
    )


# =============================================================================
# 医院订单
# =============================================================================


@staff_member_required
def order_list(request):
    return render(
        request,
        "portal/orders/list.html",
        build_order_list_context(request),
    )


@staff_member_required
def order_detail(request, order_id):
    return render(
        request,
        "portal/orders/detail.html",
        build_order_detail_context(request, order_id),
    )


@staff_member_required
def order_upload(request):
    """上传医院订单 PDF，并自动提取/基础验证。"""
    if request.method == "POST":
        uploaded_file = request.FILES.get("hospital_order_pdf")

        if not uploaded_file:
            messages.error(request, "请选择一个医院订单 PDF 文件。")
            return redirect("portal:order_upload")

        filename = uploaded_file.name or ""

        if not filename.lower().endswith(".pdf"):
            messages.error(request, "目前医院订单只支持 PDF 文件。")
            return redirect("portal:order_upload")

        order = create_order_for_upload(
            uploaded_file=uploaded_file,
            user=request.user,
        )

        try:
            status, errors, warnings = run_order_extraction(
                order,
                force_ocr=False,
            )
        except Exception as exc:
            order.refresh_from_db()
            messages.error(
                request,
                f"医院订单已上传，但自动提取失败：{exc}",
            )
            return redirect("portal:order_detail", order_id=order.id)

        order.refresh_from_db()

        if status == "error":
            messages.error(
                request,
                "医院订单已上传，但提取结果存在错误。请在详情页检查。",
            )
            return redirect("portal:order_detail", order_id=order.id)

        if status == "pending":
            messages.warning(
                request,
                "医院订单已上传，但系统没有确认提取完成。请在详情页检查。",
            )
            return redirect("portal:order_detail", order_id=order.id)

        if errors:
            messages.error(
                request,
                f"医院订单 {order.bon_de_commande} 已成功提取，但订单基础验证存在问题。请检查详情页。",
            )
            return redirect("portal:order_detail", order_id=order.id)

        if warnings:
            messages.warning(
                request,
                f"医院订单 {order.bon_de_commande} 已成功提取，但有一些提醒。请检查详情页。",
            )
            return redirect("portal:order_detail", order_id=order.id)

        messages.success(
            request,
            f"医院订单 {order.bon_de_commande} 已上传、自动提取并通过基础验证。",
        )
        return redirect("portal:order_list")

    return render(
        request,
        "portal/orders/upload.html",
        build_order_upload_context(request),
    )


@staff_member_required
def order_edit(request, order_id):
    """医院订单详情页的人工编辑保存。"""
    from orders.models import Order

    order = get_object_or_404(Order, id=order_id)

    if request.method != "POST":
        return redirect("portal:order_detail", order_id=order.id)

    errors, warnings = save_order_manual_edit(
        order=order,
        post_data=request.POST,
    )

    if errors:
        messages.error(request, "修改已保存，但订单基础验证仍存在问题。")
    elif warnings:
        messages.warning(request, "修改已保存，但订单基础验证存在提醒。")
    else:
        messages.success(request, "修改已保存，订单基础验证已通过。")

    return redirect("portal:order_detail", order_id=order.id)


@staff_member_required
def order_action(request, order_id):
    """医院订单详情页操作：重新提取、基础验证、生成 Factory Request。"""
    from orders.models import Order

    order = get_object_or_404(Order, id=order_id)

    if request.method != "POST":
        return redirect("portal:order_detail", order_id=order.id)

    action = request.POST.get("action")

    if action == "extract":
        try:
            status, errors, warnings = run_order_extraction(
                order,
                force_ocr=True,
            )
            order.refresh_from_db()

            if status in ["error", "pending"]:
                messages.error(
                    request,
                    f"订单 {order.bon_de_commande} 已重新提取，但提取状态异常。",
                )
            elif errors:
                messages.error(
                    request,
                    f"订单 {order.bon_de_commande} 已重新提取，但基础验证存在问题。",
                )
            elif warnings:
                messages.warning(
                    request,
                    f"订单 {order.bon_de_commande} 已重新提取，但存在提醒。",
                )
            else:
                messages.success(
                    request,
                    f"订单 {order.bon_de_commande} 已重新提取并通过基础验证。",
                )

        except Exception as exc:
            messages.error(request, f"重新提取失败：{exc}")

        return redirect("portal:order_detail", order_id=order.id)

    if action == "validate":
        errors, warnings = validate_portal_order_after_extraction(order)

        if errors:
            messages.error(request, "订单基础验证存在问题，请检查详情页。")
        elif warnings:
            messages.warning(request, "订单基础验证存在提醒，请检查详情页。")
        else:
            messages.success(request, "订单基础验证已通过。")

        return redirect("portal:order_detail", order_id=order.id)

    if action == "generate_request":
        try:
            document, errors, warnings = generate_factory_request_for_order(
                order=order,
                user=request.user,
            )

            if errors:
                messages.error(
                    request,
                    "订单基础验证未通过，暂时不能生成 Factory Request。",
                )
                return redirect("portal:order_detail", order_id=order.id)

            messages.success(request, f"Factory Request 已生成：{document}")

        except Exception as exc:
            messages.error(request, f"Factory Request 生成失败：{exc}")

        fallback_url = reverse("portal:order_detail", args=[order.id])
        return safe_redirect_after_action(request, fallback_url)

    messages.error(request, "未知操作。")
    return redirect("portal:order_detail", order_id=order.id)


# =============================================================================
# 工厂采购
# =============================================================================


@staff_member_required
def factory_list(request):
    return render(
        request,
        "portal/factory/list.html",
        build_factory_list_context(request),
    )


@staff_member_required
def factory_detail(request, confirmation_id):
    return render(
        request,
        "portal/factory/detail.html",
        build_factory_detail_context(request=request, confirmation_id=confirmation_id),
    )


@staff_member_required
def factory_upload(request):
    """
    上传工厂采购 PDF，并执行一键化流程：
        上传 → 提取 → 匹配医院订单 → 创建 Serial/ShipmentBatch → 进入 Workflow → 验证。

    跳转规则：
        - 全部通过：直接进入 Workflow 详情页。
        - 有任何错误、提醒或未进入 workflow：留在工厂采购详情页。
    """
    if request.method == "POST":
        uploaded_file = request.FILES.get("confirmation_pdf")
        confirmation_type = request.POST.get("confirmation_type")
        order_id = request.POST.get("order_id")

        if not uploaded_file:
            messages.error(request, "请先选择工厂采购 PDF。")
            return render(
                request,
                "portal/factory/upload.html",
                build_factory_upload_context(
                    request,
                    selected_order_id=order_id,
                    default_confirmation_type=confirmation_type,
                ),
            )

        try:
            confirmation, success, message_text = create_and_extract_factory_confirmation(
                uploaded_file=uploaded_file,
                confirmation_type=confirmation_type,
                order_id=order_id,
                user=request.user,
            )
        except Exception as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "portal/factory/upload.html",
                build_factory_upload_context(
                    request,
                    selected_order_id=order_id,
                    default_confirmation_type=confirmation_type,
                ),
            )

        confirmation.refresh_from_db()

        return _redirect_after_factory_processing(
            request=request,
            confirmation=confirmation,
            success=success,
            message_text=message_text,
        )

    return render(
        request,
        "portal/factory/upload.html",
        build_factory_upload_context(
            request,
            selected_order_id=request.GET.get("order_id"),
            default_confirmation_type=request.GET.get("type"),
        ),
    )


@staff_member_required
def order_factory_upload(request, order_id):
    if request.method == "POST":
        uploaded_file = request.FILES.get("confirmation_pdf")
        confirmation_type = request.POST.get("confirmation_type")

        if not uploaded_file:
            messages.error(request, "请先选择工厂采购 PDF。")
            return render(
                request,
                "portal/factory/upload.html",
                build_factory_upload_context(
                    request,
                    selected_order_id=order_id,
                    default_confirmation_type=confirmation_type,
                    order_locked=True,
                ),
            )

        try:
            confirmation, success, message_text = create_and_extract_factory_confirmation(
                uploaded_file=uploaded_file,
                confirmation_type=confirmation_type,
                order_id=order_id,
                user=request.user,
            )
        except Exception as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "portal/factory/upload.html",
                build_factory_upload_context(
                    request,
                    selected_order_id=order_id,
                    default_confirmation_type=confirmation_type,
                    order_locked=True,
                ),
            )

        confirmation.refresh_from_db()

        return _redirect_after_factory_processing(
            request=request,
            confirmation=confirmation,
            success=success,
            message_text=message_text,
        )

    return render(
        request,
        "portal/factory/upload.html",
        build_factory_upload_context(
            request,
            selected_order_id=order_id,
            default_confirmation_type=request.GET.get("type"),
            order_locked=True,
        ),
    )


@staff_member_required
def factory_action(request, confirmation_id):
    """工厂采购详情页操作：重新提取、安全删除。"""
    if request.method != "POST":
        return redirect("portal:factory_detail", confirmation_id=confirmation_id)

    action = request.POST.get("action")

    if action == "re_extract":
        try:
            confirmation = reextract_factory_confirmation_for_portal(
                confirmation_id=confirmation_id,
            )
            confirmation.refresh_from_db()

            return _redirect_after_factory_processing(
                request=request,
                confirmation=confirmation,
                success=True,
                message_text=f"工厂采购 FC #{confirmation.id} 已重新提取并进入工作流。",
            )

        except Exception as exc:
            messages.error(request, f"重新提取失败：{exc}")
            return redirect("portal:factory_detail", confirmation_id=confirmation_id)

    if action == "save_serials":
        try:
            errors, warnings, workflow_item = save_factory_serial_manual_edit(
                confirmation_id=confirmation_id,
                post_data=request.POST,
                user=request.user,
            )

            if errors:
                messages.error(
                    request,
                    "Serial 修改已保存失败，请检查错误：" + " / ".join(errors[:3]),
                )

            elif warnings:
                messages.warning(
                    request,
                    "Serial 修改已保存，并已重新同步后续流程，但存在提醒："
                    + " / ".join(warnings[:3]),
                )

            else:
                messages.success(
                    request,
                    "Serial 修改已保存，并已重新同步 ShipmentBatch / Workflow。",
                )

        except Exception as exc:
            messages.error(
                request,
                f"保存 Serial 修改失败：{exc}",
            )

        return redirect("portal:factory_detail", confirmation_id=confirmation_id)

    if action == "associate_order":
        try:
            associate_order_and_finalize_factory_confirmation(
                confirmation_id=confirmation_id,
                order_id=request.POST.get("order_id"),
                user=request.user,
            )
            messages.success(
                request,
                "订单已关联，并已继续完成 ShipmentBatch / Workflow 同步。",
            )
        except Exception as exc:
            messages.error(
                request,
                f"关联订单并继续处理失败：{exc}",
            )

        return redirect("portal:factory_detail", confirmation_id=confirmation_id)
        
    if action == "delete":
        try:
            result = safely_delete_factory_confirmation_for_portal(
                confirmation_id=confirmation_id,
            )

            messages.success(
                request,
                (
                    f"工厂采购 FC #{result['confirmation_id']} 已删除。"
                    f"Serial 删除 {result['serial_deleted']} 条，"
                    f"ShipmentBatch 删除 {result['shipment_batch_deleted']} 条，"
                    f"Workflow 删除 {result['workflow_deleted']} 条。"
                ),
            )
            return redirect("portal:factory_list")

        except Exception as exc:
            messages.error(request, f"删除失败：{exc}")
            return redirect("portal:factory_detail", confirmation_id=confirmation_id)

    messages.warning(request, "未知操作。")
    return redirect("portal:factory_detail", confirmation_id=confirmation_id)

# =============================================================================
# Settlements / 发票与结算
# =============================================================================


@staff_member_required
def settlement_home(request):
    from portal.services.settlement_portal_service import (
        build_settlement_home_context,
    )

    return render(
        request,
        "portal/settlements/home.html",
        build_settlement_home_context(
            request
        ),
    )


@staff_member_required
def settlement_receivables(request):
    from portal.services.settlement_portal_service import (
        build_account_list_context,
    )
    from settlements.models import (
        SettlementAccount,
    )

    return render(
        request,
        "portal/settlements/account_list.html",
        build_account_list_context(
            request,
            direction=(
                SettlementAccount
                .Direction
                .RECEIVABLE
            ),
        ),
    )


@staff_member_required
def settlement_payables(request):
    from portal.services.settlement_portal_service import (
        build_account_list_context,
    )
    from settlements.models import (
        SettlementAccount,
    )

    return render(
        request,
        "portal/settlements/account_list.html",
        build_account_list_context(
            request,
            direction=(
                SettlementAccount
                .Direction
                .PAYABLE
            ),
        ),
    )


@staff_member_required
def settlement_transactions(request):
    from portal.services.settlement_portal_service import (
        build_transaction_list_context,
    )

    return render(
        request,
        "portal/settlements/transactions.html",
        build_transaction_list_context(
            request
        ),
    )
