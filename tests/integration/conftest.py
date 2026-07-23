from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from takealot_ops.metrics.service import DashboardDataset, PRODUCT_DAILY_COLUMNS


@pytest.fixture
def dashboard_dataset() -> DashboardDataset:
    product_daily = pd.DataFrame(
        [
            {
                "metric_date": date(2026, 7, 19),
                "offer_id": "offer-a",
                "sku": "SKU-A",
                "ordered_units": 3,
                "effective_units": 2,
                "ordered_revenue": 599.97,
                "page_views_30_days": 900,
                "page_views_30_day_average": 30.0,
                "page_views_window_net_change": None,
                "conversion_percentage_30_days": 3.5,
                "conversion_percentage_previous_30_days": 3.0,
                "conversion_change_points": 0.5,
                "total_stock": 12,
                "offer_status": "buyable",
            },
            {
                "metric_date": date(2026, 7, 20),
                "offer_id": "offer-a",
                "sku": "SKU-A",
                "ordered_units": 5,
                "effective_units": 4,
                "ordered_revenue": 999.95,
                "page_views_30_days": 960,
                "page_views_30_day_average": 32.0,
                "page_views_window_net_change": 60,
                "conversion_percentage_30_days": 4.0,
                "conversion_percentage_previous_30_days": 3.5,
                "conversion_change_points": 0.5,
                "total_stock": 7,
                "offer_status": "buyable",
            },
            {
                "metric_date": date(2026, 7, 20),
                "offer_id": "offer-b",
                "sku": None,
                "ordered_units": 2,
                "effective_units": 0,
                "ordered_revenue": 300.0,
                "page_views_30_days": None,
                "page_views_30_day_average": None,
                "page_views_window_net_change": None,
                "conversion_percentage_30_days": None,
                "conversion_percentage_previous_30_days": None,
                "conversion_change_points": None,
                "total_stock": None,
                "offer_status": None,
            },
        ],
        columns=PRODUCT_DAILY_COLUMNS,
    )
    return DashboardDataset(
        store_daily=pd.DataFrame(
            [
                {
                    "metric_date": date(2026, 7, 19),
                    "ordered_units": 3,
                    "effective_units": 2,
                    "ordered_revenue": 599.97,
                },
                {
                    "metric_date": date(2026, 7, 20),
                    "ordered_units": 7,
                    "effective_units": 4,
                    "ordered_revenue": 1299.95,
                },
            ]
        ),
        product_daily=product_daily,
        offer_current=pd.DataFrame(
            [
                {
                    "offer_id": "offer-a",
                    "tsin_id": "tsin-a",
                    "sku": "SKU-A",
                    "barcode": "100000000001",
                    "title": "示例商品 A",
                    "selling_price": 199.99,
                    "rrp": 229.99,
                    "benchmark_price": 205.0,
                    "status": "buyable",
                    "image_url": "https://example.invalid/a.png",
                    "productline_id": "line-a",
                    "conversion_percentage_30_days": 4.0,
                    "conversion_percentage_previous_30_days": 3.5,
                    "page_views_30_days": 960,
                    "quantity_returned_30_days": 1,
                    "total_wishlist": 42,
                    "wishlist_30_days": 8,
                    "listing_quality": "good",
                    "discount_percentage": 13.0,
                    "created_at": datetime(2026, 1, 15, 10, 34, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
                    "captured_at": datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
                    "total_stock": 7,
                },
                {
                    "offer_id": "offer-b",
                    "tsin_id": "tsin-b",
                    "sku": None,
                    "barcode": None,
                    "title": "示例商品 B",
                    "selling_price": 150.0,
                    "rrp": None,
                    "benchmark_price": None,
                    "status": None,
                    "image_url": None,
                    "productline_id": None,
                    "conversion_percentage_30_days": None,
                    "conversion_percentage_previous_30_days": None,
                    "page_views_30_days": None,
                    "quantity_returned_30_days": None,
                    "total_wishlist": None,
                    "wishlist_30_days": None,
                    "listing_quality": None,
                    "discount_percentage": None,
                    "created_at": None,
                    "updated_at": None,
                    "captured_at": datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
                    "total_stock": None,
                },
            ]
        ),
        anomalies=pd.DataFrame(
            [
                {
                    "event_date": date(2026, 7, 20),
                    "offer_id": "offer-b",
                    "anomaly_type": "non_buyable",
                    "severity": "warning",
                    "explanation": "商品当前不可购买。",
                    "details": None,
                    "created_at": datetime(2026, 7, 20, 10, tzinfo=timezone.utc),
                }
            ]
        ),
        quality_events=pd.DataFrame(
            [
                {
                    "event_id": "quality-1",
                    "event_date": date(2026, 7, 20),
                    "event_type": "missing_sku",
                    "severity": "warning",
                    "offer_id": "offer-b",
                    "details": "SKU 缺失",
                    "created_at": datetime(2026, 7, 20, 10, tzinfo=timezone.utc),
                }
            ]
        ),
    )


@pytest.fixture
def empty_dashboard_dataset() -> DashboardDataset:
    return DashboardDataset(
        store_daily=pd.DataFrame(
            columns=["metric_date", "ordered_units", "effective_units", "ordered_revenue"]
        ),
        product_daily=pd.DataFrame(columns=PRODUCT_DAILY_COLUMNS),
        offer_current=pd.DataFrame(),
        anomalies=pd.DataFrame(),
        quality_events=pd.DataFrame(),
    )
