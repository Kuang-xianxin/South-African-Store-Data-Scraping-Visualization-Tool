"""Operational data-quality checks shared by the CLI and daily workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from takealot_ops.storage.repository import Repository


@dataclass(frozen=True)
class QualityResult:
    """Summarize durable quality events known through one business date."""

    as_of: date
    issue_count: int
    unknown_sales_status_count: int

    @property
    def passed(self) -> bool:
        return self.issue_count == 0


def verify_quality(repository: Repository, as_of: date) -> QualityResult:
    """Count quality events without changing stored data."""
    with repository.transaction():
        events = [
            event
            for event in repository.list_quality_events(as_of)
            if event.event_date == as_of
        ]
    return QualityResult(
        as_of=as_of,
        issue_count=len(events),
        unknown_sales_status_count=sum(
            event.event_type == "unknown_sale_status" for event in events
        ),
    )
