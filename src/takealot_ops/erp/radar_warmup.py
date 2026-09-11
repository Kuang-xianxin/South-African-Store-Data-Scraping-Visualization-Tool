"""Restartable, bounded radar warming; persist query intent, never authentication."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event, Lock, Thread

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from takealot_ops.erp.auth import StoreIdentity, UserIdentity, _identity
from takealot_ops.competitors.service import load_true_competitor_date_range
from takealot_ops.erp.permissions import COMPETITORS_VIEW
from takealot_ops.storage.models import ErpUser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RadarWarmRequest:
    user_id: int
    store_code: str
    own: bool
    scope: str = "all"
    start: str | None = None
    end: str | None = None


def resolve_warm_request(engine: Engine, request: RadarWarmRequest) -> tuple[
    UserIdentity, StoreIdentity
] | None:
    with Session(engine) as session:
        record = session.get(ErpUser, request.user_id)
        if record is None or not record.active:
            return None
        user = _identity(session, record)
        if not user.can(COMPETITORS_VIEW):
            return None
        store = next((s for s in user.accessible_stores if s.code == request.store_code
                      and s.active and s.data_connected), None)
        return (user, store) if store else None


def initial_warm_requests(engine: Engine) -> list[RadarWarmRequest]:
    requests: list[RadarWarmRequest] = []
    seen: set[tuple[bool, tuple[str, ...]]] = set()
    with Session(engine) as session:
        for record in session.scalars(select(ErpUser).where(ErpUser.active.is_(True))
                                      .order_by(ErpUser.last_login_at.desc(), ErpUser.id)):
            user = _identity(session, record)
            stores = [s for s in user.accessible_stores if s.active and s.data_connected]
            if not user.can(COMPETITORS_VIEW) or not stores:
                continue
            candidates: list[tuple[bool, str, StoreIdentity, tuple[str, ...]]] = [(False, "all", stores[0], ())]
            candidates.append((True, "all", stores[0], tuple(sorted(s.code for s in stores))))
            candidates.append((True, "operating", stores[0], tuple(sorted(
                s.code for s in stores if s.id in user.assigned_store_ids))))
            candidates.extend((True, "current", store, (store.code,)) for store in stores)
            for own, scope, store, codes in candidates:
                if (own, codes) not in seen:
                    requests.append(RadarWarmRequest(user.id, store.code, own, scope))
                    seen.add((own, codes))
    # The current UI reads the public date-range endpoint before opening either
    # partition. Prepare that exact explicit interval as well as rolling defaults.
    dates = load_true_competitor_date_range(engine)
    start, end = dates.get("selected_start"), dates.get("selected_end")
    return requests + ([replace(r, start=start, end=end) for r in requests] if start or end else [])


class RadarWarmup:
    """One coordinator revalidates each saved user before invoking read handlers."""

    def __init__(self, path: Path, *, max_requests: int = 64, interval: float = 60) -> None:
        self.path = path
        self.max_requests = max_requests
        self.interval = interval
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS requests (body TEXT PRIMARY KEY, touched REAL)")
        return db

    def remember(self, request: RadarWarmRequest) -> None:
        # No cookies, session tokens, passwords or serialized permissions on disk.
        with self._lock:
            db = self._connect()
            try:
                with db:
                    # Keep the latest date intent per user/store/partition. A
                    # yesterday tab must not consume a second warming slot.
                    for body, in db.execute("SELECT body FROM requests").fetchall():
                        old = RadarWarmRequest(**json.loads(body))
                        if self._scope(old) == self._scope(request):
                            db.execute("DELETE FROM requests WHERE body=?", (body,))
                    db.execute("INSERT OR REPLACE INTO requests VALUES (?, ?)",
                               (json.dumps(asdict(request), sort_keys=True), time.time_ns()))
                    db.execute("DELETE FROM requests WHERE body IN (SELECT body FROM requests "
                               "ORDER BY touched DESC, body LIMIT -1 OFFSET ?)",
                               (self.max_requests,))
            finally:
                db.close()

    def requests(self) -> list[RadarWarmRequest]:
        with self._lock:
            db = self._connect()
            try:
                saved: dict[tuple[int, str, bool, str], RadarWarmRequest] = {}
                for body, in db.execute("SELECT body FROM requests ORDER BY touched DESC, body"):
                    request = RadarWarmRequest(**json.loads(body))
                    saved.setdefault(self._scope(request), request)
                # Apply the same bound to restored legacy registrations.
                return list(saved.values())[:self.max_requests]
            finally:
                db.close()

    @staticmethod
    def _scope(request: RadarWarmRequest) -> tuple[int, str, bool, str]:
        return request.user_id, request.store_code, request.own, request.scope

    def run_once(self, dispatch: Callable[[RadarWarmRequest], object]) -> None:
        saved = self.requests()
        # An old open tab keeps requesting yesterday explicitly. Also prepare
        # the rolling default, without changing that tab's chosen historical dates.
        candidates = [replace(r, start=None, end=None) for r in saved] + saved
        seen: set[RadarWarmRequest] = set()
        counts = {False: 0, True: 0}
        for request in candidates:
            if request in seen or counts[request.own] >= max(1, self.max_requests // 2):
                continue
            seen.add(request)
            counts[request.own] += 1
            if self._stop.is_set():
                break
            try:
                dispatch(request)
            except Exception:
                # One failed scope must not prevent the other partition warming.
                logger.exception("Radar warmup failed for user=%s own=%s",
                                 request.user_id, request.own)

    def start(self, dispatch: Callable[[RadarWarmRequest], object],
              seed: Callable[[], None]) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            while not self._stop.is_set():
                try:
                    # Discover new authorized users/scopes before their first visit.
                    seed()
                    self.run_once(dispatch)
                except Exception:
                    logger.exception("Radar warmup coordinator failed")
                if self._stop.wait(self.interval):
                    break

        self._thread = Thread(target=run, name="radar-warmup", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
