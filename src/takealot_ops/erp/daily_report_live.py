"""Server-sent events for live daily-report updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    DailyReportAudit,
    DailyReportDeadlineSnapshot,
    DailyReportResolution,
    DailyReportRun,
)


def daily_report_change_token(engine: Engine) -> str:
    """Return a stable fingerprint for every persisted daily-report mutation."""
    with Session(engine) as session:
        run_state = session.execute(
            select(
                func.count(DailyReportRun.run_id),
                func.max(DailyReportRun.created_at),
            )
        ).one()
        audit_state = session.execute(
            select(
                func.count(DailyReportAudit.id),
                func.max(DailyReportAudit.id),
            )
        ).one()
        resolution_state = session.execute(
            select(
                func.count(DailyReportResolution.id),
                func.max(DailyReportResolution.updated_at),
            )
        ).one()
        deadline_state = session.execute(
            select(
                func.count(DailyReportDeadlineSnapshot.business_date),
                func.max(DailyReportDeadlineSnapshot.snapped_at),
                func.max(DailyReportDeadlineSnapshot.resolved_at),
            )
        ).one()
    values = (*run_state, *audit_state, *resolution_state, *deadline_state)
    return json.dumps(
        [value.isoformat() if hasattr(value, "isoformat") else value for value in values],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def format_server_event(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE event without allowing payload newlines to break framing."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def daily_report_event_stream(
    engine: Engine,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
    business_date: Callable[[], date],
    poll_seconds: float = 2.0,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Push changes and reconnect-safe initial state to one authenticated client."""
    token = await run_in_threadpool(daily_report_change_token, engine)
    yield format_server_event(
        "ready",
        {"token": token, "business_date": business_date().isoformat()},
    )
    elapsed_since_heartbeat = 0.0
    while not await is_disconnected():
        await asyncio.sleep(poll_seconds)
        elapsed_since_heartbeat += poll_seconds
        current = await run_in_threadpool(daily_report_change_token, engine)
        if current != token:
            token = current
            elapsed_since_heartbeat = 0.0
            yield format_server_event(
                "daily-report-updated",
                {"token": token, "business_date": business_date().isoformat()},
            )
        elif elapsed_since_heartbeat >= heartbeat_seconds:
            elapsed_since_heartbeat = 0.0
            yield ": keep-alive\n\n"
