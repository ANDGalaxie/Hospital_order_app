from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import OrderCreateForm
from .models import Order


@login_required
def order_list(request):
    """
    订单列表页面。

    只有登录用户可以访问。
    """
    orders = Order.objects.all()

    return render(
        request,
        "orders/order_list.html",
        {
            "orders": orders,
        },
    )


@login_required
def order_create(request):
    """
    创建订单页面。

    功能：
        1. 输入 bon de commande
        2. 输入医院名称
        3. 上传医院订单 PDF
        4. 保存订单记录
        5. 自动记录创建人 created_by
    """
    if request.method == "POST":
        form = OrderCreateForm(request.POST, request.FILES)

        if form.is_valid():
            order = form.save(commit=False)
            order.created_by = request.user
            order.status = Order.Status.HOSPITAL_ORDER_UPLOADED
            order.save()

            return redirect("order_detail", pk=order.pk)

    else:
        form = OrderCreateForm()

    return render(
        request,
        "orders/order_form.html",
        {
            "form": form,
        },
    )


@login_required
def order_detail(request, pk):
    """
    订单详情页面。

    Milestone 1 只显示基本信息和上传的 PDF。
    后续 Milestone 2 会在这里加入 OCR 提取按钮和提取结果展示。
    """
    order = get_object_or_404(Order, pk=pk)

    return render(
        request,
        "orders/order_detail.html",
        {
            "order": order,
        },
    )