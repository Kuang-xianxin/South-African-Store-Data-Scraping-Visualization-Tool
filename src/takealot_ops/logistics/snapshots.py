"""Durable latest-success snapshots for logistics provider data."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from takealot_ops.storage.models import LogisticsProviderSnapshot
from takealot_ops.storage.store_context import current_store_code


def save_provider_snapshot(
    engine: Engine,
    provider: str,
    payload: dict[str, Any],
    *,
    fetched_at: datetime | None = None,
) -> str:
    """Replace one provider's durable snapshot after a successful API read."""
    if not payload.get("connected"):
        raise ValueError("only successful provider payloads may be persisted")
    normalized_provider = provider.strip().casefold()
    if not normalized_provider:
        raise ValueError("provider is required")
    captured_at = fetched_at or datetime.now(UTC)
    stored_payload = deepcopy(payload)
    with Session(engine) as session, session.begin():
        snapshot = session.get(
            LogisticsProviderSnapshot,
            (current_store_code(), normalized_provider),
        )
        if snapshot is None:
            snapshot = LogisticsProviderSnapshot(
                provider=normalized_provider,
                fetched_at=captured_at,
                payload=stored_payload,
            )
            session.add(snapshot)
        else:
            snapshot.fetched_at = captured_at
            snapshot.payload = stored_payload
    return captured_at.isoformat()


def load_provider_snapshot(engine: Engine, provider: str) -> dict[str, Any] | None:
    """Return a detached copy of the latest successful provider payload."""
    normalized_provider = provider.strip().casefold()
    with Session(engine) as session:
        snapshot = session.scalar(
            select(LogisticsProviderSnapshot).where(
                LogisticsProviderSnapshot.provider == normalized_provider
            )
        )
        if snapshot is None:
            return None
        fetched_at = snapshot.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return {
            "provider": snapshot.provider,
            "fetched_at": fetched_at.isoformat(),
            "payload": deepcopy(snapshot.payload),
        }
