"""Unattended rotating checks for follower offers on current own-store products."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorCollector,
)
from takealot_ops.storage.models import (
    CompetitorSnapshot,
    ErpStore,
    OfferCurrent,
    OwnStoreFollowerTracking,
)
from takealot_ops.storage.store_context import store_scope


@dataclass(frozen=True)
class AutomaticFollowerTarget:
    """One deduplicated own-store PLID selected for the next rotation."""

    plid: str
    url: str
    store_codes: tuple[str, ...]
    last_attempted_at: datetime | None


@dataclass(frozen=True)
class AutomaticFollowerRunResult:
    """Bounded summary for one unattended rotation."""

    available_targets: int
    selected_targets: int
    attempted: int
    succeeded: int
    partial: int
    failed: int


def select_automatic_follower_targets(
    engine: Engine,
    *,
    max_targets: int | None,
    store_codes: Sequence[str] | None = None,
) -> tuple[int, list[AutomaticFollowerTarget]]:
    """Select never-checked then stalest current own-store PLIDs across stores."""
    if max_targets is not None and max_targets < 1:
        raise ValueError("max_targets must be at least 1")

    requested_codes = {str(code).strip().casefold() for code in store_codes or ()}
    plid_stores: dict[str, set[str]] = {}
    with Session(engine) as session:
        stores = list(
            session.scalars(
                select(ErpStore)
                .where(ErpStore.active.is_(True), ErpStore.data_connected.is_(True))
                .order_by(ErpStore.code)
            )
        )
        if requested_codes:
            stores = [store for store in stores if store.code in requested_codes]
        for store in stores:
            with store_scope(store.code):
                plids = session.scalars(
                    select(OfferCurrent.productline_id).where(
                        OfferCurrent.productline_id.is_not(None)
                    )
                )
                for raw_plid in plids:
                    plid = str(raw_plid or "").strip()
                    if plid:
                        plid_stores.setdefault(plid, set()).add(store.code)

        tracking = {
            row.plid: row
            for row in session.scalars(select(OwnStoreFollowerTracking))
            if row.plid in plid_stores
        }
        latest_snapshots = {
            str(plid): collected_at
            for plid, collected_at in session.execute(
                select(
                    CompetitorSnapshot.plid,
                    func.max(CompetitorSnapshot.collected_at),
                ).group_by(CompetitorSnapshot.plid)
            )
            if str(plid) in plid_stores
        }

    def effective_attempt(plid: str) -> datetime | None:
        state = tracking.get(plid)
        if state is not None:
            return state.last_attempted_at
        return latest_snapshots.get(plid)

    ordered_plids = sorted(
        plid_stores,
        key=lambda plid: (_sortable_datetime(effective_attempt(plid)), plid),
    )
    selected_plids = ordered_plids if max_targets is None else ordered_plids[:max_targets]
    selected = [
        AutomaticFollowerTarget(
            plid=plid,
            url=f"https://www.takealot.com/p/PLID{plid}",
            store_codes=tuple(sorted(plid_stores[plid])),
            last_attempted_at=effective_attempt(plid),
        )
        for plid in selected_plids
    ]
    return len(ordered_plids), selected


def record_automatic_follower_attempt(
    engine: Engine,
    *,
    plid: str,
    attempted_at: datetime,
    result: CompetitorCollectionResult | None,
    error_type: str | None = None,
) -> str:
    """Persist scheduling state even when a public-page attempt fails."""
    partial = result is not None and result.failure_kind == "stock-unprobed"
    succeeded = result is not None and (result.succeeded or partial)
    status = "partial" if partial else "success" if succeeded else "failed"
    message = (
        result.message
        if result is not None
        else f"{error_type or 'UnexpectedError'}: automatic follower collection failed"
    )
    with Session(engine) as session, session.begin():
        state = session.get(OwnStoreFollowerTracking, plid)
        if state is None:
            state = OwnStoreFollowerTracking(
                plid=plid,
                last_attempted_at=attempted_at,
                last_succeeded_at=attempted_at if succeeded else None,
                last_status=status,
                consecutive_failures=0 if succeeded else 1,
                last_message=message,
            )
            session.add(state)
        else:
            state.last_attempted_at = attempted_at
            state.last_status = status
            state.last_message = message
            if succeeded:
                state.last_succeeded_at = attempted_at
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
    return status


async def run_automatic_follower_tracking(
    engine: Engine,
    *,
    project_root: Path,
    max_targets: int | None,
    with_stock_probe: bool,
    store_codes: Sequence[str] | None = None,
    minimum_delay_seconds: float = 5.0,
    maximum_delay_seconds: float = 10.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    collector_factory: Callable[[], CompetitorCollector] | None = None,
) -> AutomaticFollowerRunResult:
    """Check one bounded, rotating slice and persist progress after every PLID."""
    if minimum_delay_seconds < 0 or maximum_delay_seconds < minimum_delay_seconds:
        raise ValueError("invalid automatic follower delay range")
    available, targets = select_automatic_follower_targets(
        engine,
        max_targets=max_targets,
        store_codes=store_codes,
    )
    attempted = succeeded = partial = failed = 0
    if not targets:
        return AutomaticFollowerRunResult(available, 0, 0, 0, 0, 0)

    collector = (
        collector_factory()
        if collector_factory is not None
        else CompetitorCollector(engine=engine, project_root=project_root)
    )
    delay_random = random.SystemRandom()
    async with collector:
        for index, target in enumerate(targets):
            if index:
                await sleep(
                    delay_random.uniform(minimum_delay_seconds, maximum_delay_seconds)
                )
            attempted_at = datetime.now(UTC)
            result: CompetitorCollectionResult | None = None
            error_type: str | None = None
            try:
                result = await collector.collect(
                    target.url,
                    with_stock_probe=with_stock_probe,
                    visible_browser=False,
                    followers_only=True,
                )
            except Exception as error:  # keep the rotation moving after one bad PLID
                error_type = type(error).__name__
            status = record_automatic_follower_attempt(
                engine,
                plid=target.plid,
                attempted_at=attempted_at,
                result=result,
                error_type=error_type,
            )
            attempted += 1
            if status == "success":
                succeeded += 1
            elif status == "partial":
                partial += 1
            else:
                failed += 1

    return AutomaticFollowerRunResult(
        available_targets=available,
        selected_targets=len(targets),
        attempted=attempted,
        succeeded=succeeded,
        partial=partial,
        failed=failed,
    )


def _sortable_datetime(value: datetime | None) -> tuple[int, datetime]:
    if value is None:
        return (0, datetime.min)
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return (1, value)
