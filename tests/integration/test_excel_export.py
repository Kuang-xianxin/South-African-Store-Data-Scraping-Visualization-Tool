from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import load_workbook

from takealot_ops.exports.excel import export_excel
from takealot_ops.metrics.service import DashboardDataset


SHEET_NAMES = [
    "运营总览",
    "单品分析",
    "异常商品",
    "每日汇总",
    "销售明细",
    "流量快照",
    "指标说明",
    "数据质量",
]


def test_excel_contains_all_eight_required_sheets(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    destination = export_excel(dashboard_dataset, tmp_path / "report.xlsx")

    workbook = load_workbook(destination, read_only=False, data_only=False)
    try:
        assert workbook.sheetnames == SHEET_NAMES
    finally:
        workbook.close()


def test_excel_has_no_vba_project_and_opens_with_openpyxl(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    destination = export_excel(dashboard_dataset, tmp_path / "report.xlsx")

    workbook = load_workbook(destination, keep_vba=False)
    try:
        assert workbook.vba_archive is None
        assert not workbook._external_links
    finally:
        workbook.close()


def test_excel_key_totals_match_dataset(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    destination = export_excel(dashboard_dataset, tmp_path / "report.xlsx")

    workbook = load_workbook(destination, data_only=False)
    try:
        overview = workbook["运营总览"]
        latest_date = dashboard_dataset.product_daily["metric_date"].max()
        latest_views = dashboard_dataset.product_daily.loc[
            dashboard_dataset.product_daily["metric_date"] == latest_date,
            "page_views_30_days",
        ].sum(min_count=1)
        assert overview["A3"].value == "近7天订购件数"
        assert overview["A4"].value == dashboard_dataset.store_daily["ordered_units"].sum()
        assert overview["C3"].value == "近30天浏览量（商品汇总）"
        assert overview["C4"].value == latest_views
        assert overview["E3"].value == "近7天订购销售额"
        assert overview["E4"].value == dashboard_dataset.store_daily["ordered_revenue"].sum()
        assert overview["G3"].value == "异常记录数"
        assert overview["G4"].value == len(dashboard_dataset.anomalies)
        assert overview["A5"].value.startswith("口径提示：")
    finally:
        workbook.close()


def test_excel_has_filters_freezes_charts_and_conditional_formatting(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    destination = export_excel(dashboard_dataset, tmp_path / "report.xlsx")

    workbook = load_workbook(destination)
    try:
        overview = workbook["运营总览"]
        product = workbook["单品分析"]
        assert overview.freeze_panes is not None
        assert overview.auto_filter.ref
        assert len(overview._charts) == 1
        assert overview._charts[0].anchor._from.row == 8
        assert product.freeze_panes is not None
        assert product.auto_filter.ref
        assert len(product._charts) == 1
        assert any(len(sheet.conditional_formatting) for sheet in workbook.worksheets)
        assert workbook["销售明细"]["A1"].value == "商品每日销售明细/汇总"
    finally:
        workbook.close()


def test_excel_handles_empty_frames_and_preserves_blank_unknowns(
    tmp_path: Path, empty_dashboard_dataset: DashboardDataset
) -> None:
    destination = export_excel(empty_dashboard_dataset, tmp_path / "empty.xlsx")

    workbook = load_workbook(destination, data_only=False)
    try:
        assert workbook.sheetnames == SHEET_NAMES
        assert workbook["运营总览"]["A4"].value is None
        assert workbook["流量快照"].max_row == 3
    finally:
        workbook.close()


@pytest.mark.parametrize(
    "untrusted_text",
    [
        '=HYPERLINK("https://example.invalid", "click")',
        "+SUM(1,1)",
        "-2+3",
        "@SUM(1,1)",
    ],
)
def test_excel_writes_untrusted_text_as_literal_cells(
    tmp_path: Path,
    dashboard_dataset: DashboardDataset,
    untrusted_text: str,
) -> None:
    product_daily = dashboard_dataset.product_daily.copy(deep=True)
    product_daily.at[0, "sku"] = untrusted_text
    unsafe_dataset = replace(dashboard_dataset, product_daily=product_daily)

    destination = export_excel(unsafe_dataset, tmp_path / "safe.xlsx")
    workbook = load_workbook(destination, data_only=False)
    try:
        cell = workbook["销售明细"]["C4"]
        assert cell.data_type == "s"
        assert cell.value == f"'{untrusted_text}"
    finally:
        workbook.close()
