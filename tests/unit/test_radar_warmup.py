from dataclasses import fields
from dataclasses import asdict
import json
import sqlite3
from threading import Event

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.radar_warmup import (
    RadarWarmRequest, RadarWarmup, resolve_warm_request,
)
from takealot_ops.storage.models import Base, ErpUser


def test_restart_keeps_only_bounded_query_intent_and_failure_does_not_block(tmp_path):
    path = tmp_path / "warm.sqlite3"
    warmer = RadarWarmup(path, max_requests=2)
    a, b, c = [RadarWarmRequest(i, "current", bool(i % 2)) for i in range(3)]
    warmer.remember(a)
    warmer.remember(b)
    warmer.remember(c)
    saved = RadarWarmup(path, max_requests=2).requests()
    assert len(saved) == 2 and c in saved
    assert {f.name for f in fields(RadarWarmRequest)} == {
        "user_id", "store_code", "own", "scope", "start", "end"}
    called = []
    def dispatch(request):
        called.append(request)
        raise RuntimeError("one unavailable partition")
    warmer.run_once(dispatch)
    assert called == saved


def test_start_replays_without_visitors_and_close_stops_coordinator(tmp_path):
    warmer = RadarWarmup(tmp_path / "warm.sqlite3", interval=0.02)
    saved = RadarWarmRequest(7, "current", True)
    ready = Event()
    calls = []
    def dispatch(request):
        calls.append(request)
        if len(calls) >= 2:
            ready.set()
    warmer.start(dispatch, lambda: warmer.remember(saved))
    try:
        assert ready.wait(2)
        assert calls[:2] == [saved, saved]
    finally:
        warmer.close()
    assert not warmer._thread.is_alive()


def test_revoked_or_deleted_user_is_not_replayed(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'users.sqlite3'}")
    Base.metadata.create_all(engine, tables=[ErpUser.__table__])
    from datetime import datetime
    with Session(engine) as session:
        session.add(ErpUser(id=1, username="disabled", display_name="Disabled", role="admin",
                            password_hash="irrelevant", active=False, store_access_all=True,
                            created_at=datetime.now(), updated_at=datetime.now()))
        session.commit()
    assert resolve_warm_request(engine, RadarWarmRequest(1, "current", True)) is None
    assert resolve_warm_request(engine, RadarWarmRequest(2, "current", True)) is None
    engine.dispose()


def test_new_date_replaces_old_intent_and_legacy_restart_deduplicates(tmp_path):
    path = tmp_path / "warm.sqlite3"
    warmer = RadarWarmup(path)
    yesterday = RadarWarmRequest(1, "current", False, end="2026-09-10")
    today = RadarWarmRequest(1, "current", False)
    own = RadarWarmRequest(1, "current", True)
    warmer.remember(yesterday)
    warmer.remember(own)
    warmer.remember(today)
    assert set(warmer.requests()) == {today, own}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO requests VALUES (?,?)", (json.dumps(asdict(yesterday)), 1))
    restored = RadarWarmup(path).requests()
    assert restored == [today, own]


def test_old_open_tab_still_warms_rolling_default_and_keeps_explicit_history(tmp_path):
    warmer = RadarWarmup(tmp_path / "warm.sqlite3")
    old = RadarWarmRequest(1, "current", False, end="2026-09-10")
    own = RadarWarmRequest(1, "current", True, end="2026-09-10")
    warmer.remember(old)
    warmer.remember(own)
    called = []
    warmer.run_once(called.append)
    assert called == [RadarWarmRequest(1, "current", True), RadarWarmRequest(1, "current", False), own, old]
    assert warmer.requests() == [own, old]
