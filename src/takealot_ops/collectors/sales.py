"""Atomic sales collection workflow."""

from __future__ import annotations

from datetime import date

from takealot_ops.api.client import TakealotClient, _sale_record_from_api
from takealot_ops.collectors.offers import (
    CollectionResult,
    _persist_run_failure,
    _persist_run_start,
)
from takealot_ops.storage.repository import Repository


def collect_sales(
    client: TakealotClient, repository: Repository, start: date, end: date
) -> CollectionResult:
    """Fetch and convert all inclusive sales pages before atomic upsert."""
    run_id = _persist_run_start(repository, "sales")
    params = {
        "order_date__gte": start.isoformat(),
        "order_date__lte": end.isoformat(),
        "limit": 100,
    }
    try:
        raw_items = list(client.iter_items("/sales", params))
        converted = [(raw_item, _sale_record_from_api(raw_item)) for raw_item in raw_items]
        counts = {"records": len(converted)}
        with repository.transaction():
            for raw_item, record in converted:
                repository.upsert_sale(record, raw_item)
            repository.finish_run(run_id, "success", counts, None)
    except Exception as error:
        return _persist_run_failure(repository, run_id, error)
    return CollectionResult(run_id=run_id, status="success", counts=counts)
