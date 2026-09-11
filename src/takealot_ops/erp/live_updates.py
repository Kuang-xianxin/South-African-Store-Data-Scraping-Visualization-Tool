"""Tiny shared revision reads and version-aware read projection keys."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Hashable, Iterable
from threading import Lock
from typing import TypeVar

from sqlalchemy import Engine, select

from takealot_ops.erp.read_cache import ReadProjectionCache
from takealot_ops.storage.models import ErpDataRevision, ErpStore, OfferCurrent


MODULE_TOPICS: dict[str, frozenset[str]] = {
    "overview": frozenset(("store", "home", "returns", "master", "users")),
    "keyword-traffic": frozenset(("store", "search", "master", "users")),
    "search-ranking": frozenset(("store", "search", "master", "users")),
    "quadrants": frozenset(("store", "master", "users")),
    "anomaly-products": frozenset(("store", "returns", "competitors", "master", "users")),
    "returns": frozenset(("store", "returns", "logistics", "master", "users")),
    "logistics": frozenset(("store", "logistics", "master", "users")),
    "container-selection": frozenset(("store", "returns", "logistics", "competitors", "master", "users")),
    "competitors": frozenset(("store", "returns", "competitors", "own-identities", "watchlists", "master", "users")),
    "users": frozenset(("users",)),
}
_CACHE_MODULES = {
    "home-dashboard": "overview",
    "store-summary": "overview", "store-summaries": "overview", "sales-revisions": "overview",
    "products": "overview", "product-detail": "overview", "seller-returns": "returns",
    "keyword-traffic-list": "keyword-traffic", "keyword-traffic-detail": "keyword-traffic",
    "search-ranking-list": "search-ranking", "anomaly-products": "anomaly-products",
    "logistics-overview": "logistics",
    "quadrants": "quadrants", "container-selection": "container-selection",
    "competitors-list": "competitors", "competitors-own-store": "competitors",
    "competitor-personal-overview": "competitors", "competitor-store-targets": "competitors",
}


class DataRevisionReader:
    """One small SELECT per process per two seconds, shared by all visitors."""

    def __init__(self, engine: Engine, *, ttl_seconds: float = 2.0) -> None:
        self._engine = engine
        self._ttl = ttl_seconds
        self._lock = Lock()
        self._loaded_at = float("-inf")
        self._rows: tuple[tuple[str, str, str], ...] = ()
        self._ownership_lock = Lock()
        self._ownership_loaded_at = float("-inf")
        self._ownership_token = ""
        self._catalog_token = ""
        self._ownership_revision = ""

    def rows(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            if time.monotonic() - self._loaded_at >= self._ttl:
                with self._engine.connect() as connection:
                    rows = connection.execute(select(
                        ErpDataRevision.scope, ErpDataRevision.topic, ErpDataRevision.revision,
                    )).tuples().all()
                self._rows = tuple(sorted((row[0], row[1], row[2]) for row in rows))
                self._loaded_at = time.monotonic()
            return self._rows

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = float("-inf")
        with self._ownership_lock:
            self._ownership_loaded_at = float("-inf")

    def token(self, module: str, store_codes: Iterable[str]) -> str:
        return self._topic_token(MODULE_TOPICS[module], store_codes)

    def radar_access_token(self, store_codes: Iterable[str], *, permissions: str, own: bool = False) -> str:
        """Stock/price updates refresh data without discarding a safe preview."""
        # The write marker also changes on ordinary Offer price/stock updates.
        # Verify actual global ownership: true competitors exclude every store.
        revision = self._topic_token(frozenset(("own-identities", "users")), store_codes)
        with self._ownership_lock:
            if revision != self._ownership_revision or time.monotonic() - self._ownership_loaded_at >= self._ttl:
                offers = OfferCurrent.__table__
                stores = ErpStore.__table__
                with self._engine.connect() as connection:
                    identities = [tuple(row) for row in connection.execute(select(
                        offers.c.store_code, offers.c.offer_id, offers.c.productline_id,
                        offers.c.sku, offers.c.tsin_id,
                    ).order_by(offers.c.store_code, offers.c.offer_id))]
                    catalog = [tuple(row) for row in connection.execute(select(
                        stores.c.id, stores.c.code, stores.c.active, stores.c.data_connected,
                    ).order_by(stores.c.id))]
                self._ownership_token = hashlib.sha256(json.dumps(
                    (identities, catalog), separators=(",", ":"),
                ).encode()).hexdigest()[:24]
                self._catalog_token = hashlib.sha256(json.dumps(
                    catalog, separators=(",", ":"),
                ).encode()).hexdigest()[:24]
                self._ownership_loaded_at = time.monotonic()
                self._ownership_revision = revision
            # An own-store preview is a dated snapshot of already authorized
            # stores. Offer membership changes refresh its data, not its access.
            # Public competitors still require current global ownership masking.
            ownership = self._catalog_token if own else self._ownership_token
        return ownership + hashlib.sha256(permissions.encode()).hexdigest()[:24]

    def _topic_token(self, topics: frozenset[str], store_codes: Iterable[str]) -> str:
        scopes = {"*", *store_codes}
        rows = [row for row in self.rows() if row[0] in scopes and row[1] in topics]
        return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:24]

    def cache_token(self, key: Hashable) -> str:
        parts = key if isinstance(key, tuple) else (key,)
        prefix = str(parts[0]).rsplit("-v", 1)[0]
        module = _CACHE_MODULES.get(prefix)
        component = prefix in {"radar-store-history", "radar-official-sales"}
        if module is None and not component:
            return ""
        scopes: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, str):
                scopes.add(value)
            elif isinstance(value, tuple):
                for part in value:
                    visit(part)

        for part in parts[1:]:
            visit(part)
        if component:
            return self._topic_token(frozenset(("store", "users")), scopes)
        if prefix == "competitors-list" and parts[-1] is False:
            return self._topic_token(frozenset(("competitors", "own-identities", "users")), ())
        if prefix == "competitors-own-store":
            return self._topic_token(
                frozenset(("store", "competitors", "own-identities", "master", "users")), scopes,
            )
        assert module is not None
        return self.token(module, scopes)


T = TypeVar("T")


class VersionedReadProjectionCache(ReadProjectionCache):
    def __init__(self, revisions: DataRevisionReader, *, ttl_seconds: float, max_entries: int) -> None:
        super().__init__(ttl_seconds=ttl_seconds, max_entries=max_entries)
        self._revisions = revisions

    def get_or_load(self, key: Hashable, loader: Callable[[], T]) -> T:
        return super().get_or_load((key, self._revisions.cache_token(key)), loader)

    def _entry_ttl(self, key: Hashable) -> float:
        # Unregistered projections have no change markers: keep the legacy bound.
        if not isinstance(key, tuple) or not key[-1]:
            return min(20.0, self._ttl_seconds)
        return self._ttl_seconds
