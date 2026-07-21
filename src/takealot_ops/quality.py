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


def verify_quality(
    repository: Repository, as_of: date, *, start_date: date | None = None
) -> QualityResult:
    """Count quality events in one day or an inclusive business-date window."""
    first_date = start_date or as_of
    if first_date > as_of:
        raise ValueError("start_date cannot be after as_of")
    with repository.transaction():
        events = [
            event
            for event in repository.list_quality_events(as_of)
            if first_date <= event.event_date <= as_of
        ]
        issue_count = len(events)
        unknown_sales_status_count = sum(
            event.event_type == "unknown_sale_status" for event in events
        )
    return QualityResult(
        as_of=as_of,
        issue_count=issue_count,
        unknown_sales_status_count=unknown_sales_status_count,
    )
