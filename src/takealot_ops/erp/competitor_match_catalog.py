"""Small identity directory for matching; never load sales or historical offers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from typing import Any

from sqlalchemy import Engine, func, select

from takealot_ops.erp.radar_materialized import MaterializedRadar
from takealot_ops.storage.models import CompetitorSnapshot, CompetitorTarget, ErpStore, OfferCurrent


def load_match_catalog(engine: Engine, store_codes: set[str]) -> dict[str, Any]:
    """Explicit cross-store Core reads expose only authorized private identities.

    Global ownership is used solely to exclude private PLIDs from public results.
    Breadcrumbs use the latest nonempty captured path, just like the full radar.
    Dates and inventory revisions do not form part of the matching identity hash.
    """
    offers, stores = OfferCurrent.__table__, ErpStore.__table__
    snapshots, targets = CompetitorSnapshot.__table__, CompetitorTarget.__table__
    with engine.connect() as connection:
        connected = set(connection.scalars(select(stores.c.code).where(
            stores.c.active.is_(True), stores.c.data_connected.is_(True),
        )))
        # Legacy installations without a store catalog use the default store.
        if not connection.scalar(select(func.count()).select_from(stores)):
            connected = {"current"}
        owned = set(connection.scalars(select(offers.c.productline_id).where(
            offers.c.store_code.in_(connected), offers.c.productline_id.is_not(None),
        )))
        private = list(connection.execute(select(
            offers.c.productline_id, offers.c.title, stores.c.display_name, offers.c.sku,
        ).select_from(offers.outerjoin(stores, stores.c.code == offers.c.store_code)).where(
            offers.c.store_code.in_(connected & store_codes),
            offers.c.productline_id.is_not(None),
        ).order_by(offers.c.captured_at.desc(), offers.c.offer_id)))
        public = list(connection.execute(select(targets.c.plid, targets.c.title, targets.c.url).where(
            targets.c.active.is_(True), targets.c.plid.not_in(owned),
        )))
        eligible = {str(r.productline_id) for r in private} | {str(r.plid) for r in public}
        if not eligible:
            return _catalog_payload([])
        latest = select(snapshots.c.plid, func.max(snapshots.c.collected_at).label("at")).where(
            snapshots.c.plid.in_(eligible),
        ).group_by(snapshots.c.plid).subquery()
        evidence = {r.plid: r for r in connection.execute(select(
            snapshots.c.plid, snapshots.c.title, snapshots.c.url, snapshots.c.seller_name, snapshots.c.category_path,
        ).join(latest, (latest.c.plid == snapshots.c.plid) & (latest.c.at == snapshots.c.collected_at)))}
        paths = {plid: path for plid, row in evidence.items() if (path := _category_path(row.category_path))}
        missing_paths = evidence.keys() - paths.keys()
        # JSON length works on both the production MySQL database and SQLite fixtures.
        path_length = func.json_array_length if engine.dialect.name == "sqlite" else func.json_length
        path_latest = select(snapshots.c.plid, func.max(snapshots.c.collected_at).label("at")).where(
            snapshots.c.plid.in_(missing_paths), path_length(snapshots.c.category_path) > 0,
            func.json_type(snapshots.c.category_path) == ("array" if engine.dialect.name == "sqlite" else "ARRAY"),
        ).group_by(snapshots.c.plid).subquery()
        paths.update({r.plid: _category_path(r.category_path) for r in connection.execute(select(
            snapshots.c.plid, snapshots.c.category_path,
        ).join(path_latest, (path_latest.c.plid == snapshots.c.plid)
               & (path_latest.c.at == snapshots.c.collected_at)))})
    by_plid: dict[str, dict[str, Any]] = {}
    for target in public:
        row = evidence.get(target.plid)
        if row is not None:
            by_plid[target.plid] = {
                "plid": target.plid, "来源": "competitor", "商品": row.title or target.title or "",
                "链接": row.url or target.url, "当前卖家": row.seller_name or "",
                "类目路径": paths.get(target.plid, []),
            }
    for offer in private:
        plid = str(offer.productline_id)
        row = evidence.get(plid)
        existing = by_plid.get(plid)
        if existing is None:
            existing = by_plid[plid] = {
                "plid": plid, "来源": "own_store", "商品": offer.title or (row.title if row else ""),
                "链接": row.url if row else "", "当前卖家": "", "类目路径": paths.get(plid, []),
            }
        names = [part for part in [existing["当前卖家"], offer.display_name or "", offer.sku or ""] if part]
        existing["当前卖家"] = " · ".join(dict.fromkeys(names))
    return _catalog_payload(sorted(by_plid.values(), key=lambda item: item["plid"]))


def _category_path(raw: Any) -> list[dict[str, str | None]]:
    if not isinstance(raw, list):
        return []
    return [{"name": " ".join(str(item["name"]).split())[:200],
             **{key: str(item.get(key) or "").strip()[:255] or None for key in ("id", "slug", "type")}}
            for item in raw[:12] if isinstance(item, dict) and str(item.get("name") or "").strip()]


def _catalog_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    revision = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"items": items, "revision": revision}


def read_precomputed_match_cards(
    materialized: MaterializedRadar, *, key: tuple[Any, ...], boundary: str,
    version: str, plids: set[str],
) -> dict[str, dict[str, Any]]:
    """Reuse only fresh, complete, exact-scope cards; never start a whole radar build."""
    if not plids or not materialized.path.exists():
        return {}
    digest = hashlib.sha256(repr((materialized.namespace, key, boundary)).encode()).hexdigest()
    try:
        with sqlite3.connect(materialized.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("BEGIN")
            scope = db.execute("SELECT version,generated FROM scopes WHERE key=?", (digest,)).fetchone()
            if not scope or scope[0] != version or time.time() - scope[1] >= 180:
                return {}
            rows = db.execute(
                f"SELECT plid,body FROM cards WHERE scope=? AND plid IN ({','.join('?' for _ in plids)})",
                (digest, *sorted(plids)),
            ).fetchall()
            return {plid: json.loads(zlib.decompress(body)) for plid, body in rows if body is not None}
    except (sqlite3.Error, ValueError, zlib.error):
        # A rebuildable cache is optional; bounded MySQL projection remains authoritative.
        return {}
