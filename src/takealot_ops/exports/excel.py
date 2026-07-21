"""Create a professional OpenPyXL operations workbook."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.chart import LineChart, Reference  # type: ignore[import-untyped]
from openpyxl.formatting.rule import CellIsRule, FormulaRule  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]

from takealot_ops.metrics.service import DashboardDataset


SHEET_NAMES = (
    "运营总览",
    "单品分析",
    "异常商品",
    "每日汇总",
    "销售明细",
    "流量快照",
    "指标说明",
    "数据质量",
)

_NAVY = "173B68"
_BLUE = "2563EB"
_TEAL = "0F766E"
_PALE_BLUE = "EAF2FF"
_PALE_GREEN = "DCFCE7"
_PALE_RED = "FEE2E2"
_PALE_AMBER = "FEF3C7"
_TEXT = "172033"
_MUTED = "64748B"
_WHITE = "FFFFFF"
_LINE = "DCE4EF"
_HEADER_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color=_WHITE)
_BODY_FONT = Font(name="Microsoft YaHei", size=10, color=_TEXT)
_THIN_BOTTOM = Border(bottom=Side(style="thin", color=_LINE))

_PRODUCT_COLUMNS = [
    ("metric_date", "日期"),
    ("offer_id", "Offer ID"),
    ("sku", "SKU"),
    ("ordered_units", "订购件数"),
    ("effective_units", "有效件数"),
    ("ordered_revenue", "订购销售额（ZAR）"),
    ("page_views_30_days", "近30天浏览量"),
    ("page_views_30_day_average", "近30天日均浏览量"),
    ("page_views_window_net_change", "30天浏览量窗口净变化"),
    ("conversion_percentage_30_days", "近30天转化率（%）"),
    ("conversion_percentage_previous_30_days", "前30天转化率（%）"),
    ("conversion_change_points", "转化率变化（百分点）"),
    ("total_stock", "当前库存"),
    ("offer_status", "商品状态"),
]


def export_excel(dataset: DashboardDataset, destination: Path) -> Path:
    """Write a macro-free workbook derived exclusively from ``dataset``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    overview = workbook.active
    overview.title = SHEET_NAMES[0]
    for name in SHEET_NAMES[1:]:
        workbook.create_sheet(name)

    _build_overview(overview, dataset)
    _build_product_analysis(workbook["单品分析"], dataset.product_daily)
    _build_frame_sheet(
        workbook["异常商品"],
        "异常商品",
        "异常规则输出；详情保持原始可审计内容。",
        dataset.anomalies,
        [
            ("event_date", "日期"),
            ("offer_id", "Offer ID"),
            ("anomaly_type", "异常类型"),
            ("severity", "严重程度"),
            ("explanation", "说明"),
            ("details", "详情"),
            ("created_at", "创建时间"),
        ],
    )
    _build_frame_sheet(
        workbook["每日汇总"],
        "每日汇总",
        "按商品每日数据汇总的店铺日级指标。",
        dataset.store_daily,
        [
            ("metric_date", "日期"),
            ("ordered_units", "订购件数"),
            ("effective_units", "有效件数"),
            ("ordered_revenue", "订购销售额（ZAR）"),
        ],
    )
    _build_frame_sheet(
        workbook["销售明细"],
        "商品每日销售明细/汇总",
        "数据集没有订单行；本表为可审计的商品/日期销售汇总，不代表订单级明细。",
        dataset.product_daily,
        _PRODUCT_COLUMNS,
    )
    _build_frame_sheet(
        workbook["流量快照"],
        "流量快照",
        "流量均为 30 天滚动窗口口径；缺失观测保持空白。",
        dataset.product_daily,
        [
            ("metric_date", "日期"),
            ("offer_id", "Offer ID"),
            ("sku", "SKU"),
            ("page_views_30_days", "近30天浏览量"),
            ("page_views_30_day_average", "近30天日均浏览量"),
            ("page_views_window_net_change", "30天浏览量窗口净变化"),
            ("conversion_percentage_30_days", "近30天转化率（%）"),
            ("conversion_percentage_previous_30_days", "前30天转化率（%）"),
            ("conversion_change_points", "转化率变化（百分点）"),
        ],
    )
    _build_metric_notes(workbook["指标说明"])
    _build_frame_sheet(
        workbook["数据质量"],
        "数据质量",
        "采集和业务规则产生的数据质量事件。",
        dataset.quality_events,
        [
            ("event_id", "事件 ID"),
            ("event_date", "日期"),
            ("event_type", "事件类型"),
            ("severity", "严重程度"),
            ("offer_id", "Offer ID"),
            ("details", "详情"),
            ("created_at", "创建时间"),
        ],
    )
    _add_conditional_formatting(workbook)
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.save(destination)
    return destination


def _build_overview(sheet: Worksheet, dataset: DashboardDataset) -> None:
    _base_sheet(sheet)
    sheet.merge_cells("A1:N1")
    sheet["A1"] = "Takealot 运营总览"
    _style_title(sheet["A1"])
    sheet.merge_cells("A2:N2")
    sheet["A2"] = "只读日报 · KPI 来源于同一 DashboardDataset · 空白表示未知"
    _style_subtitle(sheet["A2"])
    cards = [
        (
            "A3",
            "A4",
            "近7天订购件数",
            _sum_or_none(dataset.store_daily, "ordered_units"),
            "#,##0",
            _PALE_BLUE,
        ),
        (
            "C3",
            "C4",
            "近30天浏览量（商品汇总）",
            _latest_sum_or_none(dataset.product_daily, "metric_date", "page_views_30_days"),
            "#,##0",
            "ECFDF5",
        ),
        (
            "E3",
            "E4",
            "近7天订购销售额",
            _sum_or_none(dataset.store_daily, "ordered_revenue"),
            '"R" #,##0.00',
            _PALE_BLUE,
        ),
        ("G3", "G4", "异常记录数", len(dataset.anomalies), "#,##0", "FFF7ED"),
    ]
    card_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    for label_cell, value_cell, label, value, number_format, fill_color in cards:
        first_column = sheet[label_cell].column
        last_column = first_column + 1
        sheet.merge_cells(
            start_row=sheet[label_cell].row,
            start_column=first_column,
            end_row=sheet[label_cell].row,
            end_column=last_column,
        )
        sheet.merge_cells(
            start_row=sheet[value_cell].row,
            start_column=first_column,
            end_row=sheet[value_cell].row,
            end_column=last_column,
        )
        sheet[label_cell] = label
        sheet[label_cell].font = Font(name="Microsoft YaHei", size=10, bold=True, color=_MUTED)
        sheet[label_cell].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet[value_cell] = _excel_value(value)
        sheet[value_cell].font = Font(name="Microsoft YaHei", size=18, bold=True, color=_NAVY)
        sheet[value_cell].alignment = Alignment(horizontal="center", vertical="center")
        sheet[value_cell].number_format = number_format
        for row in sheet.iter_rows(
            min_row=sheet[label_cell].row,
            max_row=sheet[value_cell].row,
            min_col=first_column,
            max_col=last_column,
        ):
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=fill_color)
                cell.border = card_border
    sheet.merge_cells("A5:N5")
    sheet["A5"] = (
        "口径提示：近30天浏览量为各商品 page_views_30_days 的汇总，不代表独立访客；"
        "有效销量需完成销售状态规则确认。"
    )
    sheet["A5"].fill = PatternFill("solid", fgColor=_PALE_AMBER)
    sheet["A5"].font = Font(name="Microsoft YaHei", size=9, color="92400E")
    sheet["A5"].alignment = Alignment(vertical="center", wrap_text=True)
    sheet["A5"].border = Border(
        left=Side(style="thin", color="F59E0B"),
        right=Side(style="thin", color="F59E0B"),
        top=Side(style="thin", color="F59E0B"),
        bottom=Side(style="thin", color="F59E0B"),
    )
    sheet["A6"] = "近7天订购件数趋势（可审计）"
    sheet["A6"].font = Font(name="Microsoft YaHei", size=11, bold=True, color=_NAVY)
    columns = [
        ("metric_date", "日期"),
        ("ordered_units", "订购件数"),
        ("effective_units", "有效件数"),
        ("ordered_revenue", "订购销售额（ZAR）"),
    ]
    last_row = _write_table(sheet, dataset.store_daily, columns, header_row=8)
    sheet["E8"] = "图表日期标签"
    sheet["E8"].fill = PatternFill("solid", fgColor=_NAVY)
    sheet["E8"].font = _HEADER_FONT
    sheet["E8"].alignment = Alignment(vertical="center", wrap_text=True)
    for row_index in range(9, last_row + 1):
        sheet.cell(row_index, 5, f'=TEXT(A{row_index},"yyyy-mm-dd")')
        sheet.cell(row_index, 5).font = _BODY_FONT
        sheet.cell(row_index, 5).border = _THIN_BOTTOM
    sheet.freeze_panes = "A9"
    sheet.auto_filter.ref = f"A8:E{last_row}"
    if last_row >= 9:
        chart = LineChart()
        chart.title = "近7天每日订购件数趋势"
        chart.style = 13
        chart.height = 7.2
        chart.width = 14.5
        chart.y_axis.title = "件数"
        chart.x_axis.title = "日期"
        chart.add_data(Reference(sheet, min_col=2, min_row=8, max_row=last_row), titles_from_data=True)
        chart.set_categories(Reference(sheet, min_col=5, min_row=9, max_row=last_row))
        chart.legend = None
        # Keep the chart below the A9 freeze boundary to avoid a visual split while scrolling.
        sheet.add_chart(chart, "G9")
    for column, width in {"A": 14, "B": 14, "C": 14, "D": 20, "E": 17, "F": 14, "G": 14, "H": 14, "I": 14, "J": 14, "K": 14, "L": 14, "M": 14, "N": 14}.items():
        sheet.column_dimensions[column].width = width
    sheet.row_dimensions[1].height = 34
    sheet.row_dimensions[2].height = 24
    sheet.row_dimensions[3].height = 24
    sheet.row_dimensions[4].height = 38
    sheet.row_dimensions[5].height = 30
    sheet.row_dimensions[6].height = 26


def _build_product_analysis(sheet: Worksheet, frame: pd.DataFrame) -> None:
    _base_sheet(sheet)
    sheet.merge_cells("A1:Z1")
    sheet["A1"] = "单品分析"
    _style_title(sheet["A1"])
    sheet.merge_cells("A2:Z2")
    sheet["A2"] = "表格保留全部商品每日数据；图表默认选择首个 Offer ID，源范围可见。"
    _style_subtitle(sheet["A2"])
    ordered = frame.sort_values(["offer_id", "metric_date"], na_position="last") if not frame.empty else frame
    selected: str | None = None
    if not ordered.empty and not ordered["offer_id"].dropna().empty:
        selected = str(ordered["offer_id"].dropna().astype(str).iloc[0])
    sheet["A3"] = "图表商品"
    sheet["B3"] = selected
    sheet["A3"].font = Font(name="Microsoft YaHei", bold=True, color=_MUTED)
    sheet["B3"].font = Font(name="Microsoft YaHei", bold=True, color=_NAVY)
    last_row = _write_table(sheet, ordered, _PRODUCT_COLUMNS, header_row=5)
    sheet.freeze_panes = "A6"
    sheet.auto_filter.ref = f"A5:N{last_row}"
    if selected is not None:
        selected_count = int((ordered["offer_id"].astype(str) == selected).sum())
        chart_last_row = 5 + selected_count
        sheet["P5"] = "图表日期标签"
        sheet["P5"].fill = PatternFill("solid", fgColor=_NAVY)
        sheet["P5"].font = _HEADER_FONT
        sheet["P5"].alignment = Alignment(vertical="center", wrap_text=True)
        for row_index in range(6, chart_last_row + 1):
            sheet.cell(row_index, 16, f'=TEXT(A{row_index},"yyyy-mm-dd")')
            sheet.cell(row_index, 16).font = _BODY_FONT
            sheet.cell(row_index, 16).border = _THIN_BOTTOM
        chart = LineChart()
        chart.title = f"商品 {selected} 订购件数趋势"
        chart.style = 13
        chart.height = 7.2
        chart.width = 14.5
        chart.y_axis.title = "件数"
        chart.x_axis.title = "日期"
        chart.add_data(Reference(sheet, min_col=4, min_row=5, max_row=chart_last_row), titles_from_data=True)
        chart.set_categories(Reference(sheet, min_col=16, min_row=6, max_row=chart_last_row))
        chart.legend = None
        sheet.add_chart(chart, "R4")
    _set_widths(sheet, _PRODUCT_COLUMNS)
    sheet.column_dimensions["O"].width = 3
    sheet.column_dimensions["P"].width = 16
    sheet.column_dimensions["Q"].width = 3


def _build_frame_sheet(
    sheet: Worksheet,
    title: str,
    subtitle: str,
    frame: pd.DataFrame,
    columns: list[tuple[str, str]],
) -> None:
    _base_sheet(sheet)
    last_column = max(1, len(columns))
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    sheet["A1"] = title
    _style_title(sheet["A1"])
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
    sheet["A2"] = subtitle
    _style_subtitle(sheet["A2"])
    last_row = _write_table(sheet, frame, columns, header_row=3)
    sheet.freeze_panes = "A4"
    sheet.auto_filter.ref = f"A3:{get_column_letter(last_column)}{last_row}"
    _set_widths(sheet, columns)


def _build_metric_notes(sheet: Worksheet) -> None:
    rows = pd.DataFrame(
        [
            {"metric": "ordered_units", "label": "订购件数", "definition": "所有销售状态的订购数量合计。"},
            {"metric": "effective_units", "label": "有效件数", "definition": "仅计入配置为有效状态的数量。"},
            {"metric": "ordered_revenue", "label": "订购销售额", "definition": "销售价乘以订购数量；币种为 ZAR。"},
            {"metric": "page_views_30_days", "label": "近30天浏览量", "definition": "API 返回的 30 天滚动窗口值。"},
            {"metric": "page_views_30_day_average", "label": "近30天日均浏览量", "definition": "近30天浏览量除以 30 的派生平均值。"},
            {"metric": "page_views_window_net_change", "label": "30天浏览量窗口净变化", "definition": "相邻快照的滚动窗口净变化，仅作趋势参考。"},
            {"metric": "conversion_change_points", "label": "转化率变化（百分点）", "definition": "近 30 天与前 30 天转化率之差。"},
            {"metric": "missing_values", "label": "缺失值", "definition": "空白表示未知，不推断为零。"},
        ]
    )
    _build_frame_sheet(
        sheet,
        "指标说明",
        "指标口径与使用限制；报告不包含写入或刷新功能。",
        rows,
        [("metric", "字段"), ("label", "展示名称"), ("definition", "说明")],
    )
    sheet.column_dimensions["A"].width = 38
    sheet.column_dimensions["B"].width = 28
    sheet.column_dimensions["C"].width = 62


def _write_table(
    sheet: Worksheet,
    frame: pd.DataFrame,
    columns: list[tuple[str, str]],
    *,
    header_row: int,
) -> int:
    for column_index, (_, label) in enumerate(columns, start=1):
        cell = sheet.cell(header_row, column_index, label)
        cell.fill = PatternFill("solid", fgColor=_NAVY)
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[header_row].height = 30
    for row_index, record in enumerate(frame.to_dict(orient="records"), start=header_row + 1):
        for column_index, (key, _) in enumerate(columns, start=1):
            cell = sheet.cell(row_index, column_index, _excel_value(record.get(key)))
            cell.font = _BODY_FONT
            cell.border = _THIN_BOTTOM
            cell.alignment = Alignment(vertical="top", wrap_text=key in {"details", "explanation"})
            cell.number_format = _number_format(key)
        if (row_index - header_row) % 2 == 0:
            for column_index in range(1, len(columns) + 1):
                sheet.cell(row_index, column_index).fill = PatternFill("solid", fgColor="F8FAFC")
    return max(header_row, header_row + len(frame))


def _base_sheet(sheet: Worksheet) -> None:
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.outlinePr.summaryBelow = True


def _style_title(cell: Any) -> None:
    cell.fill = PatternFill("solid", fgColor=_NAVY)
    cell.font = Font(name="Microsoft YaHei", size=18, bold=True, color=_WHITE)
    cell.alignment = Alignment(vertical="center")
    cell.parent.row_dimensions[cell.row].height = 34


def _style_subtitle(cell: Any) -> None:
    cell.fill = PatternFill("solid", fgColor=_PALE_BLUE)
    cell.font = Font(name="Microsoft YaHei", size=10, color=_MUTED)
    cell.alignment = Alignment(vertical="center", wrap_text=True)
    cell.parent.row_dimensions[cell.row].height = 28


def _set_widths(sheet: Worksheet, columns: list[tuple[str, str]]) -> None:
    for index, (key, label) in enumerate(columns, start=1):
        width = max(12, min(42, len(label) * 2 + 4))
        if key in {"details", "explanation", "definition"}:
            width = 36
        elif key in {"offer_id", "event_id"}:
            width = 20
        elif key in {"created_at", "updated_at", "captured_at"}:
            width = 22
        sheet.column_dimensions[get_column_letter(index)].width = width


def _add_conditional_formatting(workbook: Workbook) -> None:
    green = PatternFill("solid", fgColor=_PALE_GREEN)
    red = PatternFill("solid", fgColor=_PALE_RED)
    amber = PatternFill("solid", fgColor=_PALE_AMBER)
    product = workbook["单品分析"]
    if product.max_row >= 6:
        product.conditional_formatting.add(
            f"I6:I{product.max_row}", CellIsRule(operator="greaterThan", formula=["0"], fill=green)
        )
        product.conditional_formatting.add(
            f"I6:I{product.max_row}", CellIsRule(operator="lessThan", formula=["0"], fill=red)
        )
        product.conditional_formatting.add(
            f"N6:N{product.max_row}", FormulaRule(formula=["N6=\"buyable\""], fill=green)
        )
        product.conditional_formatting.add(
            f"N6:N{product.max_row}", FormulaRule(formula=["AND(N6<>\"\",N6<>\"buyable\")"], fill=red)
        )
    traffic = workbook["流量快照"]
    if traffic.max_row >= 4:
        traffic.conditional_formatting.add(
            f"F4:F{traffic.max_row}", CellIsRule(operator="greaterThan", formula=["0"], fill=green)
        )
        traffic.conditional_formatting.add(
            f"F4:F{traffic.max_row}", CellIsRule(operator="lessThan", formula=["0"], fill=red)
        )
    anomalies = workbook["异常商品"]
    if anomalies.max_row >= 4:
        anomalies.conditional_formatting.add(
            f"D4:D{anomalies.max_row}", FormulaRule(formula=["D4=\"critical\""], fill=red)
        )
        anomalies.conditional_formatting.add(
            f"D4:D{anomalies.max_row}", FormulaRule(formula=["D4=\"warning\""], fill=amber)
        )


def _excel_value(value: object) -> object:
    if value is None or _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if hasattr(value, "item"):
        return _excel_value(value.item())
    return value


def _sum_or_none(frame: pd.DataFrame, column: str) -> int | float | None:
    if frame.empty or column not in frame:
        return None
    value = frame[column].sum(min_count=1)
    if _is_missing(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value if isinstance(value, (int, float)) else float(value)


def _latest_sum_or_none(
    frame: pd.DataFrame, date_column: str, value_column: str
) -> int | float | None:
    if frame.empty or date_column not in frame or value_column not in frame:
        return None
    dates = frame[date_column].dropna()
    if dates.empty:
        return None
    value = frame.loc[frame[date_column] == dates.max(), value_column].sum(min_count=1)
    if _is_missing(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value if isinstance(value, (int, float)) else float(value)


def _number_format(key: str) -> str:
    if key in {"metric_date", "event_date"}:
        return "yyyy-mm-dd"
    if key in {"created_at", "updated_at", "captured_at"}:
        return "yyyy-mm-dd hh:mm"
    if key in {"ordered_revenue", "selling_price", "rrp", "benchmark_price"}:
        return '"R" #,##0.00'
    if key in {
        "ordered_units",
        "effective_units",
        "page_views_30_days",
        "page_views_window_net_change",
        "total_stock",
    }:
        return "#,##0"
    if key in {
        "page_views_30_day_average",
        "conversion_percentage_30_days",
        "conversion_percentage_previous_30_days",
        "conversion_change_points",
    }:
        return '0.0"%"' if "conversion" in key else "#,##0.0"
    return "General"


def _is_missing(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value) or value is pd.NA or value is pd.NaT
