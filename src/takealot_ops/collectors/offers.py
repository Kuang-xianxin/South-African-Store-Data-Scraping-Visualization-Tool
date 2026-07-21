"""Atomic offer collection workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from takealot_ops.api.client import TakealotClient, _offer_record_from_api
from takealot_ops.domain import sast_date
from takealot_ops.storage.repository import Repository


@dataclass(frozen=True)
class CollectionResult:
    """Durable outcome of one collection attempt."""

    run_id: str
    status: str
    counts: dict[str, int]
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether the complete collection was published."""
        return self.status == "success"


def collect_offers(
    client: TakealotClient, repository: Repository, captured_at: datetime
) -> CollectionResult:
    """Fetch every offer before atomically publishing one SAST snapshot."""
    snapshot_date = sast_date(captured_at)
    run_id = _persist_run_start(repository, "offers", snapshot_date)
    try:
        raw_items = list(client.iter_items("/offers", {"limit": 100}))
        records = [_offer_record_from_api(item, captured_at) for item in raw_items]
        counts = {"records": len(records)}
        with repository.transaction():
            repository.prune_offer_snapshot(
                snapshot_date, [record.offer_id for record in records]
            )
            for record in records:
                repository.upsert_offer_snapshot(record, snapshot_date)
            repository.finish_run(run_id, "success", counts, None)
    except Exception as error:
        return _persist_run_failure(repository, run_id, error)
    return CollectionResult(run_id=run_id, status="success", counts=counts)


def _persist_run_start(
    repository: Repository, run_type: str, scope_date: date | None = None
) -> str:
    with repository.transaction():
        return repository.begin_run(run_type, scope_date=scope_date)


def _persist_run_failure(
    repository: Repository, run_id: str, error: Exception
) -> CollectionResult:
    counts = {"records": 0}
    safe_error = f"{type(error).__name__}: collection failed"
    with repository.transaction():
        repository.finish_run(run_id, "failed", counts, safe_error)
    return CollectionResult(run_id=run_id, status="failed", counts=counts, error=safe_error)
