"""Shared persistence for public offer targets discovered during collection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store import connected_store_plids
from takealot_ops.competitors.service import CompetitorDiscoveredTarget
from takealot_ops.storage.models import CompetitorTarget, CompetitorTargetAudit


def sync_discovered_competitor_targets(
    engine: Engine,
    *,
    origin_plid: str,
    discovered_targets: Sequence[CompetitorDiscoveredTarget],
    actor_username: str,
    actor_display_name: str,
    actor_user_id: int | None = None,
    changed_at: datetime | None = None,
) -> tuple[CompetitorDiscoveredTarget, ...]:
    """Persist new/reactivated offers while preserving existing target grouping."""

    if not discovered_targets:
        return ()
    now = _as_utc(changed_at or datetime.now(UTC))
    username = _required_text(actor_username, "actor_username", maximum=64)
    display_name = _required_text(
        actor_display_name,
        "actor_display_name",
        maximum=100,
    )
    normalized_origin = _required_text(origin_plid, "origin_plid", maximum=30)
    added: list[CompetitorDiscoveredTarget] = []
    with Session(engine) as session:
        store_plids = connected_store_plids(session)
        unique_targets = {
            target.plid: target
            for target in discovered_targets
            if target.plid not in store_plids
        }
        origin = unique_targets.get(normalized_origin)
        if origin is None:
            return ()
        origin_row = session.get(CompetitorTarget, normalized_origin)
        if origin_row is None:
            origin_row = CompetitorTarget(
                plid=origin.plid,
                offer_group_plid=origin.plid,
                url=origin.url,
                title=origin.title,
                active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(origin_row)
            session.flush()
        group_plid = origin_row.offer_group_plid or origin_row.plid
        origin_row.offer_group_plid = group_plid

        existing_rows = {
            target_plid: session.get(CompetitorTarget, target_plid)
            for target_plid in unique_targets
        }
        merged_group_ids = {
            row.offer_group_plid or row.plid
            for row in existing_rows.values()
            if row is not None
        }
        if merged_group_ids:
            for group_member in session.scalars(
                select(CompetitorTarget).where(
                    CompetitorTarget.offer_group_plid.in_(merged_group_ids)
                )
            ):
                group_member.offer_group_plid = group_plid

        for target_plid, discovered in unique_targets.items():
            target_row = existing_rows.get(target_plid)
            added_now = False
            if target_row is None:
                target_row = CompetitorTarget(
                    plid=target_plid,
                    offer_group_plid=group_plid,
                    url=discovered.url,
                    title=discovered.title,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(target_row)
                if target_plid != normalized_origin:
                    added.append(discovered)
                    added_now = True
            else:
                target_row.offer_group_plid = group_plid
                if not target_row.title:
                    target_row.title = discovered.title
                if not target_row.active:
                    target_row.url = discovered.url
                    target_row.active = True
                    target_row.updated_at = now
                    if target_plid != normalized_origin:
                        added.append(discovered)
                        added_now = True
            if added_now:
                session.add(
                    CompetitorTargetAudit(
                        plid=target_plid,
                        action="auto_discover",
                        old_url=None,
                        new_url=discovered.url,
                        actor_user_id=actor_user_id,
                        actor_username=username,
                        actor_display_name=display_name,
                        changed_at=now,
                    )
                )
        session.commit()
    return tuple(added)


def discovered_target_payload(
    target: CompetitorDiscoveredTarget,
) -> dict[str, object]:
    """Serialize one discovered target for the durable job result."""

    return {
        "plid": target.plid,
        "url": target.url,
        "title": target.title,
        "seller_name": target.seller_name,
        "price": target.price,
        "selected": target.selected,
    }


def _required_text(value: object, label: str, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return text


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
