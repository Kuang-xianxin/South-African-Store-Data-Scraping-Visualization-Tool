from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.service import (
    build_quadrant_payload, build_summary_payload, load_erp_dataset,
    load_quadrant_dataset, load_summary_dataset,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CollectionRun, DailyProductMetric, OfferSnapshot
from takealot_ops.storage.store_context import store_scope


def test_narrow_projections_preserve_history_gaps_stale_offers_and_store_isolation(tmp_path):
    url = f"sqlite:///{tmp_path / 'projections.db'}"
    engine = create_engine(url)
    create_schema(engine)
    start = date(2026, 5, 1)
    cutoff = start + timedelta(days=70)
    with Session(engine) as session:
        for code in ("one", "two"):
            with store_scope(code):
                for day in range(76):
                    scope = start + timedelta(days=day)
                    session.add(CollectionRun(
                        store_code=code, run_id=f"{code}-{day}", run_type="offers", status="success",
                        scope_date=scope, started_at=datetime.combine(scope, datetime.min.time(), UTC),
                    ))
                    for offer in ("a", "b", "old"):
                        if offer == "old" and day > 10:
                            continue
                        if day in (17, 35, 67):
                            continue
                        units = None if day % 13 == 0 else day % 7
                        session.add(DailyProductMetric(
                            store_code=code, metric_date=scope, offer_id=offer, sku=f"{code}-{offer}",
                            ordered_units=units, effective_units=units,
                            ordered_revenue=None if units is None else units * 15.25,
                            page_views_30_days=100 + day, total_stock=day % 8,
                            offer_status="buyable",
                        ))
                        # Includes unknown stock between exact observations and an
                        # old replenishment whose evidence predates the chart window.
                        stock = None if day % 11 == 0 else (30 if day >= 20 else 5)
                        if offer == "b":
                            stock = None if day % 11 == 0 else day % 8
                        session.add(OfferSnapshot(
                            store_code=code, offer_id=offer, snapshot_date=scope,
                            sku=f"{code}-{offer}", title=f"{code} {offer}", productline_id=offer,
                            captured_at=datetime.combine(scope, datetime.min.time(), UTC),
                            total_stock=stock, selling_price=15.25,
                        ))
        session.commit()
    settings = DashboardSettings(project_root=Path.cwd(), database_url=url,
                                 dashboard_host="127.0.0.1", dashboard_port=8501)
    for code in ("one", "two"):
        with store_scope(code):
            for as_of in (start - timedelta(days=1), cutoff, cutoff + timedelta(days=30)):
                canonical = load_erp_dataset(settings, as_of, engine=engine)
                compact = load_quadrant_dataset(settings, as_of, engine=engine)
                assert build_quadrant_payload(compact, as_of, 50) == build_quadrant_payload(canonical, as_of, 50)
                summary = load_summary_dataset(settings, as_of, engine=engine)
                for range_start in (None, cutoff - timedelta(days=4)):
                    assert build_summary_payload(summary, as_of, start_date=range_start) == build_summary_payload(
                        canonical, as_of, start_date=range_start,
                    )
                if not compact.product_daily.empty:
                    assert len(compact.product_daily) < len(canonical.product_daily)
                    assert len(compact.offer_history) <= 6
                    assert set(compact.product_daily.sku) <= {f"{code}-{offer}" for offer in ("a", "b", "old")}
    engine.dispose()
