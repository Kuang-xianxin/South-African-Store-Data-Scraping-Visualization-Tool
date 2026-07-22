from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from takealot_ops.dashboard.app import (
    CHINESE_UI_STYLES,
    PLOTLY_CHART_CONFIG,
    _calendar_window_sum,
    _sum_value,
    create_read_only_engine,
    filter_as_of,
    load_dashboard_dataset,
    search_products,
)
from takealot_ops.dashboard.charts import (
    build_quadrant_figure,
    build_sales_figure,
    build_traffic_figure,
)
from takealot_ops.dashboard.labels import (
    FIELD_LABELS,
    PAGE_NAMES,
    QUADRANT_LABELS,
    TRAFFIC_METRIC_LABELS,
)
from takealot_ops.settings import DashboardSettings, SettingsError


def _product_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "metric_date": date(2026, 7, 19),
                "offer_id": "offer-a",
                "sku": "SKU-A",
                "ordered_units": 3.0,
                "page_views_30_days": 900.0,
                "page_views_30_day_average": 30.0,
                "page_views_window_net_change": None,
            },
            {
                "metric_date": date(2026, 7, 20),
                "offer_id": "offer-a",
                "sku": "SKU-A",
                "ordered_units": None,
                "page_views_30_days": None,
                "page_views_30_day_average": None,
                "page_views_window_net_change": 60.0,
            },
            {
                "metric_date": date(2026, 7, 21),
                "offer_id": "offer-b",
                "sku": None,
                "ordered_units": 2.0,
                "page_views_30_days": 300.0,
                "page_views_30_day_average": 10.0,
                "page_views_window_net_change": -30.0,
            },
        ]
    )


def _offer_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "offer_id": "offer-a",
                "sku": "SKU-A",
                "tsin_id": "TSIN-A",
                "barcode": "600100000001",
                "title": "Premium Solar Lantern",
            },
            {
                "offer_id": "offer-b",
                "sku": "SKU-B",
                "tsin_id": "TSIN-B",
                "barcode": "600100000002",
                "title": "Compact Travel Kettle",
            },
        ]
    )


def test_traffic_figure_contains_only_approved_metric_names() -> None:
    figure = build_traffic_figure(_product_rows())

    assert {trace.name for trace in figure.data} == set(TRAFFIC_METRIC_LABELS.values())


def test_sales_and_traffic_are_separate_figures_without_dual_axis() -> None:
    sales = build_sales_figure(_product_rows())
    traffic = build_traffic_figure(_product_rows())

    assert sales is not traffic
    assert "yaxis2" not in sales.layout.to_plotly_json()
    assert "yaxis2" not in traffic.layout.to_plotly_json()


def test_missing_values_remain_plotly_gaps_instead_of_zeroes() -> None:
    sales = build_sales_figure(_product_rows())
    traffic = build_traffic_figure(_product_rows())

    assert pd.isna(list(sales.data[0].y)[1])
    assert pd.isna(list(sales.data[1].y)[1])
    assert pd.isna(list(traffic.data[0].y)[1])
    assert all(value != 0 for trace in traffic.data for value in trace.y if pd.notna(value))


def test_empty_frames_build_useful_empty_figures() -> None:
    sales = build_sales_figure(pd.DataFrame())
    traffic = build_traffic_figure(pd.DataFrame())

    assert sales.layout.annotations[0].text == "暂无销售数据"
    assert traffic.layout.annotations[0].text == "暂无流量快照数据"


def test_product_search_matches_all_identity_fields_and_title_case_insensitively() -> None:
    product_daily = _product_rows()
    offers = _offer_rows()

    queries = {
        "sku-b": "offer-b",
        "offer-a": "offer-a",
        "tsin-b": "offer-b",
        "600100000001": "offer-a",
        "SOLAR lantern": "offer-a",
    }
    for query, expected_offer_id in queries.items():
        result = search_products(product_daily, offers, query)
        assert set(result["offer_id"]) == {expected_offer_id}


def test_product_search_handles_empty_and_partial_identity_frames() -> None:
    assert search_products(pd.DataFrame(), pd.DataFrame(), "anything").empty
    result = search_products(
        _product_rows(), pd.DataFrame([{"offer_id": "offer-a", "title": "Lantern"}]), "lantern"
    )
    assert set(result["offer_id"]) == {"offer-a"}


def test_page_filter_never_shows_rows_after_as_of() -> None:
    result = filter_as_of(_product_rows(), date(2026, 7, 20), "metric_date")

    assert set(result["metric_date"]) == {date(2026, 7, 19), date(2026, 7, 20)}


def test_navigation_and_quadrant_labels_are_closed_approved_mappings() -> None:
    assert PAGE_NAMES == (
        "店铺总览",
        "单品分析",
        "经营四象限",
        "异常商品",
        "数据质量",
        "NFT102 日报更新",
        "导出中心",
    )
    assert QUADRANT_LABELS == {
        "star": "明星商品",
        "conversion_issue": "转化问题商品",
        "potential": "潜力商品",
        "optimize": "待优化商品",
        "unclassified": "未分类",
    }


def test_frontend_controls_and_identifiers_use_chinese_labels() -> None:
    assert "选择文件" in CHINESE_UI_STYLES
    assert "单个文件不超过100兆字节" in CHINESE_UI_STYLES
    assert PLOTLY_CHART_CONFIG["displayModeBar"] is False
    assert FIELD_LABELS["offer_id"] == "商品编号"
    assert FIELD_LABELS["sku"] == "库存编码"
    assert FIELD_LABELS["tsin_id"] == "平台商品编号"
    assert FIELD_LABELS["rrp"] == "建议零售价"


def test_quadrant_chart_labels_traffic_axis_accurately() -> None:
    classified = pd.DataFrame(
        [
            {
                "offer_id": "offer-a",
                "page_views_30_days": 900,
                "ordered_units": 3,
                "quadrant": "star",
            }
        ]
    )

    figure = build_quadrant_figure(classified)

    assert figure.layout.xaxis.title.text == "近30天浏览量"


def test_all_missing_kpi_value_stays_unknown_instead_of_becoming_zero() -> None:
    frame = pd.DataFrame({"page_views_30_days": [None, float("nan")]})

    assert _sum_value(frame, "page_views_30_days", integer=True) == "—"


def test_dashboard_sqlite_engine_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "readonly.db"
    database_url = f"sqlite:///{database.as_posix()}"
    writable = create_engine(database_url)
    with writable.begin() as connection:
        connection.execute(text("CREATE TABLE example (value INTEGER)"))
    writable.dispose()

    readonly = create_read_only_engine(database_url)
    with pytest.raises(OperationalError, match="readonly"):
        with readonly.begin() as connection:
            connection.execute(text("INSERT INTO example VALUES (1)"))
    readonly.dispose()


@pytest.mark.parametrize(
    "database_url",
    ["mysql+pymysql://localhost/takealot", "sqlite+aiosqlite:///takealot.db"],
)
def test_dashboard_engine_rejects_unsupported_dialect_before_driver_import(
    database_url: str,
) -> None:
    with pytest.raises(SettingsError, match="本机文件数据库"):
        create_read_only_engine(database_url)


def test_malformed_database_url_returns_friendly_load_error(tmp_path: Path) -> None:
    settings = DashboardSettings(
        project_root=tmp_path,
        database_url="not a database url [",
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )

    dataset, error = load_dashboard_dataset(settings, date(2026, 7, 20))

    assert dataset is None
    assert error is not None
    assert "本地数据暂不可用" in error


def test_quadrant_chart_skips_non_numeric_boundaries() -> None:
    classified = pd.DataFrame(
        [
            {
                "offer_id": "offer-missing",
                "page_views_30_days": None,
                "ordered_units": None,
                "quadrant": "unclassified",
            }
        ]
    )
    classified.attrs = {
        "page_views_boundary": float("nan"),
        "ordered_units_boundary": float("nan"),
    }

    figure = build_quadrant_figure(classified)

    assert not figure.layout.shapes


def test_seven_day_kpi_uses_calendar_window_not_last_seven_rows() -> None:
    sparse = pd.DataFrame(
        {
            "metric_date": [date(2026, 7, 1), date(2026, 7, 20)],
            "ordered_units": [9, 2],
        }
    )
    all_missing_recent = pd.DataFrame(
        {
            "metric_date": [date(2026, 7, 1), date(2026, 7, 19), date(2026, 7, 20)],
            "ordered_units": [9, None, None],
        }
    )

    assert _calendar_window_sum(sparse, "ordered_units", days=7) == 2
    assert _calendar_window_sum(all_missing_recent, "ordered_units", days=7) is None
