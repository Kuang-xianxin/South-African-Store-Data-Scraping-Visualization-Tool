"""Supplementary public breadcrumbs, kept separate from historical title reviews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from takealot_ops.storage.models import CompetitorSnapshot


def category_path(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value[:12]
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
            and item["name"].strip()]


def enrich_reference_categories(engine: Engine, benchmarks: dict[str, Any]) -> None:
    """Read only these references' latest nonempty category snapshots; never crawl."""
    with Session(engine) as session:
        for item in (benchmarks.get("items") or [])[:10]:
            if category_path((item.get("category_observation") or {}).get("path")):
                continue
            # JSON length avoids taking an empty latest snapshot over an older path.
            length = func.json_array_length if engine.dialect.name == "sqlite" else func.json_length
            row = session.execute(select(
                CompetitorSnapshot.category_path, CompetitorSnapshot.collected_at,
            ).where(
                CompetitorSnapshot.plid == item["plid"],
                length(CompetitorSnapshot.category_path) > 0,
            ).order_by(CompetitorSnapshot.collected_at.desc(), CompetitorSnapshot.id.desc()).limit(1)).first()
            if row and (path := category_path(row[0])):
                item["category_observation"] = {
                    "status": "available", "path": path, "source": "monitored_snapshot",
                    "captured_at": row[1].isoformat() if row[1] else None,
                }
