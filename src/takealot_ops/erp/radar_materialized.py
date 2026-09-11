"""Bounded, incremental local radar projections. Business MySQL remains read-only.

Only a compact search index is scanned for a page. Card bodies live compressed on
disk and are decoded for the selected page only. A single worker prepares changes
in small batches; readers see the previous complete transaction during refresh.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store import load_connected_store_offers
from takealot_ops.storage.models import CompetitorSnapshot, CompetitorTarget, CompetitorVariantSnapshot, StoreOfferBaseline, StoreOfferObservation
from takealot_ops.storage.store_context import store_scope
from takealot_ops.competitors.service import _competitor_display_date, load_true_competitor_date_range
from takealot_ops.erp.radar_list_query import RadarListQuery, list_index, matches_index, seller_groups

logger = logging.getLogger(__name__)


def radar_date_bounds(engine: Engine, *, own: bool, store_codes: set[str]) -> tuple[date | None, date | None]:
    """Resolve one shared interval before partitioning a query into PLID batches."""
    if not own:
        bounds = load_true_competitor_date_range(engine)
        return tuple(date.fromisoformat(bounds[k]) if bounds[k] else None for k in ("available_start", "available_end"))  # type: ignore[return-value, arg-type]
    dates: list[date] = []
    with Session(engine) as session:
        offers = load_connected_store_offers(session)
        plids = {o.offer.productline_id for o in offers if o.store_code in store_codes and o.offer.productline_id}
        first, last = session.execute(select(func.min(CompetitorSnapshot.collected_at), func.max(CompetitorSnapshot.collected_at))
                                      .where(CompetitorSnapshot.plid.in_(plids))).one()
        dates.extend(_competitor_display_date(v) for v in (first, last) if v is not None)
        for code in store_codes:
            with store_scope(code):
                for model in (StoreOfferBaseline, StoreOfferObservation):
                    first_day, last_day = session.execute(select(func.min(model.display_date), func.max(model.display_date))
                                                          .where(model.productline_id.in_(plids))).one()
                    dates.extend(v for v in (first_day, last_day) if v is not None)
    return (min(dates), max(dates)) if dates else (None, None)


def radar_fingerprints(engine: Engine, *, own: bool, store_codes: set[str], store_version: str) -> dict[str, str]:
    """Read small per-PLID change markers, never hydrate historical JSON rows."""
    with Session(engine) as session:
        offers = load_connected_store_offers(session)
        all_owned = {str(offer.offer.productline_id) for offer in offers if offer.offer.productline_id}
        targets = {row.plid: (row.url, row.title, str(row.updated_at)) for row in session.scalars(
            select(CompetitorTarget).where(CompetitorTarget.active.is_(True)))}
        plids = ({str(offer.offer.productline_id) for offer in offers if offer.store_code in store_codes and offer.offer.productline_id}
                 if own else targets.keys() - all_owned)
        markers: dict[str, list[Any]] = {p: [targets.get(p), store_version if own else ""] for p in plids}
        global_markers = []
        for model in (CompetitorSnapshot, CompetitorVariantSnapshot):
            for plid, last_id, count in session.execute(select(
                model.plid, func.max(model.id), func.count(model.id),
            ).group_by(model.plid)):
                global_markers.append((plid, last_id, count))
                if plid in markers:
                    markers[plid].append((last_id, count))
        markers["__global__"] = global_markers
    return {p: hashlib.sha256(json.dumps(v, default=str).encode()).hexdigest() for p, v in markers.items()}


@dataclass
class _Work:
    version: Callable[[], str]
    fingerprints: Callable[[], dict[str, str]]
    loader: Callable[[set[str]], dict[str, Any]]
    field: str
    touched: float
    future: Future[None] | None = None


class MaterializedRadar:
    def __init__(self, path: Path, *, namespace: str, max_scopes: int = 4, batch_size: int = 64,
                 max_pages: int = 65536) -> None:
        self.path, self.namespace = path, namespace
        self.max_scopes, self.batch_size = max_scopes, batch_size
        self.max_pages = max_pages
        self._lock = Lock()
        self._capacity = Condition(self._lock)
        self._readers: dict[str, int] = {}
        self._works: OrderedDict[str, _Work] = OrderedDict()
        self._executor: ThreadPoolExecutor | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(f"PRAGMA max_page_count={self.max_pages}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("CREATE TABLE IF NOT EXISTS scopes (key TEXT PRIMARY KEY, version TEXT, generated REAL, metadata TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS cards (scope TEXT, plid TEXT, fingerprint TEXT, search TEXT, body BLOB, date_range TEXT, PRIMARY KEY(scope,plid))")
        self._initialized = True

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA cache_size=-2048")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _metadata(self, key: str) -> tuple[str, float, dict[str, Any]] | None:
        with self._db() as db:
            row = db.execute("SELECT version,generated,metadata FROM scopes WHERE key=?", (key,)).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def _submit(self, key: str, work: _Work) -> Future[None]:
        if work.future is None or work.future.done():
            assert self._executor is not None
            # A new request may replace callbacks while this generation is running.
            # Keep its date/context/loader together for the entire transaction.
            work.future = self._executor.submit(copy_context().run, self._refresh, key, replace(work))
        return work.future

    def _poll(self) -> None:
        while not self._stop.wait(10):
            with self._lock:
                for key, work in list(self._works.items()):
                    if time.time() - work.touched > 300:
                        continue
                    self._submit(key, work)

    def _refresh(self, key: str, work: _Work) -> None:
        version = work.version()
        old = self._metadata(key)
        if old and old[0] == version and time.time() - old[1] < 180:
            return
        markers = work.fingerprints()
        global_marker = markers.pop("__global__", "")
        with self._db() as db:
            previous = dict(db.execute("SELECT plid,fingerprint FROM cards WHERE scope=?", (key,)))
        changed = [p for p, v in markers.items() if previous.get(p) != v]
        # Reconcile edits of existing historical rows as well as append/delete.
        # An unchanged marker with a changed revision cannot certify freshness.
        if old and old[0] != version and not changed and global_marker == old[2].get("_global_marker", ""):
            changed = list(markers)
        with self._db() as db:
            for offset in range(0, len(changed), self.batch_size):
                if self._stop.is_set():
                    raise RuntimeError("Radar preparation stopped")
                batch = set(changed[offset:offset + self.batch_size])
                result = jsonable_encoder(work.loader(batch))
                by_plid = {item["plid"]: item for item in result[work.field]}
                for plid in batch:
                    item = by_plid.get(plid)
                    # Missing interval data is distinct from a fabricated zero row.
                    db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?)", (
                        key, plid, markers[plid], json.dumps(list_index(item), ensure_ascii=False) if item else None,
                        zlib.compress(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(), 3) if item else None,
                        json.dumps(result["date_range"]),
                    ))
                # Release batch DataFrames/cards before the next historical read.
                del result, by_plid
            db.executemany("DELETE FROM cards WHERE scope=? AND plid=?", [(key, p) for p in previous.keys() - markers.keys()])
            ranges = [json.loads(row[0]) for row in db.execute("SELECT DISTINCT date_range FROM cards WHERE scope=?", (key,))]
            dates = {}
            for name in ("available_start", "available_end", "selected_start", "selected_end"):
                values = [r[name] for r in ranges if r.get(name)]
                dates[name] = (min(values) if name.endswith("start") else max(values)) if values else None
            db.execute("INSERT OR REPLACE INTO scopes VALUES (?,?,?,?)", (key, version, time.time(), json.dumps({"date_range": dates, "_global_marker": global_marker})))
            old_keys = [r[0] for r in db.execute("SELECT key FROM scopes ORDER BY generated DESC LIMIT -1 OFFSET ?", (self.max_scopes,))]
            with self._lock:
                for old_key in old_keys:
                    if self._readers.get(old_key):
                        continue
                    db.execute("DELETE FROM cards WHERE scope=?", (old_key,))
                    db.execute("DELETE FROM scopes WHERE key=?", (old_key,))

    def page(self, *, key: Any, boundary: str, version: Callable[[], str],
             fingerprints: Callable[[], dict[str, str]], loader: Callable[[set[str]], dict[str, Any]],
             field: str, query: RadarListQuery, prefer_cached: bool, watchlist: set[str],
             prepare_only: bool = False) -> tuple[dict[str, Any], bool, float] | None:
        digest = hashlib.sha256(repr((self.namespace, key, boundary)).encode()).hexdigest()
        with self._lock:
            self._initialize()
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radar-prepare")
                self._thread = Thread(target=self._poll, daemon=True, name="radar-prepare-clock")
                self._thread.start()
            work = self._works.get(digest)
            if work is None:
                while work is None and len(self._works) >= self.max_scopes:
                    idle = next((k for k, w in self._works.items()
                                 if not self._readers.get(k) and (w.future is None or w.future.done())), None)
                    if idle is None:
                        # Warmers never occupy an HTTP thread waiting for another
                        # scope. Foreground reads wait outside the shared lock.
                        if prepare_only:
                            return None
                        self._capacity.wait(timeout=0.1)
                        if self._stop.is_set():
                            raise RuntimeError("Radar preparation stopped")
                        work = self._works.get(digest)
                        continue
                    self._works.pop(idle)
            if work is None:
                context = copy_context()
                # Polling retains the request's store context without retaining Request/cookies.
                work = _Work(lambda: context.copy().run(version),
                             lambda: context.copy().run(fingerprints),
                             lambda p: context.copy().run(loader, p), field, time.time())
                self._works[digest] = work
            work.touched = time.time()
            self._works.move_to_end(digest)
            context = copy_context()
            work.version = lambda: context.copy().run(version)
            work.fingerprints = lambda: context.copy().run(fingerprints)
            work.loader = lambda p: context.copy().run(loader, p)
            metadata = self._metadata(digest)
            fresh = metadata and metadata[0] == version() and time.time() - metadata[1] < 180
            future = None if fresh else self._submit(digest, work)
            if prepare_only:
                return None
            self._readers[digest] = self._readers.get(digest, 0) + 1
        try:
            return self._read_page(digest, key, field, query, watchlist, metadata, future, prefer_cached)
        finally:
            with self._capacity:
                self._readers[digest] -= 1
                if not self._readers[digest]:
                    del self._readers[digest]
                self._capacity.notify_all()

    def _read_page(self, digest: str, key: Any, field: str, query: RadarListQuery,
                   watchlist: set[str], metadata: tuple[str, float, dict[str, Any]] | None,
                   future: Future[None] | None, prefer_cached: bool) -> tuple[dict[str, Any], bool, float]:
        refreshing = future is not None
        if future and (not metadata or not prefer_cached):
            future.result()
            metadata = self._metadata(digest)
            refreshing = False
        assert metadata is not None
        with self._db() as db:
            # One read transaction binds the index and selected bodies to a single generation.
            db.execute("BEGIN")
            current = db.execute("SELECT generated,metadata FROM scopes WHERE key=?", (digest,)).fetchone()
            indexes = [json.loads(row[0]) for row in db.execute("SELECT search FROM cards WHERE scope=? AND search IS NOT NULL", (digest,))]
            indexes.sort(key=lambda i: (i["collected"], i["snapshot"], i["plid"]), reverse=True)
            if field == "store_items":
                indexes.sort(key=lambda i: i["own_order"])
            filtered = [i for i in indexes if matches_index(i, query, watchlist)]
            sort_key = query.signal if query.sort == "signal" else query.sort
            if sort_key != "全部":
                filtered.sort(key=lambda i: (i["sort"].get(sort_key) is None,
                    (i["sort"].get(sort_key) or 0) * (1 if query.direction == "asc" else -1)))
            total = len(filtered)
            page = min(query.page or 1, max(1, (total + query.page_size - 1) // query.page_size))
            selected = filtered[(page - 1) * query.page_size:page * query.page_size]
            bodies = {p: json.loads(zlib.decompress(body)) for p, body in db.execute(
                f"SELECT plid,body FROM cards WHERE scope=? AND plid IN ({','.join('?' for _ in selected)})",
                (digest, *(i["plid"] for i in selected)),
            )} if selected else {}
        if not current:
            raise RuntimeError("雷达查询已过期，请重新读取")
        ratings = [i["rating"] for i in indexes if i["rating"] is not None]
        result = {"items": [], "store_items": [], "own_follower_events": [],
                  "date_range": json.loads(current[1])["date_range"], field: [bodies[i["plid"]] for i in selected],
                  "pagination": {"page": page, "page_size": query.page_size, "total": total, "source_total": len(indexes),
                    "exact_stock_count": sum(i["exact"] for i in indexes),
                    "average_rating": sum(ratings) / len(ratings) if ratings else None,
                    "latest_collection": max((i["collected"] for i in indexes), default=None),
                    "seller_groups": seller_groups(indexes) if field == "items" else []}}
        if isinstance(key, tuple) and len(key) > 3:
            for name, value in zip(("selected_start", "selected_end"), key[2:4]):
                if value is not None:
                    result["date_range"][name] = value
        return result, refreshing, current[0]

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
