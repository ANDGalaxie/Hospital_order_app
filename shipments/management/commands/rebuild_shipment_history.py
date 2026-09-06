from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from orders.models import Order
from shipments.services.shipment_history_service import (
    rebuild_order_shipment_history,
    summarize_order_shipment_history,
)


class Command(BaseCommand):
    help = "重建 ShipmentBatch 的累计数量、OrderItem 和 BackorderSnapshot。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--order-id",
            type=int,
            help="只重建一个医院订单的发货历史。",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="rebuild_all",
            help="重建全部订单的发货历史。",
        )
        parser.add_argument(
            "--confirm-all",
            action="store_true",
            help="与 --all 配合使用，明确确认执行全量重建。",
        )

    def handle(self, *args, **options):
        order_id = options.get("order_id")
        rebuild_all = options.get("rebuild_all")
        confirm_all = options.get("confirm_all")

        if bool(order_id) == bool(rebuild_all):
            raise CommandError("必须且只能指定 --order-id 或 --all 其中一个。")

        if rebuild_all and not confirm_all:
            raise CommandError(
                "全量重建需要显式传入 --confirm-all，避免误操作。"
            )

        if order_id:
            order = Order.objects.filter(id=order_id).first()
            if not order:
                raise CommandError(f"未找到订单 id={order_id}。")

            self._rebuild_order(order)
            return

        orders = Order.objects.order_by("id")
        self.stdout.write(self.style.WARNING(f"即将重建 {orders.count()} 个订单的发货历史。"))

        for order in orders.iterator():
            self._rebuild_order(order)

    def _rebuild_order(self, order):
        before = summarize_order_shipment_history(order)

        with transaction.atomic():
            result = rebuild_order_shipment_history(order)

        after = summarize_order_shipment_history(order)

        self.stdout.write(
            self.style.SUCCESS(
                f"Order {order.bon_de_commande} 重建完成："
                f"批次数 {before['batch_count']} -> {after['batch_count']}，"
                f"最终累计已发 {result['final_total_shipped_quantity']}，"
                f"最终待发 {result['final_remaining_quantity']}。"
            )
        )

        self.stdout.write(f"  重建前：{before['batches']}")
        self.stdout.write(f"  重建后：{after['batches']}")
