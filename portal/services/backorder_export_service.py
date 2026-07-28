from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font


BACKORDER_EXPORT_HEADERS = [
    "医院订单",
    "医院",
    "产品号",
    "订单数量",
    "已发数量",
    "待补发",
]

BACKORDER_EXPORT_COLUMN_WIDTHS = {
    "A": 18,
    "B": 35,
    "C": 20,
    "D": 12,
    "E": 12,
    "F": 12,
}


def _set_text_cell(cell, value):
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"
    cell.number_format = "@"


def build_backorder_xlsx(queryset):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "待补发库"

    worksheet.append(BACKORDER_EXPORT_HEADERS)
    worksheet.freeze_panes = "A2"

    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
        )

    for row_number, line in enumerate(queryset.iterator(), start=2):
        hospital = (
            line.order.hospital.name
            if line.order.hospital
            else (line.order.hospital_name or "")
        )

        _set_text_cell(
            worksheet.cell(row=row_number, column=1),
            line.order.bon_de_commande,
        )
        _set_text_cell(
            worksheet.cell(row=row_number, column=2),
            hospital,
        )
        _set_text_cell(
            worksheet.cell(row=row_number, column=3),
            line.product_code,
        )

        quantities = (
            line.requested_quantity,
            line.shipped_quantity,
            line.remaining_quantity,
        )
        for column, quantity in enumerate(quantities, start=4):
            cell = worksheet.cell(
                row=row_number,
                column=column,
                value=int(quantity),
            )
            cell.number_format = "0"

        for cell in worksheet[row_number]:
            cell.alignment = Alignment(vertical="center")

    worksheet.auto_filter.ref = f"A1:F{worksheet.max_row}"

    for column, width in BACKORDER_EXPORT_COLUMN_WIDTHS.items():
        worksheet.column_dimensions[column].width = width

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
