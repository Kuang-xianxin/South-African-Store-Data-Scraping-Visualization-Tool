from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from takealot_ops.erp.daily_report_live import (
    daily_report_change_token,
    daily_report_event_stream,
    format_server_event,
)
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import DailyReportRun


def test_change_token_tracks_new_daily_report_run(tmp_path: Path) -> None:
    engine = create_engine_for_database_url(
        f"sqlite:///{(tmp_path / 'daily-report-live.db').as_posix()}"
    )
    create_schema(engine)
    before = daily_report_change_token(engine)
    captured_at = datetime(2026, 7, 28, 10, 5)
    with Session(engine) as session:
        session.add(
            DailyReportRun(
                run_id="live-run-1",
                business_date=date(2026, 7, 27),
                slot="morning",
                captured_at=captured_at,
                status="success",
                counts={"observed": 1},
                created_at=captured_at,
            )
        )
        session.commit()
    assert daily_report_change_token(engine) != before
    engine.dispose()


def test_event_stream_sends_reconnect_safe_ready_event(tmp_path: Path) -> None:
    engine = create_engine_for_database_url(
        f"sqlite:///{(tmp_path / 'daily-report-events.db').as_posix()}"
    )
    create_schema(engine)

    async def disconnected() -> bool:
        return True

    async def first_event() -> str:
        stream = daily_report_event_stream(
            engine,
            is_disconnected=disconnected,
            business_date=lambda: date(2026, 7, 27),
            poll_seconds=0,
        )
        return await anext(stream)

    event = asyncio.run(first_event())
    assert event.startswith("event: ready\n")
    assert '"business_date":"2026-07-27"' in event
    engine.dispose()


def test_server_event_keeps_payload_on_one_data_line() -> None:
    event = format_server_event("daily-report-updated", {"note": "a\nb"})
    assert event.count("data: ") == 1
    assert "a\\nb" in event
