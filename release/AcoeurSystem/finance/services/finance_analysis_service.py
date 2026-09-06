from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from orders.models import Order
from factory_confirmations.models import SerialItem
from shipments.models import ShipmentBatchItem
from backorders.models import BackorderLine

try:
    from pricing.services.price_policy_service import get_hospital_order_date
except Exception:
    get_hospital_order_date = None


ZERO = Decimal("0.00")


def to_decimal(value):
    if value is None:
        return ZERO

    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def money(value):
    return to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_float(value):
    return float(money(value))


def format_money(value):
    value = money(value)
    text = f"{value:,.2f}".replace(",", " ")
    return f"{text} €"


def format_percent(value):
    value = to_decimal(value)

    if value == 0:
        return "0.00%"

    return f"{value.quantize(Decimal('0.0001')) * 100:.2f}%"


def get_order_business_date(order):
    """
    财务分析里的订单月份，优先使用医院订单 Date de commande。
    如果无法读取，才 fallback 到订单创建日期。
    """

    if get_hospital_order_date:
        try:
            order_date, source = get_hospital_order_date(order)
            if order_date:
                return order_date, source
        except Exception:
            pass

    if getattr(order, "created_at", None):
        return order.created_at.date(), "created_at"

    return timezone.localdate(), "fallback_today"


def get_hospital_name(order):
    if getattr(order, "hospital", None):
        return order.hospital.name

    return "未匹配医院"


def is_short_expiration(serial):
    """
    判断是否低于一年有效期：
      expiration_date < shipping_date + 365 days
    """

    expiration_date = getattr(serial, "expiration_date", None)
    confirmation = getattr(serial, "factory_confirmation", None)
    shipping_date = getattr(confirmation, "shipping_date", None)

    if not expiration_date or not shipping_date:
        return False

    return expiration_date < shipping_date + timedelta(days=365)


def get_item_prices(item):
    hospital_unit_price = to_decimal(getattr(item, "hospital_unit_price", None))

    factory_unit_price = to_decimal(getattr(item, "factory_unit_price", None))

    if factory_unit_price <= 0 and getattr(item, "product", None):
        factory_unit_price = to_decimal(getattr(item.product, "factory_unit_price", None))

    expiration_discount_rate = to_decimal(
        getattr(item, "expiration_discount_rate", None)
    )

    return hospital_unit_price, factory_unit_price, expiration_discount_rate


def calculate_factory_cost_for_item(item, serials):
    """
    优先按 SerialItem 精确计算采购成本。
    如果没有 serial，就按 confirmed_quantity 估算。
    """

    confirmed_quantity = int(getattr(item, "confirmed_quantity", 0) or 0)
    _, factory_unit_price, expiration_discount_rate = get_item_prices(item)

    if confirmed_quantity <= 0:
        return ZERO

    if not serials:
        return money(factory_unit_price * confirmed_quantity)

    total_cost = ZERO

    for serial in serials:
        if is_short_expiration(serial):
            final_rate = Decimal("1.00") - expiration_discount_rate
        else:
            final_rate = Decimal("1.00")

        total_cost += factory_unit_price * final_rate

    # 如果 confirmed_quantity 比 serial 数量多，用普通采购价补估算。
    missing_qty = confirmed_quantity - len(serials)

    if missing_qty > 0:
        total_cost += factory_unit_price * missing_qty

    return money(total_cost)


def build_finance_dashboard_data():
    """
    Phase 1 财务分析数据：
      - 核心指标
      - 月度趋势图
      - 医院毛利润排行图
    """

    orders = list(
        Order.objects
        .select_related("hospital")
        .prefetch_related("items", "items__product")
        .all()
        .order_by("bon_de_commande")
    )

    order_ids = [order.id for order in orders]

    serials_by_order_product = defaultdict(list)

    for serial in (
        SerialItem.objects
        .select_related("factory_confirmation")
        .filter(order_id__in=order_ids)
    ):
        if serial.product_code:
            serials_by_order_product[(serial.order_id, serial.product_code)].append(serial)

    order_item_by_key = {}

    total_revenue = ZERO
    total_factory_cost = ZERO

    monthly_summary = defaultdict(lambda: {
        "revenue": ZERO,
        "factory_cost": ZERO,
        "gross_profit": ZERO,
        "order_count": 0,
    })

    hospital_summary = defaultdict(lambda: {
        "revenue": ZERO,
        "factory_cost": ZERO,
        "gross_profit": ZERO,
        "order_count": 0,
    })

    product_summary = defaultdict(lambda: {
        "revenue": ZERO,
        "factory_cost": ZERO,
        "gross_profit": ZERO,
        "quantity": 0,
    })

    today = timezone.localdate()
    current_month_key = today.strftime("%Y-%m")

    this_month_order_revenue = ZERO

    for order in orders:
        order_date, date_source = get_order_business_date(order)
        month_key = order_date.strftime("%Y-%m")
        hospital_name = get_hospital_name(order)

        order_revenue = ZERO
        order_factory_cost = ZERO
        order_has_financial_data = False

        for item in order.items.all():
            product_code = getattr(item, "product_code", None)

            if not product_code:
                continue

            order_item_by_key[(order.id, product_code)] = item

            confirmed_quantity = int(getattr(item, "confirmed_quantity", 0) or 0)

            if confirmed_quantity <= 0:
                continue

            hospital_unit_price, _, _ = get_item_prices(item)

            item_revenue = money(hospital_unit_price * confirmed_quantity)

            serials = serials_by_order_product.get((order.id, product_code), [])
            item_factory_cost = calculate_factory_cost_for_item(item, serials)
            item_gross_profit = money(item_revenue - item_factory_cost)

            order_revenue += item_revenue
            order_factory_cost += item_factory_cost
            order_has_financial_data = True

            product_summary[product_code]["revenue"] += item_revenue
            product_summary[product_code]["factory_cost"] += item_factory_cost
            product_summary[product_code]["gross_profit"] += item_gross_profit
            product_summary[product_code]["quantity"] += confirmed_quantity

        if not order_has_financial_data:
            continue

        order_gross_profit = money(order_revenue - order_factory_cost)

        total_revenue += order_revenue
        total_factory_cost += order_factory_cost

        monthly_summary[month_key]["revenue"] += order_revenue
        monthly_summary[month_key]["factory_cost"] += order_factory_cost
        monthly_summary[month_key]["gross_profit"] += order_gross_profit
        monthly_summary[month_key]["order_count"] += 1

        hospital_summary[hospital_name]["revenue"] += order_revenue
        hospital_summary[hospital_name]["factory_cost"] += order_factory_cost
        hospital_summary[hospital_name]["gross_profit"] += order_gross_profit
        hospital_summary[hospital_name]["order_count"] += 1

        if month_key == current_month_key:
            this_month_order_revenue += order_revenue

    gross_profit = money(total_revenue - total_factory_cost)

    if total_revenue > 0:
        gross_margin = gross_profit / total_revenue
    else:
        gross_margin = ZERO

    # 本月发货销售额：按 ShipmentBatch 的 batch_date 判断。
    this_month_shipped_revenue = ZERO

    shipment_items = (
        ShipmentBatchItem.objects
        .select_related("batch", "batch__order")
        .filter(
            batch__batch_date__year=today.year,
            batch__batch_date__month=today.month,
        )
    )

    for shipped_item in shipment_items:
        order = shipped_item.batch.order
        product_code = shipped_item.product_code
        quantity = int(shipped_item.shipped_quantity or 0)

        order_item = order_item_by_key.get((order.id, product_code))

        if not order_item:
            order_item = order.items.filter(product_code=product_code).first()

        if not order_item:
            continue

        hospital_unit_price, _, _ = get_item_prices(order_item)
        this_month_shipped_revenue += money(hospital_unit_price * quantity)

    # 当前待发销售额 / 当前待发预计毛利润
    backorder_revenue = ZERO
    backorder_estimated_profit = ZERO

    for line in (
        BackorderLine.objects
        .filter(is_active=True, remaining_quantity__gt=0)
        .select_related("order", "product")
    ):
        quantity = int(line.remaining_quantity or 0)

        order_item = order_item_by_key.get((line.order_id, line.product_code))

        if not order_item:
            order_item = line.order.items.filter(product_code=line.product_code).first()

        if not order_item:
            continue

        hospital_unit_price, factory_unit_price, _ = get_item_prices(order_item)

        line_revenue = money(hospital_unit_price * quantity)
        line_cost = money(factory_unit_price * quantity)
        line_profit = money(line_revenue - line_cost)

        backorder_revenue += line_revenue
        backorder_estimated_profit += line_profit

    # KPI 卡片，全部中文，并带 hover 解释。
    kpi_cards = [
        {
            "label": "总销售额",
            "value": format_money(total_revenue),
            "help": "已确认发货产品的销售金额总和。计算方式：confirmed_quantity × 订单行医院卖价快照。",
        },
        {
            "label": "总采购成本",
            "value": format_money(total_factory_cost),
            "help": "已确认发货产品的预计采购成本。优先按 Serial Number 和有效期折扣计算。",
        },
        {
            "label": "预计毛利润",
            "value": format_money(gross_profit),
            "help": "预计毛利润 = 总销售额 - 总采购成本。这里还没有扣除运费、手续费、税费等其他费用。",
        },
        {
            "label": "毛利率",
            "value": format_percent(gross_margin),
            "help": "毛利率 = 预计毛利润 ÷ 总销售额。用于观察产品销售的整体盈利能力。",
        },
        {
            "label": "当前待发销售额",
            "value": format_money(backorder_revenue),
            "help": "当前还没有发完的产品对应的预计销售额。计算方式：remaining_quantity × 订单行医院卖价快照。",
        },
        {
            "label": "当前待发预计毛利润",
            "value": format_money(backorder_estimated_profit),
            "help": "当前待发产品对应的预计毛利润。第一版按订单行采购价估算，未来实际发货后可按 Serial Number 精确计算。",
        },
        {
            "label": "本月订单销售额",
            "value": format_money(this_month_order_revenue),
            "help": "医院下单日期属于本月的订单销售额。注意：这是按医院 Date de commande 统计。",
        },
        {
            "label": "本月发货销售额",
            "value": format_money(this_month_shipped_revenue),
            "help": "本月实际发货批次对应的销售额。统计依据是 Shipment Batch 的 batch_date。",
        },
    ]

    monthly_labels = sorted(monthly_summary.keys())

    monthly_chart = {
        "labels": monthly_labels,
        "revenue": [money_float(monthly_summary[m]["revenue"]) for m in monthly_labels],
        "factory_cost": [money_float(monthly_summary[m]["factory_cost"]) for m in monthly_labels],
        "gross_profit": [money_float(monthly_summary[m]["gross_profit"]) for m in monthly_labels],
    }

    top_hospitals = sorted(
        hospital_summary.items(),
        key=lambda x: x[1]["gross_profit"],
        reverse=True,
    )[:10]

    hospital_chart = {
        "labels": [name for name, data in top_hospitals],
        "gross_profit": [money_float(data["gross_profit"]) for name, data in top_hospitals],
    }

    product_rows = sorted(
        product_summary.items(),
        key=lambda x: x[1]["gross_profit"],
        reverse=True,
    )[:10]

    return {
        "kpi_cards": kpi_cards,
        "chart_data": {
            "monthly": monthly_chart,
            "hospital": hospital_chart,
        },
        "top_products": [
            {
                "product_code": code,
                "quantity": data["quantity"],
                "revenue": format_money(data["revenue"]),
                "factory_cost": format_money(data["factory_cost"]),
                "gross_profit": format_money(data["gross_profit"]),
            }
            for code, data in product_rows
        ],
    }
