from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd

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
    offer_a = next(item for item in payload["items"] if item["offer_id"] == "offer-a")
    offer_b = next(item for item in payload["items"] if item["offer_id"] == "offer-b")
    assert "page_views_7_day_estimate" not in offer_a
    assert offer_a["first_listed_at"] == "2026-01-15 12:34"
    assert offer_a["first_listed_source"] == "platform"
    assert offer_a["latest_restock_date"] is None
    assert offer_b["first_listed_at"] == "2026-07-20"
    assert offer_b["first_listed_source"] == "first_observed"


def test_erp_quadrants_record_latest_restock_from_stock_increase(
    dashboard_dataset: DashboardDataset,
) -> None:
    offer_history = dashboard_dataset.offer_history.copy(deep=True)
    offer_history = pd.concat(
        [
            pd.DataFrame(
                [
                    {
                        "snapshot_date": date(2026, 7, 19),
                        "offer_id": "offer-a",
                        "captured_at": "2026-07-19T09:00:00Z",
                        "total_stock": 4,
                    }
                ]
            ),
            offer_history,
        ],
        ignore_index=True,
    )
    payload = build_quadrant_payload(
        replace(dashboard_dataset, offer_history=offer_history),
        AS_OF,
        50,
    )

    offer_a = next(item for item in payload["items"] if item["offer_id"] == "offer-a")
    assert offer_a["latest_restock_date"] == "2026-07-20 17:00"
    assert offer_a["latest_restock_increase"] == 3


def test_erp_risks_are_localized_and_count_unique_latest_products(
    dashboard_dataset: DashboardDataset,
) -> None:
    payload = build_risk_payload(dashboard_dataset, AS_OF)

    assert payload["latest_metric_date"] == "2026-07-20"
    assert payload["summary"]["latest_anomaly_products"] == 1
    anomaly = payload["latest_anomalies"][0]
    assert anomaly["anomaly_label"] == "不可购买"
    assert anomaly["title"] == "示例商品 B"
    assert anomaly["metric_date"] == "2026-07-20"
    assert anomaly["ordered_units_7_days"] == 2
    assert anomaly["selling_price"] == 150.0
    assert anomaly["first_listed_at"] == "2026-07-20"
    assert anomaly["first_listed_source"] == "first_observed"
    assert payload["quality_events"][0]["event_label"] == "库存编码缺失"


def test_erp_risks_include_quadrant_and_extended_product_detail(
    dashboard_dataset: DashboardDataset,
) -> None:
    anomalies = pd.DataFrame(
        [
            {
                "event_date": date(2026, 7, 20),
                "offer_id": "offer-a",
                "anomaly_type": "sales_drop",
                "severity": "critical",
                "explanation": "当日下单件数低于历史基准。",
                "details": {
                    "short_window_days": 3,
                    "long_window_days": 15,
                    "short_window_average_units": 1.0,
                    "long_window_average_units": 2.5,
                },
            }
        ]
    )

    payload = build_risk_payload(
        replace(dashboard_dataset, anomalies=anomalies),
        AS_OF,
    )

    anomaly = payload["latest_anomalies"][0]
    assert anomaly["anomaly_label"] == "销量下降"
    assert anomaly["explanation"] == "近3天下单件数日均值低于近15天下单件数日均值。"
    assert anomaly["title"] == "示例商品 A"
    assert anomaly["sku"] == "SKU-A"
    assert anomaly["image_url"] == "https://example.invalid/a.png"
    assert anomaly["total_stock"] == 7
    assert anomaly["page_views_30_days"] == 960
    assert anomaly["ordered_units_7_days"] == 8
    assert anomaly["conversion_percentage_30_days"] == 4.0
    assert anomaly["effective_units"] == 4
    assert anomaly["ordered_revenue"] == 999.95
    assert anomaly["status_label"] == "可购买"
    assert anomaly["first_listed_at"] == "2026-01-15 12:34"
    assert anomaly["first_listed_source"] == "platform"
    assert anomaly["latest_restock_date"] is None
    assert anomaly["details"]["short_window_average_units"] == 1.0
    assert anomaly["details"]["long_window_average_units"] == 2.5
    assert anomaly["details"]["sales_series_covered_days"] == 15
    assert len(anomaly["details"]["sales_daily_series"]) == 15
    assert anomaly["details"]["sales_daily_series"][0] == {
        "date": "2026-07-06",
        "ordered_units": 0,
    }
    assert anomaly["details"]["sales_daily_series"][-2:] == [
        {"date": "2026-07-19", "ordered_units": 3},
        {"date": "2026-07-20", "ordered_units": 5},
    ]


def test_erp_risk_traffic_evidence_uses_recorded_sales_days_only(
    dashboard_dataset: DashboardDataset,
) -> None:
    anomalies = pd.DataFrame(
        [
            {
                "event_date": date(2026, 7, 20),
                "offer_id": "offer-a",
                "anomaly_type": "high_views_low_conversion",
                "severity": "warning",
                "explanation": "流量高但转化率低。",
                "details": {
                    "page_views_30_days": 960,
                    "high_views_threshold": 800,
                    "conversion_percentage_30_days": 4.0,
                    "low_conversion_threshold": 5.0,
                },
            }
        ]
    )

    payload = build_risk_payload(
        replace(dashboard_dataset, anomalies=anomalies),
        AS_OF,
    )

    details = payload["latest_anomalies"][0]["details"]
    assert details["sales_window_days"] == 2
    assert details["sales_window_total_units"] == 8
    assert details["sales_window_start"] == "2026-07-19"
    assert details["sales_window_end"] == "2026-07-20"
    assert details["sales_window_complete"] is False
