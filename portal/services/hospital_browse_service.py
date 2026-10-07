"""Allowlisted shared data. No pricing policies, raw JSON or business models."""
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.urls import reverse
from django.utils.translation import gettext as _

from factories.models import Factory
from hospitals.models import Hospital
from orders.models import Order, OrderItem
from products.models import Product, ProductCategory
from portal.services.common import get_user_display_name


def build_shared_read_context(request, **kwargs):
    route = request.resolver_match.view_name.removeprefix("portal:")
    query = (request.GET.get("q") or "").strip()[:200]
    context = {"page_title": _("资料库"), "query": query, "rows": [], "headers": [],
               "fields": [], "modules": [], "attachments": [],
               "back_url": reverse("portal:library_home"),
               "user_display_name": get_user_display_name(request.user)}
    if route == "library_home":
        for title, target in ((_("产品库"), "library_products"), (_("医院库"), "library_hospitals"), (_("工厂库"), "library_factories")):
            context["modules"].append({"title": title, "url": reverse("portal:" + target)})
        context["back_url"] = reverse("portal:home")
        return context
    if route.startswith("order_"):
        context.update(page_title=_("医院订单"), back_url=reverse("portal:home"))
        records = Order.objects.all()
        if "order_id" in kwargs:
            records = records.filter(pk=kwargs["order_id"])
        elif query:
            records = records.filter(Q(bon_de_commande__icontains=query) | Q(hospital_name__icontains=query) | Q(hospital__name__icontains=query))
        records = records.order_by("-order_date", "-pk").values("id", "bon_de_commande", "hospital_name", "hospital__name", "order_date", "hospital_order_pdf")
        if "order_id" in kwargs:
            record = records.first()
            if record is None:
                raise Http404
            context["fields"] = [{"label": _("订单号"), "value": record["bon_de_commande"]},
                                 {"label": _("医院"), "value": record["hospital_name"] or record["hospital__name"] or "—"},
                                 {"label": _("下单日期"), "value": record["order_date"]}]
            if record["hospital_order_pdf"]:
                context["attachments"] = [{"label": _("原始 PDF"), "url": reverse("portal:protected_media", kwargs={"path": record["hospital_order_pdf"]})}]
            context["headers"] = [_("产品号"), _("描述"), _("数量")]
            context["rows"] = [{"cells": [item["product_code"], item["description"], item["requested_quantity"]]} for item in
                               OrderItem.objects.filter(order_id=record["id"]).order_by("id").values("product_code", "description", "requested_quantity")]
            context["back_url"] = reverse("portal:order_list")
        else:
            page = Paginator(records, 25).get_page(request.GET.get("page"))
            context["headers"] = [_("订单号"), _("医院"), _("下单日期")]
            context["rows"] = [{"cells": [r["bon_de_commande"], r["hospital_name"] or r["hospital__name"] or "—", r["order_date"]],
                                "url": reverse("portal:order_detail", args=[r["id"]])} for r in page]
            context["page_obj"] = page
        return context
    if route.startswith("library_product"):
        context["page_title"] = _("产品库")
        products = Product.objects.filter(is_active=True)
        if "product_id" in kwargs:
            products = products.filter(pk=kwargs["product_id"])
        else:
            for key, kind in (("department_id", "department"), ("factory_node_id", "factory"), ("category_id", "category")):
                if key in kwargs:
                    node = ProductCategory.objects.filter(pk=kwargs[key], node_type=kind, is_active=True).values("id", "name").first()
                    if node is None:
                        raise Http404
                    from portal.services.product_library_portal_service import get_descendant_category_ids
                    ids = [node["id"]] + get_descendant_category_ids(ProductCategory.objects.only("id").get(pk=node["id"]))
                    products = products.filter(category_id__in=ids)
                    context["fields"] = [{"label": _("分类"), "value": node["name"]}]
            if query:
                products = products.filter(Q(code__icontains=query) | Q(description__icontains=query))
        records = products.order_by("code", "id").values("id", "code", "description", "category__name")
        if "product_id" in kwargs and not records.exists():
            raise Http404
        page = Paginator(records, 25).get_page(request.GET.get("page"))
        context["headers"] = [_("产品号"), _("描述"), _("分类")]
        context["rows"] = [{"cells": [r["code"], r["description"], r["category__name"] or "—"],
                            "url": reverse("portal:library_product_detail", args=[r["id"]]) if "product_id" not in kwargs else None} for r in page]
        context["page_obj"] = page
        return context
    for prefix, model, detail_key, target, title, fields in (
        ("library_hospital", Hospital, "hospital_id", "library_hospital_detail", _("医院库"),
         (("name", _("医院")), ("billing_address", _("账单地址")), ("default_shipping_address", _("收货地址")), ("contact_name", _("联系人")), ("phone", _("电话")), ("email", _("邮箱")))),
        ("library_factor", Factory, "factory_id", "library_factory_detail", _("工厂库"),
         (("name", _("工厂")), ("address", _("地址")), ("buyer", _("联系人")))),
    ):
        if route.startswith(prefix):
            records = model.objects.filter(is_active=True)
            if detail_key in kwargs:
                records = records.filter(pk=kwargs[detail_key])
            elif query:
                records = records.filter(name__icontains=query)
            records = records.order_by("name", "id").values("id", *(key for key, label in fields))
            context["page_title"] = title
            if detail_key in kwargs:
                record = records.first()
                if record is None:
                    raise Http404
                context["fields"] = [{"label": label, "value": record[key] or "—"} for key, label in fields]
            else:
                page = Paginator(records, 25).get_page(request.GET.get("page"))
                context["headers"] = [fields[0][1]]
                context["rows"] = [{"cells": [r["name"]], "url": reverse("portal:" + target, args=[r["id"]])} for r in page]
                context["page_obj"] = page
            return context
    raise Http404
