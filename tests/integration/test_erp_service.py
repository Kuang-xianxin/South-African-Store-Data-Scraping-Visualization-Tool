from __future__ import annotations

from datetime import date

from takealot_ops.erp.service import (
    build_product_detail_payload,
    build_products_payload,
    build_quadrant_payload,
    build_risk_payload,
    build_summary_payload,
)
from takealot_ops.metrics.service import DashboardDataset


AS_OF = date(2026, 7, 20)


def test_erp_summary_and_products_share_canonical_metric_dataset(
    dashboard_dataset: DashboardDataset,
) -> None:
    summary = build_summary_payload(dashboard_dataset, AS_OF)
    products = build_products_payload(dashboard_dataset, AS_OF)

    assert summary["latest_metric_date"] == "2026-07-20"
    assert summary["kpis"]["latest_ordered_units"] == 7
    assert summary["kpis"]["latest_ordered_revenue"] == 1299.95
    assert summary["kpis"]["seven_day_ordered_units"] == 10
    assert [item["offer_id"] for item in products["items"]] == [
        "offer-a",
        "offer-b",
    ]
    assert products["items"][0]["title"] == "示例商品 A"


def test_erp_product_detail_keeps_daily_history_and_rolling_traffic_label_data(
    dashboard_dataset: DashboardDataset,
) -> None:
    detail = build_product_detail_payload(dashboard_dataset, AS_OF, "offer-a")

    assert detail["identity"]["title"] == "示例商品 A"
    assert detail["kpis"]["latest_ordered_units"] == 5
    assert detail["kpis"]["seven_day_ordered_units"] == 8
    assert detail["kpis"]["page_views_30_days"] == 960
    assert len(detail["history"]) == 2


def test_erp_quadrants_keep_real_boundaries_after_identity_enrichment(
    dashboard_dataset: DashboardDataset,
) -> None:
    payload = build_quadrant_payload(dashboard_dataset, AS_OF, 50)

    assert payload["window_start"] == "2026-07-14"
    assert payload["window_end"] == "2026-07-20"
    assert payload["boundaries"]["page_views"] == 960
    assert sum(payload["counts"].values()) == 2
    assert {item["offer_id"] for item in payload["items"]} == {
        "offer-a",
        "offer-b",
    }


def test_erp_risks_are_localized_and_count_unique_latest_products(
    dashboard_dataset: DashboardDataset,
) -> None:
    payload = build_risk_payload(dashboard_dataset, AS_OF)

    assert payload["latest_metric_date"] == "2026-07-20"
    assert payload["summary"]["latest_anomaly_products"] == 1
    assert payload["latest_anomalies"][0]["anomaly_label"] == "不可购买"
    assert payload["quality_events"][0]["event_label"] == "库存编码缺失"
