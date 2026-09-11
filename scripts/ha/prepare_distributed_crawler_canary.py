"""Prepare the distributed queue schema and enqueue a non-overlapping canary batch."""

from __future__ import annotations

import argparse
import json
import re
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine, Table, func, or_, select, text
from sqlalchemy.orm import Session

from takealot_ops.competitors.distributed_queue import (
    DistributedCompetitorQueue,
    DistributedJobTarget,
)
from takealot_ops.competitors.own_store import connected_store_plids
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import (
    Base,
    CompetitorCollectionJob,
    CompetitorLinkHealth,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorWorkerHeartbeat,
)

EXPECTED_HOSTNAME = "DESKTOP-NTRMANG"
PLID_PATTERN = re.compile(r"PLID(\d+)", re.IGNORECASE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--batch-id")
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


def _database_role(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT CURRENT_USER(), @@hostname, @@server_id, "
                "@@global.read_only, @@global.super_read_only"
            )
        ).one()
    return {
        "current_user": str(row[0]),
        "hostname": str(row[1]),
        "server_id": int(row[2]),
        "read_only": int(row[3]),
        "super_read_only": int(row[4]),
    }


def _excluded_current_plids(project_root: Path) -> set[str]:
    path = project_root / "logs" / "competitor-scheduled-batch.json"
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    excluded: set[str] = set()
    queue = payload.get("queue") if isinstance(payload, dict) else None
    for item in queue if isinstance(queue, list) else []:
        if not isinstance(item, dict):
            continue
        match = PLID_PATTERN.search(str(item.get("url") or ""))
        if match:
            excluded.add(match.group(1))
    active = payload.get("active_item") if isinstance(payload, dict) else None
    if isinstance(active, dict):
        match = PLID_PATTERN.search(str(active.get("url") or ""))
        if match:
            excluded.add(match.group(1))
    return excluded


def _canary_targets(
    engine: Engine,
    *,
    excluded_plids: set[str],
    count: int,
) -> list[DistributedJobTarget]:
    with Session(engine) as session:
        own_plids = connected_store_plids(session)
        latest_snapshot = (
            select(
                CompetitorSnapshot.plid.label("plid"),
                func.max(CompetitorSnapshot.collected_at).label("collected_at"),
            )
            .group_by(CompetitorSnapshot.plid)
            .subquery()
        )
        statement = (
            select(CompetitorTarget)
            .join(
                latest_snapshot,
                latest_snapshot.c.plid == CompetitorTarget.plid,
            )
            .outerjoin(
                CompetitorLinkHealth,
                CompetitorLinkHealth.plid == CompetitorTarget.plid,
            )
            .where(
                CompetitorTarget.active.is_(True),
                or_(
                    CompetitorLinkHealth.plid.is_(None),
                    CompetitorLinkHealth.status == "healthy",
                ),
            )
            .order_by(
                latest_snapshot.c.collected_at.desc(),
                CompetitorTarget.plid.asc(),
            )
        )
        rows = []
        for row in session.scalars(statement):
            if row.plid in excluded_plids or row.plid in own_plids:
                continue
            rows.append(row)
            if len(rows) >= count:
                break
    return [
        DistributedJobTarget(
            item_index=index,
            plid=row.plid,
            url=row.url,
            followers_only=False,
            with_stock_probe=True,
            max_attempts=3,
        )
        for index, row in enumerate(rows)
    ]


def _create_queue_tables(engine: Engine) -> None:
    Base.metadata.create_all(
        engine,
        tables=[
            cast(Table, CompetitorCollectionJob.__table__),
            cast(Table, CompetitorWorkerHeartbeat.__table__),
        ],
        checkfirst=True,
    )


def main() -> int:
    args = _parser().parse_args()
    project_root = args.project_root.resolve()
    if socket.gethostname().casefold() != EXPECTED_HOSTNAME.casefold():
        raise RuntimeError("canary preparation must run on the main computer")
    if not project_root.joinpath("pyproject.toml").is_file():
        raise RuntimeError("project_root is not the Takealot project")
    if args.count < 1 or args.count > 50:
        raise ValueError("count must be between 1 and 50")
    settings = DashboardSettings.from_env(project_root)
    engine = create_engine_for_database_url(settings.database_url)
    try:
        role = _database_role(engine)
        if role["server_id"] != 1 or role["read_only"] != 0 or role["super_read_only"] != 0:
            raise RuntimeError("canary preparation refused a database that is not writable main")
        excluded = _excluded_current_plids(project_root)
        batch_id = args.batch_id or datetime.now(UTC).strftime(
            "distributed-canary-%Y%m%d-%H%M%S"
        )
        targets = [] if args.schema_only else _canary_targets(
            engine,
            excluded_plids=excluded,
            count=args.count,
        )
        if not args.schema_only and len(targets) != args.count:
            raise RuntimeError(
                f"only {len(targets)} non-overlapping canary targets are available"
            )
        result: dict[str, Any] = {
            "execute": bool(args.execute),
            "schema_only": bool(args.schema_only),
            "database_role": role,
            "batch_id": batch_id,
            "selection": "recent-successful-healthy-targets",
            "excluded_current_plids": len(excluded),
            "targets": [target.plid for target in targets],
            "tables": [
                CompetitorCollectionJob.__tablename__,
                CompetitorWorkerHeartbeat.__tablename__,
            ],
        }
        if args.execute:
            _create_queue_tables(engine)
            result["enqueued"] = (
                0
                if args.schema_only
                else DistributedCompetitorQueue(engine).enqueue(batch_id, targets)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
