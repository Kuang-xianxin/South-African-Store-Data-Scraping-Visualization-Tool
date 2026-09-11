"""Database-backed leases for safely sharing competitor crawl work across hosts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable
from uuid import uuid4

from sqlalchemy import Engine, Select, and_, func, or_, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from takealot_ops.storage.models import (
    CompetitorCollectionJob,
    CompetitorWorkerHeartbeat,
)

PENDING_JOB_STATUSES = ("pending", "retry")
FINISHED_JOB_STATUSES = ("succeeded", "terminal", "cancelled")


@dataclass(frozen=True)
class DistributedJobTarget:
    """One idempotent target to add to a distributed collection batch."""

    item_index: int
    plid: str
    url: str
    followers_only: bool = False
    with_stock_probe: bool = True
    priority: int = 0
    max_attempts: int = 3


@dataclass(frozen=True)
class DistributedJobLease:
    """A time-bounded claim that must accompany all job updates."""

    job_id: int
    batch_id: str
    item_index: int
    plid: str
    url: str
    followers_only: bool
    with_stock_probe: bool
    attempt: int
    max_attempts: int
    worker_id: str
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class DistributedJobOutcome:
    """The bounded result returned by one crawler attempt."""

    succeeded: bool
    title: str | None
    message: str
    failure_kind: str | None
    retryable: bool
    discovered_targets: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class DistributedBatchStatus:
    """Counts for one durable batch without loading every result body."""

    batch_id: str
    total: int
    pending: int
    leased: int
    succeeded: int
    retry: int
    terminal: int
    cancelled: int

    @property
    def finished(self) -> int:
        return self.succeeded + self.terminal + self.cancelled


class DistributedCompetitorQueue:
    """Claim competitor jobs with atomic compare-and-swap leases.

    The claim update includes the complete eligibility predicate. Two workers may
    read the same candidate, but only one can change it to its unique lease token.
    The loser retries and claims a different row, so correctness does not depend on
    a process-local lock or on one particular database backend's SKIP LOCKED support.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._clock = clock or (lambda: datetime.now(UTC))

    def enqueue(
        self,
        batch_id: str,
        targets: Iterable[DistributedJobTarget],
    ) -> int:
        """Add each batch/PLID once and return the number of new rows."""

        normalized_batch_id = _required_text(batch_id, "batch_id", maximum=100)
        now = self._now()
        added = 0
        with self._sessions.begin() as session:
            existing_plids = set(
                session.scalars(
                    select(CompetitorCollectionJob.plid).where(
                        CompetitorCollectionJob.batch_id == normalized_batch_id
                    )
                )
            )
            seen = set(existing_plids)
            for target in targets:
                plid = _required_text(target.plid, "plid", maximum=30)
                if plid in seen:
                    continue
                url = _required_text(target.url, "url")
                if target.item_index < 0:
                    raise ValueError("item_index must be non-negative")
                if target.max_attempts < 1:
                    raise ValueError("max_attempts must be positive")
                session.add(
                    CompetitorCollectionJob(
                        batch_id=normalized_batch_id,
                        item_index=target.item_index,
                        plid=plid,
                        url=url,
                        followers_only=target.followers_only,
                        with_stock_probe=target.with_stock_probe,
                        status="pending",
                        priority=target.priority,
                        attempt_count=0,
                        max_attempts=target.max_attempts,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                seen.add(plid)
                added += 1
        return added

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 900,
        batch_id: str | None = None,
        retries: int = 20,
    ) -> DistributedJobLease | None:
        """Atomically lease the next eligible job, reclaiming expired leases."""

        normalized_worker_id = _required_text(worker_id, "worker_id", maximum=100)
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        if retries < 1:
            raise ValueError("retries must be positive")
        normalized_batch_id = (
            _required_text(batch_id, "batch_id", maximum=100)
            if batch_id is not None
            else None
        )

        for _ in range(retries):
            now = self._now()
            eligible = _eligible_job_predicate(now)
            candidate_statement: Select[tuple[int]] = (
                select(CompetitorCollectionJob.id)
                .where(eligible)
                .order_by(
                    CompetitorCollectionJob.priority.desc(),
                    CompetitorCollectionJob.item_index.asc(),
                    CompetitorCollectionJob.id.asc(),
                )
                .limit(1)
            )
            if normalized_batch_id is not None:
                candidate_statement = candidate_statement.where(
                    CompetitorCollectionJob.batch_id == normalized_batch_id
                )

            with self._sessions.begin() as session:
                # A worker that dies on its last allowed attempt must not cause
                # unlimited reclaims of a supposedly single-attempt canary.
                exhausted = update(CompetitorCollectionJob).where(
                    CompetitorCollectionJob.status == "leased",
                    CompetitorCollectionJob.lease_expires_at < now,
                    CompetitorCollectionJob.attempt_count >= CompetitorCollectionJob.max_attempts,
                )
                if normalized_batch_id is not None:
                    exhausted = exhausted.where(
                        CompetitorCollectionJob.batch_id == normalized_batch_id
                    )
                session.execute(exhausted.values(
                    status="terminal", lease_owner=None, lease_token=None, lease_expires_at=None,
                    finished_at=now, updated_at=now, failure_kind="lease-expired",
                    retryable=False, message="任务租约过期且达到尝试上限；结果需核对，不再自动重复采集",
                ))
                candidate_id = session.scalar(candidate_statement)
                if candidate_id is None:
                    return None
                token = uuid4().hex
                expires_at = now + timedelta(seconds=lease_seconds)
                result = session.execute(
                    update(CompetitorCollectionJob)
                    .where(
                        CompetitorCollectionJob.id == candidate_id,
                        _eligible_job_predicate(now),
                    )
                    .values(
                        status="leased",
                        attempt_count=CompetitorCollectionJob.attempt_count + 1,
                        lease_owner=normalized_worker_id,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        claimed_at=now,
                        finished_at=None,
                        updated_at=now,
                    )
                )
                if getattr(result, "rowcount", 0) != 1:
                    continue
                row = session.get(CompetitorCollectionJob, candidate_id)
                if row is None:
                    raise RuntimeError("claimed competitor job disappeared")
                return _lease_from_row(row)
        return None

    def renew(
        self,
        lease: DistributedJobLease,
        *,
        lease_seconds: int = 900,
    ) -> DistributedJobLease | None:
        """Extend a live lease; stale or replaced tokens cannot be renewed."""

        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._sessions.begin() as session:
            result = session.execute(
                update(CompetitorCollectionJob)
                .where(
                    CompetitorCollectionJob.id == lease.job_id,
                    CompetitorCollectionJob.status == "leased",
                    CompetitorCollectionJob.lease_owner == lease.worker_id,
                    CompetitorCollectionJob.lease_token == lease.lease_token,
                    CompetitorCollectionJob.lease_expires_at >= now,
                )
                .values(lease_expires_at=expires_at, updated_at=now)
            )
            if getattr(result, "rowcount", 0) != 1:
                return None
            row = session.get(CompetitorCollectionJob, lease.job_id)
            return _lease_from_row(row) if row is not None else None

    def finish(
        self,
        lease: DistributedJobLease,
        outcome: DistributedJobOutcome,
        *,
        retry_delay_seconds: int = 60,
    ) -> str | None:
        """Publish one result if the caller still owns the lease.

        Returns the new status, or ``None`` when the lease token is stale.
        """

        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        now = self._now()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(CompetitorCollectionJob).where(
                    CompetitorCollectionJob.id == lease.job_id,
                    CompetitorCollectionJob.status == "leased",
                    CompetitorCollectionJob.lease_owner == lease.worker_id,
                    CompetitorCollectionJob.lease_token == lease.lease_token,
                    CompetitorCollectionJob.lease_expires_at >= now,
                )
            )
            if row is None:
                return None
            can_retry = (
                not outcome.succeeded
                and outcome.retryable
                and row.attempt_count < row.max_attempts
            )
            if outcome.succeeded:
                new_status = "succeeded"
            elif can_retry:
                new_status = "retry"
            else:
                new_status = "terminal"
            result = session.execute(
                update(CompetitorCollectionJob)
                .where(
                    CompetitorCollectionJob.id == lease.job_id,
                    CompetitorCollectionJob.status == "leased",
                    CompetitorCollectionJob.lease_owner == lease.worker_id,
                    CompetitorCollectionJob.lease_token == lease.lease_token,
                    CompetitorCollectionJob.lease_expires_at >= self._now(),
                )
                .values(
                    status=new_status,
                    available_at=(now + timedelta(seconds=retry_delay_seconds)
                                  if new_status == "retry" else now),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=None if new_status == "retry" else now,
                    title=outcome.title,
                    message=_single_line(outcome.message),
                    failure_kind=outcome.failure_kind,
                    retryable=outcome.retryable,
                    discovered_targets=[dict(item) for item in outcome.discovered_targets] or None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            return new_status if getattr(result, "rowcount", 0) == 1 else None

    def heartbeat(
        self,
        *,
        worker_id: str,
        node_name: str,
        egress_label: str,
        state: str,
        started_at: datetime,
        current_job_id: int | None = None,
        current_plid: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Upsert one worker's latest liveness without retaining a history stream."""

        normalized_worker_id = _required_text(worker_id, "worker_id", maximum=100)
        now = self._now()
        with self._sessions.begin() as session:
            row = session.get(CompetitorWorkerHeartbeat, normalized_worker_id)
            if row is None:
                row = CompetitorWorkerHeartbeat(
                    worker_id=normalized_worker_id,
                    node_name=_required_text(node_name, "node_name", maximum=100),
                    egress_label=_required_text(
                        egress_label,
                        "egress_label",
                        maximum=100,
                    ),
                    state=_required_text(state, "state", maximum=30),
                    current_job_id=current_job_id,
                    current_plid=current_plid,
                    last_error=_single_line(last_error),
                    started_at=_as_utc(started_at),
                    last_seen_at=now,
                )
                session.add(row)
                return
            row.node_name = _required_text(node_name, "node_name", maximum=100)
            row.egress_label = _required_text(
                egress_label,
                "egress_label",
                maximum=100,
            )
            row.state = _required_text(state, "state", maximum=30)
            row.current_job_id = current_job_id
            row.current_plid = current_plid
            row.last_error = _single_line(last_error)
            row.last_seen_at = now

    def batch_status(self, batch_id: str) -> DistributedBatchStatus:
        """Return status counts for one batch."""

        normalized_batch_id = _required_text(batch_id, "batch_id", maximum=100)
        with self._sessions() as session:
            counts = {
                str(status): int(count)
                for status, count in session.execute(
                    select(
                        CompetitorCollectionJob.status,
                        func.count(CompetitorCollectionJob.id),
                    )
                    .where(CompetitorCollectionJob.batch_id == normalized_batch_id)
                    .group_by(CompetitorCollectionJob.status)
                )
            }
        return DistributedBatchStatus(
            batch_id=normalized_batch_id,
            total=sum(counts.values()),
            pending=counts.get("pending", 0),
            leased=counts.get("leased", 0),
            succeeded=counts.get("succeeded", 0),
            retry=counts.get("retry", 0),
            terminal=counts.get("terminal", 0),
            cancelled=counts.get("cancelled", 0),
        )

    def _now(self) -> datetime:
        return _as_utc(self._clock())


def _eligible_job_predicate(now: datetime) -> ColumnElement[bool]:
    return or_(
        and_(
            CompetitorCollectionJob.status.in_(PENDING_JOB_STATUSES),
            CompetitorCollectionJob.available_at <= now,
        ),
        and_(
            CompetitorCollectionJob.status == "leased",
            CompetitorCollectionJob.lease_expires_at.is_not(None),
            CompetitorCollectionJob.lease_expires_at < now,
        ),
    )


def _lease_from_row(row: CompetitorCollectionJob) -> DistributedJobLease:
    if row.lease_token is None or row.lease_owner is None or row.lease_expires_at is None:
        raise RuntimeError("competitor job lease fields are incomplete")
    return DistributedJobLease(
        job_id=row.id,
        batch_id=row.batch_id,
        item_index=row.item_index,
        plid=row.plid,
        url=row.url,
        followers_only=row.followers_only,
        with_stock_probe=row.with_stock_probe,
        attempt=row.attempt_count,
        max_attempts=row.max_attempts,
        worker_id=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=_as_utc(row.lease_expires_at),
    )


def _required_text(value: object, label: str, *, maximum: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{label} is required")
    if maximum is not None and len(text) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return text


def _single_line(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
