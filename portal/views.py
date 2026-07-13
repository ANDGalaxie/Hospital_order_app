"""
Portal views.

这个文件只负责接收 HTTP 请求、调用 service 层、返回页面或跳转。
业务逻辑尽量放在 portal/services/* 或各 app 的 services/* 里，避免 views.py 变得过重。
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from portal.services.common import (
    get_safe_next_url,
    safe_redirect_after_action,
)
from portal.services.factory_portal_service import (
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
from portal.services.workflow_portal_service import (
    build_workflow_detail_context,
    build_workflow_list_context,
    get_order_number,
)

from portal.services.library_portal_service import build_library_home_context
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
def library_hospitals(request):
    return render(request, "portal/library/coming_soon.html", {
        "title": "医院资料",
        "description": "医院资料列表页即将接入。",
    })


@staff_member_required
def library_factories(request):
    return render(request, "portal/library/coming_soon.html", {
        "title": "工厂资料",
        "description": "工厂资料列表页即将接入。",
    })


@staff_member_required
def library_prices(request):
    return render(request, "portal/library/coming_soon.html", {
        "title": "价格资料",
        "description": "价格资料列表页即将接入。",
    })
    
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
        factory_id = request.POST.get("factory_id")

        if not uploaded_file:
            messages.error(request, "请先选择工厂采购 PDF。")
            return render(
                request,
                "portal/factory/upload.html",
                build_factory_upload_context(request),
            )

        confirmation, success, message_text = create_and_extract_factory_confirmation(
            uploaded_file=uploaded_file,
            confirmation_type=confirmation_type,
            factory_id=factory_id,
            user=request.user,
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
        build_factory_upload_context(request),
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
