from pathlib import Path
from typing import Optional

from django.conf import settings
from PIL import Image


def get_order_item_row_crop_url(item) -> Optional[str]:
    """
    根据 OrderItem.raw_data 中的 page + debug.product_y，
    从 media/order_workspaces/order_x/hospital_order/ocr/page_y.png
    裁出一条横向行截图。

    第一版裁整行，不裁单独单元格。
    """
    raw_data = item.raw_data or {}

    if not isinstance(raw_data, dict):
        return None

    page = raw_data.get("page")
    debug = raw_data.get("debug") or {}

    y_value = (
        debug.get("product_y")
        or debug.get("boit_y")
        or debug.get("compte_y")
    )

    if not page or y_value is None:
        return None

    order = item.order

    page_image_rel = (
        f"order_workspaces/order_{order.id}/hospital_order/ocr/page_{page}.png"
    )
    page_image_path = Path(settings.MEDIA_ROOT) / page_image_rel

    if not page_image_path.exists():
        return None

    crop_rel = f"portal/order_item_crops/order_{order.id}/item_{item.id}.png"
    crop_path = Path(settings.MEDIA_ROOT) / crop_rel
    crop_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(page_image_path) as image:
            width, height = image.size

            y_center = int(float(y_value))

            # 裁整行：上下各留一点空间
            top = max(0, y_center - 70)
            bottom = min(height, y_center + 70)

            # 保留整行主要区域，避免裁到页边
            left = 35
            right = max(left + 1, width - 35)

            cropped = image.crop((left, top, right, bottom))
            cropped.save(crop_path)

        media_url = settings.MEDIA_URL.rstrip("/")
        return f"{media_url}/{crop_rel}"

    except Exception:
        return None
