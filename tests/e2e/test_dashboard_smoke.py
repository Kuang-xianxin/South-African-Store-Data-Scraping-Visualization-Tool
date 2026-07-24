from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from takealot_ops.dashboard.labels import PAGE_NAMES
from takealot_ops.settings import DashboardSettings, SettingsError
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    AnomalyEvent,
    CollectionRun,
    DailyProductMetric,
    DataQualityEvent,
    OfferSnapshot,
)


APP_PATH = (
    Path(__file__).parents[2] / "src" / "takealot_ops" / "dashboard" / "app.py"
)
PROJECT_ROOT = Path(__file__).parents[2]


def test_dashboard_defaults_to_lan_without_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.delenv("TAKEALOT_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("TAKEALOT_DASHBOARD_PORT", raising=False)
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")

    settings = DashboardSettings.from_env(tmp_path)

    assert settings.dashboard_host == "0.0.0.0"
    assert settings.dashboard_port == 8501


def test_dashboard_navigates_all_pages_without_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")

    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()

    assert not app.exception
    assert tuple(app.radio[0].options) == PAGE_NAMES
    assert app.button[0].label == "立即刷新看板数据"
    for page_name in PAGE_NAMES:
        app.radio[0].set_value(page_name).run()
        assert not app.exception
        assert app.title[0].value == page_name


def test_dashboard_rejects_invalid_local_port_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_DASHBOARD_PORT", "not-a-port")

    with pytest.raises(SettingsError, match="看板端口"):
        DashboardSettings.from_env(tmp_path)

    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    assert not app.exception
    assert app.title[0].value == "本地配置不可用"


def test_dashboard_rejects_unsupported_database_dialect_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        "postgresql+psycopg://localhost/takealot",
    )

    with pytest.raises(SettingsError, match="mysql\\+pymysql"):
        DashboardSettings.from_env(PROJECT_ROOT)

    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    assert not app.exception
    assert app.title[0].value == "本地配置不可用"
    assert "mysql+pymysql" in app.error[0].value


def test_populated_dashboard_renders_every_data_page_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    metric_date = date.today() - timedelta(days=1)
    database = tmp_path / "populated.db"
    _seed_dashboard(database, metric_date)
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("TAKEALOT_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.delenv("TAKEALOT_DASHBOARD_PORT", raising=False)

    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()

    assert not app.exception
    assert app.metric[0].label == "最新可用日下单件数"
    assert app.metric[3].label == "最新指标日异常商品数"
    assert metric_date.isoformat() in app.caption[0].value
    for page_name in PAGE_NAMES:
        navigation = next(
            radio for radio in app.radio if tuple(radio.options) == PAGE_NAMES
        )
        navigation.set_value(page_name).run()
        assert not app.exception
        assert app.title[0].value == page_name
        if page_name == "单品分析":
            assert len(app.get("plotly_chart")) == 2
            assert app.metric[4].label == "最新可用日下单件数"
        elif page_name == "经营四象限":
            assert len(app.get("plotly_chart")) == 1
        elif page_name == "异常商品":
            scope_radios = [
                radio
                for radio in app.radio
                if tuple(radio.options) == ("最新指标日", "全部历史")
            ]
            assert len(scope_radios) == 1
            assert scope_radios[0].value == "最新指标日"
            assert app.metric[0].label == "最新指标日异常商品数"
            assert app.metric[0].value == "1"
        elif page_name == "数据质量":
            assert app.metric[0].value == "1"
        elif page_name == "导出中心":
            assert any(button.label == "一键导出全部报表" for button in app.button)


def _seed_dashboard(database: Path, metric_date: date) -> None:
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    create_schema(engine)
    now = datetime.combine(metric_date, datetime.min.time(), tzinfo=UTC)
    products = (
        ("offer-a", "SKU-A", 900, 8),
        ("offer-b", "SKU-B", 500, 2),
        ("offer-c", "SKU-C", 200, 7),
        ("offer-d", "SKU-D", None, 1),
    )
    with Session(engine) as session, session.begin():
        session.add(
            CollectionRun(
                run_id="dashboard-smoke-offers",
                run_type="offers",
                scope_date=metric_date,
                started_at=now,
                finished_at=now,
                status="success",
                counts={"offers": len(products)},
                error=None,
            )
        )
        for index, (offer_id, sku, views, units) in enumerate(products):
            session.add(
                OfferSnapshot(
                    snapshot_date=metric_date,
                    offer_id=offer_id,
                    tsin_id=f"TSIN-{index}",
                    sku=sku,
                    barcode=f"600{index}",
                    title=f"Smoke Product {index}",
                    selling_price=Decimal("199.99"),
                    rrp=Decimal("229.99"),
                    benchmark_price=Decimal("205.00"),
                    status="buyable",
                    image_url=None,
                    productline_id=None,
                    conversion_percentage_30_days=Decimal("3.5"),
                    conversion_percentage_previous_30_days=Decimal("3.0"),
                    page_views_30_days=views,
                    quantity_returned_30_days=0,
                    total_wishlist=5,
                    wishlist_30_days=1,
                    listing_quality="good",
                    discount_percentage=Decimal("10"),
                    updated_at=now,
                    captured_at=now,
                    total_stock=5,
                )
            )
            session.add(
                DailyProductMetric(
                    metric_date=metric_date,
                    offer_id=offer_id,
                    sku=sku,
                    ordered_units=units,
                    effective_units=0,
                    ordered_revenue=Decimal(str(units * 199.99)),
                    page_views_30_days=views,
                    page_views_30_day_average=(
                        None if views is None else Decimal(str(views / 30))
                    ),
                    page_views_window_net_change=None,
                    conversion_percentage_30_days=Decimal("3.5"),
                    conversion_percentage_previous_30_days=Decimal("3.0"),
                    conversion_change_points=Decimal("0.5"),
                    total_stock=5,
                    offer_status="buyable",
                )
            )
        session.add(
            AnomalyEvent(
                event_date=metric_date,
                offer_id="offer-b",
                anomaly_type="high_views_low_conversion",
                severity="warning",
                explanation="Smoke anomaly",
                details={},
                created_at=now,
            )
        )
        session.add(
            DataQualityEvent(
                event_id="dashboard-smoke-quality",
                event_date=metric_date,
                event_type="unknown_sale_status",
                severity="warning",
                offer_id="offer-b",
                details={"sale_statuses": ["unknown"]},
                created_at=now,
            )
        )
    engine.dispose()
