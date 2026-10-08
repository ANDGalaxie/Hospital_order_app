"""Trusted static SVG plots. Only drawing coordinates use floating point."""
import math
from xml.etree import ElementTree as ET

from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from .settlement_finance_service import format_money

COLORS = ("#2563eb", "#ea580c", "#15803d", "#dc2626", "#7c3aed")


def line_chart_svg(labels, series, title):
    width, height = 1020, 285
    svg = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 {width} {height}",
        "width": str(width), "height": str(height), "role": "img",
        "font-family": "DejaVu Sans, Noto Sans CJK SC, sans-serif",
        "font-size": "11",
    })
    ET.SubElement(svg, "title").text = str(title)
    ET.SubElement(svg, "rect", {"width": "1020", "height": "285", "fill": "#ffffff"})

    def text(x, y, label, **attrs):
        node = ET.SubElement(svg, "text", {"x": str(x), "y": str(y), "fill": "#475569", **attrs})
        node.text = str(label)

    if not labels:
        text(width / 2, height / 2, _("No trend data for this selection."), **{"text-anchor": "middle"})
        return mark_safe(ET.tostring(svg, encoding="unicode"))
    values = []
    for index, item in enumerate(series):
        if len(item["values"]) != len(labels):
            raise ValueError("Chart series and month labels do not match.")
        plotted = [float(value) for value in item["values"]]
        if not all(math.isfinite(value) for value in plotted):
            raise ValueError("Chart values must be finite.")
        values.extend(plotted)
        x, y = 18 + (index % 3) * 334, 17 + (index // 3) * 19
        ET.SubElement(svg, "line", {"x1": str(x), "y1": str(y - 4), "x2": str(x + 18), "y2": str(y - 4),
                                  "stroke": COLORS[index % len(COLORS)], "stroke-width": "3"})
        text(x + 25, y, item["label"])
    minimum, maximum = min([0.0, *values]), max([0.0, *values])
    if minimum == maximum:
        minimum, maximum = -1.0, 1.0
    left, right, top, bottom = 100, 24, 66, 37
    plot_width, plot_height = width - left - right, height - top - bottom

    def xpoint(index):
        return left + plot_width * (index / (len(labels) - 1) if len(labels) > 1 else 0.5)

    def ypoint(value):
        return top + (maximum - value) / (maximum - minimum) * plot_height

    for index in range(5):
        value = maximum - (maximum - minimum) * index / 4
        y = ypoint(value)
        ET.SubElement(svg, "line", {"x1": str(left), "y1": str(y), "x2": str(width - right), "y2": str(y), "stroke": "#e2e8f0"})
        text(left - 9, y + 4, format_money(value), **{"text-anchor": "end", "font-size": "10"})
    zero_y = ypoint(0)
    ET.SubElement(svg, "line", {"x1": str(left), "y1": str(zero_y), "x2": str(width - right),
                              "y2": str(zero_y), "stroke": "#94a3b8", "stroke-width": "1.2"})
    step = max(1, math.ceil(len(labels) / 10))
    for index, label in enumerate(labels):
        if index % step == 0 or index == len(labels) - 1:
            text(xpoint(index), height - 13, label, **{"text-anchor": "middle", "font-size": "10"})
    for index, item in enumerate(series):
        points = [(xpoint(i), ypoint(float(value))) for i, value in enumerate(item["values"])]
        color = COLORS[index % len(COLORS)]
        ET.SubElement(svg, "polyline", {"points": " ".join(f"{x:.2f},{y:.2f}" for x, y in points),
            "fill": "none", "stroke": color, "stroke-width": "2.3", "stroke-linejoin": "round"})
        for x, y in points:
            ET.SubElement(svg, "circle", {"cx": f"{x:.2f}", "cy": f"{y:.2f}", "r": "2.7", "fill": color})
    return mark_safe(ET.tostring(svg, encoding="unicode"))
