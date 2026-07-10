from pathlib import Path
from typing import Optional

from django.conf import settings


def get_serial_source_text(serial_item) -> str:
    """
    返回源文件中这一条 serial 的原始文本。
    如果无法生成截图，就用这个文本作为 fallback。
    """
    raw_data = serial_item.raw_data or {}

    raw_line = raw_data.get("raw_line")
    if raw_line:
        return str(raw_line)

    parts = [
        raw_data.get("product_code") or serial_item.product_code,
        raw_data.get("serial_number") or serial_item.serial_number,
        raw_data.get("expiration_date_raw"),
    ]

    return "\n".join(str(part) for part in parts if part)


def get_serial_row_crop_url(serial_item) -> Optional[str]:
    """
    给 SerialItem 生成源 PDF 中 serial number 的局部截图。

    现在只截取 serial number 本身，不再截取整行。
    这样人工核对时可以直接对比：
        源文件 serial 截图 <-> 系统识别 serial number

    注意：
    - 优先使用 raw_data["serial_number"]，因为这是源文件里识别出来的原始 serial。
    - 如果用户后来手动修改了 SerialItem.serial_number，截图仍然对应原始 PDF 中的位置。
    """
    raw_data = serial_item.raw_data or {}

    page_number = raw_data.get("page")
    if not page_number:
        return None

    confirmation = serial_item.factory_confirmation

    if not confirmation or not confirmation.confirmation_pdf:
        return None

    pdf_path = Path(confirmation.confirmation_pdf.path)

    if not pdf_path.exists():
        return None

    source_serial_number = str(
        raw_data.get("serial_number") or serial_item.serial_number or ""
    ).strip()

    if not source_serial_number:
        return None

    crop_rel = (
        f"portal/factory_serial_crops/"
        f"confirmation_{confirmation.id}/"
        f"serial_{serial_item.id}.png"
    )

    crop_path = Path(settings.MEDIA_ROOT) / crop_rel
    crop_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fitz
    except Exception:
        return None

    try:
        with fitz.open(str(pdf_path)) as document:
            page_index = int(page_number) - 1

            if page_index < 0 or page_index >= len(document):
                return None

            page = document[page_index]

            rects = page.search_for(source_serial_number)

            if not rects:
                return None

            found_rect = rects[0]
            page_rect = page.rect

            # 只围绕 serial number 本身裁剪。
            # 横向和纵向都留一点 padding，避免文字被切边。
            x_padding = 8
            y_padding_top = 5
            y_padding_bottom = 6

            clip = fitz.Rect(
                max(0, found_rect.x0 - x_padding),
                max(0, found_rect.y0 - y_padding_top),
                min(page_rect.width, found_rect.x1 + x_padding),
                min(page_rect.height, found_rect.y1 + y_padding_bottom),
            )

            # 放大倍数提高截图清晰度。
            matrix = fitz.Matrix(3, 3)
            pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
            pixmap.save(str(crop_path))

        media_url = settings.MEDIA_URL.rstrip("/")
        return f"{media_url}/{crop_rel}"

    except Exception:
        return None