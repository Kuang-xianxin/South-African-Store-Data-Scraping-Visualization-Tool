from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.keyword_traffic import (
    build_keyword_product_detail,
    build_keyword_product_list,
    extract_title_keywords,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import OfferCurrent, OfferSnapshot


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-1",
                sku="SKU-1",
                title="Memory Foam Queen Mattress",
                image_url="https://takealot.s3.amazonaws.com/covers_images/example/s.file",
                captured_at=datetime(2026, 8, 3, 1, tzinfo=UTC),
                page_views_30_days=210,
            )
        )
    return engine


def _add_history(
    engine,
    values: dict[date, tuple[int | None, str | None]],
) -> None:
    with Session(engine) as session, session.begin():
        for snapshot_date, (page_views, title) in values.items():
            session.add(
                OfferSnapshot(
                    snapshot_date=snapshot_date,
                    offer_id="offer-1",
                    sku="SKU-1",
                    title=title,
                    captured_at=datetime.combine(
                        snapshot_date,
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    page_views_30_days=page_views,
                )
            )


def test_title_change_is_automatically_labeled_and_compared() -> None:
    engine = _engine()
    change_day = date(2026, 7, 20)
    values = {
        change_day + timedelta(days=offset): (
            page_views,
            "Memory Foam Mattress" if offset < 0 else "Memory Foam Queen Mattress",
        )
        for offset, page_views in {
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

    with Session(engine) as session:
        payload = build_keyword_product_detail(
            session,
            offer_id="offer-1",
            as_of=date(2026, 7, 23),
            history_days=30,
            comparison_days=3,
        )

    assert payload is not None
    assert len(payload["events"]) == 2
    baseline, change = payload["events"]
    assert baseline["event_kind"] == "baseline"
    assert baseline["change_label"] == "基线｜首次完整标题快照"
    assert change["event_kind"] == "change"
    assert change["event_source"] == "offer_title"
    assert change["change_label"] == "变化｜新增 1 词"
    assert change["added_keywords"] == ["Queen"]
    assert change["removed_keywords"] == []
    assert change["source_title"] == "Memory Foam Queen Mattress"
    comparison = change["comparison"]
    assert comparison["traffic_direction"] == "up"
    assert comparison["traffic_delta"] == 90
    assert comparison["traffic_delta_percent"] == 75.0
    assert comparison["before"]["slope_per_day"] == 10.0
    assert comparison["after"]["slope_per_day"] == 35.0
    assert comparison["trend_change"] == "improving"
    assert comparison["status"] == "complete"
    engine.dispose()


def test_missing_traffic_stays_missing_after_automatic_change() -> None:
    engine = _engine()
    change_day = date(2026, 7, 20)
    _add_history(
        engine,
        {
            change_day - timedelta(days=1): (100, "Memory Foam Mattress"),
            change_day: (105, "Memory Foam Queen Mattress"),
            change_day + timedelta(days=1): (None, "Memory Foam Queen Mattress"),
        },
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
    comparison = payload["events"][1]["comparison"]
    assert comparison["traffic_direction"] == "unavailable"
    assert comparison["traffic_delta"] is None
    assert comparison["trend_change"] == "insufficient"
    assert comparison["status"] == "data_missing"
    engine.dispose()


def test_product_list_automatically_archives_latest_title_snapshot() -> None:
    engine = _engine()
    _add_history(
        engine,
        {
            date(2026, 8, 1): (180, "Memory Foam Mattress"),
            date(2026, 8, 2): (200, "Memory Foam Queen Mattress"),
        },
    )

    with Session(engine) as session:
        payload = build_keyword_product_list(session, as_of=date(2026, 8, 1))

    item = payload["items"][0]
    assert item["latest_page_views_30_days"] == 180
    assert item["latest_snapshot_date"] == "2026-08-01"
    assert item["keyword_event_count"] == 1
    assert item["keyword_change_count"] == 0
    assert item["current_keywords"] == ["Memory", "Foam", "Mattress"]
    assert payload["summary"]["archived_product_count"] == 1
    engine.dispose()


def test_title_term_order_change_gets_a_change_label() -> None:
    engine = _engine()
    _add_history(
        engine,
        {
            date(2026, 8, 1): (180, "Memory Foam Mattress"),
            date(2026, 8, 2): (200, "Mattress Memory Foam"),
        },
    )

    with Session(engine) as session:
        payload = build_keyword_product_detail(
            session,
            offer_id="offer-1",
            as_of=date(2026, 8, 2),
            history_days=30,
            comparison_days=3,
        )

    assert payload is not None
    change = payload["events"][1]
    assert change["added_keywords"] == []
    assert change["removed_keywords"] == []
    assert change["change_label"] == "变化｜标题词顺序或写法变化"
    assert extract_title_keywords("Memory foam, memory foam") == ["Memory", "foam"]
    engine.dispose()
