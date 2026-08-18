"""Atomic expanded seller-return collection workflow."""

from __future__ import annotations

from datetime import UTC, date, datetime

from takealot_ops.api.client import TakealotClient, _return_record_from_api
from takealot_ops.collectors.offers import (
    CollectionResult,
    _persist_run_start,
    _safe_collection_error,
)
from takealot_ops.storage.repository import Repository


def collect_returns(
    client: TakealotClient,
    repository: Repository,
    start: date,
    end: date,
) -> CollectionResult:
    """Fetch and convert every expanded return page before atomic upsert."""
    run_id = _persist_run_start(repository, "returns", scope_date=end)
    params = {
        "return_date__gte": start.isoformat(),
        "return_date__lte": end.isoformat(),
        "limit": 100,
        "expands": ["outcomes", "transactions"],
    }
    run_counts = {
        "records": 0,
        "requested_start_ordinal": start.toordinal(),
        "requested_end_ordinal": end.toordinal(),
    }
    captured_at = datetime.now(UTC)
    try:
        raw_items = list(client.iter_items("/returns", params))
        converted = [
            (raw_item, _return_record_from_api(raw_item, captured_at))
            for raw_item in raw_items
        ]
        counts = {**run_counts, "records": len(converted)}
        with repository.transaction():
            for raw_item, record in converted:
                repository.upsert_return(record, raw_item)
            repository.finish_run(run_id, "success", counts, None)
    except Exception as error:
        safe_error = _safe_collection_error(error)
        with repository.transaction():
            repository.finish_run(run_id, "failed", run_counts, safe_error)
        return CollectionResult(
            run_id=run_id,
            status="failed",
            counts=run_counts,
            error=safe_error,
        )
    return CollectionResult(run_id=run_id, status="success", counts=counts)
