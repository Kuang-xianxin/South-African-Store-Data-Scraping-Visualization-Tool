from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.erp.keyword_traffic import (
    KeywordTrafficConflictError,
    build_keyword_product_detail,
    build_keyword_product_list,
    record_keyword_snapshot,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    OfferCurrent,
    OfferSnapshot,
    ProductKeywordSnapshot,
)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-1",
                sku="SKU-1",
                title="Tracked product",
                image_url="https://takealot.s3.amazonaws.com/covers_images/example/s.file",
                captured_at=datetime(2026, 8, 3, 1, tzinfo=UTC),
                page_views_30_days=210,
            )
        )
    return engine


def _add_history(engine, values: dict[date, int | None]) -> None:
    with Session(engine) as session, session.begin():
        for snapshot_date, page_views in values.items():
            session.add(
                OfferSnapshot(
                    snapshot_date=snapshot_date,
                    offer_id="offer-1",
                    sku="SKU-1",
                    title="Tracked product",
                    captured_at=datetime.combine(
                        snapshot_date,
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    page_views_30_days=page_views,
                )
            )


def test_keyword_change_comparison_exposes_level_and_trend_change() -> None:
    engine = _engine()
    change_day = date(2026, 7, 20)
    values = {
        change_day + timedelta(days=offset): value
        for offset, value in {
            -3: 100,
            -2: 110,
            -1: 120,
            0: 125,
            1: 140,
            2: 170,
            3: 210,
        }.items()
    }
    _add_history(engine, values)

    record_keyword_snapshot(
        engine,
        offer_id="offer-1",
        effective_date=date(2026, 7, 10),
        keywords=["memory foam", "mattress"],
        note="首次建立基线",
        actor_user_id=None,
        actor_username="operator",
        today=date(2026, 8, 3),
    )
    record_keyword_snapshot(
        engine,
        offer_id="offer-1",
        effective_date=change_day,
        keywords=["memory foam", "queen mattress"],
        note="替换核心词",
        actor_user_id=None,
        actor_username="operator",
        today=date(2026, 8, 3),
    )

    with Session(engine) as session:
        payload = build_keyword_product_detail(
            session,
            offer_id="offer-1",
            as_of=date(2026, 7, 23),
            history_days=30,
            comparison_days=3,
        )

    assert payload is not None
    change = payload["events"][1]
    assert change["event_kind"] == "change"
    assert change["added_keywords"] == ["queen mattress"]
    assert change["removed_keywords"] == ["mattress"]
    comparison = change["comparison"]
    assert comparison["traffic_direction"] == "up"
    assert comparison["traffic_delta"] == 90
    assert comparison["traffic_delta_percent"] == 75.0
    assert comparison["before"]["slope_per_day"] == 10.0
    assert comparison["after"]["slope_per_day"] == 35.0
    assert comparison["trend_change"] == "improving"
    assert comparison["status"] == "complete"
    engine.dispose()


def test_missing_traffic_stays_missing_and_breaks_comparison() -> None:
    engine = _engine()
    change_day = date(2026, 7, 20)
    _add_history(
        engine,
        {
            change_day - timedelta(days=1): 100,
            change_day: 105,
            change_day + timedelta(days=1): None,
        },
    )
    record_keyword_snapshot(
        engine,
        offer_id="offer-1",
        effective_date=change_day,
        keywords=["first keyword"],
        note=None,
        actor_user_id=None,
        actor_username="operator",
        today=date(2026, 8, 3),
    )

    with Session(engine) as session:
        payload = build_keyword_product_detail(
            session,
            offer_id="offer-1",
            as_of=change_day + timedelta(days=3),
            history_days=30,
            comparison_days=3,
        )

    assert payload is not None
    history = {row["date"]: row["page_views_30_days"] for row in payload["history"]}
    assert history[(change_day + timedelta(days=1)).isoformat()] is None
    assert history[(change_day + timedelta(days=2)).isoformat()] is None
    comparison = payload["events"][0]["comparison"]
    assert comparison["traffic_direction"] == "unavailable"
    assert comparison["traffic_delta"] is None
    assert comparison["trend_change"] == "insufficient"
    assert comparison["status"] == "data_missing"
    engine.dispose()


def test_product_list_uses_latest_snapshot_on_or_before_selected_day() -> None:
    engine = _engine()
    _add_history(
        engine,
        {
            date(2026, 8, 1): 180,
            date(2026, 8, 2): 200,
        },
    )
    record_keyword_snapshot(
        engine,
        offer_id="offer-1",
        effective_date=date(2026, 8, 1),
        keywords=["keyword"],
        note=None,
        actor_user_id=None,
        actor_username="operator",
        today=date(2026, 8, 3),
    )

    with Session(engine) as session:
        payload = build_keyword_product_list(session, as_of=date(2026, 8, 1))

    item = payload["items"][0]
    assert item["latest_page_views_30_days"] == 180
    assert item["latest_snapshot_date"] == "2026-08-01"
    assert item["keyword_event_count"] == 1
    assert item["keyword_change_count"] == 0
    engine.dispose()


def test_keyword_timeline_rejects_same_day_and_unchanged_snapshots() -> None:
    engine = _engine()
    record_keyword_snapshot(
        engine,
        offer_id="offer-1",
        effective_date=date(2026, 8, 1),
        keywords=["Memory   Foam", "memory foam", "Mattress"],
        note=None,
        actor_user_id=None,
        actor_username="operator",
        today=date(2026, 8, 3),
    )

    with pytest.raises(KeywordTrafficConflictError, match="当天已经记录"):
        record_keyword_snapshot(
            engine,
            offer_id="offer-1",
            effective_date=date(2026, 8, 1),
            keywords=["different"],
            note=None,
            actor_user_id=None,
            actor_username="operator",
            today=date(2026, 8, 3),
        )
    with pytest.raises(KeywordTrafficConflictError, match="上一次记录一致"):
        record_keyword_snapshot(
            engine,
            offer_id="offer-1",
            effective_date=date(2026, 8, 2),
            keywords=["memory foam", "mattress"],
            note=None,
            actor_user_id=None,
            actor_username="operator",
            today=date(2026, 8, 3),
        )

    with Session(engine) as session:
        rows = list(session.scalars(select(ProductKeywordSnapshot)))
    assert rows[0].keywords == ["Memory Foam", "Mattress"]
    engine.dispose()
