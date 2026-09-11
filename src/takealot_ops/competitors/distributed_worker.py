"""Standalone competitor crawler worker for one trusted Tailscale node."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from takealot_ops.competitors.api import CompetitorNetworkError, CompetitorPublicClient
from takealot_ops.competitors.batch import configure_collection_logger
from takealot_ops.competitors.distributed_queue import (
    DistributedCompetitorQueue,
    DistributedJobLease,
    DistributedJobOutcome,
)
from takealot_ops.competitors.service import (
    CompetitorCollector,
    CompetitorDiscoveredTarget,
)
from takealot_ops.competitors.target_sync import (
    discovered_target_payload,
    sync_discovered_competitor_targets,
)
EXPECTED_PRIMARY_HOST = "100.70.103.11"
EXPECTED_PRIMARY_PORT = 13306
EXPECTED_PRIMARY_SERVER_ID = 1
REQUIRED_TABLES = {
    "competitor_collection_jobs",
    "competitor_worker_heartbeats",
    "competitor_targets",
    "competitor_target_audits",
    "competitor_link_health",
    "competitor_snapshots",
    "competitor_variant_snapshots",
    "competitor_reviews",
    "erp_stores",
    "offer_current",
}


@dataclass(frozen=True)
class DistributedWorkerSettings:
    """Validated runtime settings with the password-bearing URL kept out of logs."""

    project_root: Path
    database_url: str
    worker_id: str
    node_name: str
    egress_label: str
    proxy_server: str
    batch_id: str | None = None
    lease_seconds: int = 1800
    idle_poll_seconds: float = 5.0
    max_jobs: int | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path,
        worker_id: str,
        egress_label: str,
        proxy_server: str,
        batch_id: str | None = None,
        lease_seconds: int = 1800,
        idle_poll_seconds: float = 5.0,
        max_jobs: int | None = None,
    ) -> DistributedWorkerSettings:
        database_url = os.environ.get("TAKEALOT_DISTRIBUTED_DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("TAKEALOT_DISTRIBUTED_DATABASE_URL is required")
        return cls(
            project_root=project_root.resolve(),
            database_url=database_url,
            worker_id=_required_text(worker_id, "worker_id", maximum=100),
            node_name=_required_text(socket.gethostname(), "node_name", maximum=100),
            egress_label=_required_text(egress_label, "egress_label", maximum=100),
            proxy_server=_validated_loopback_proxy(proxy_server),
            batch_id=(
                _required_text(batch_id, "batch_id", maximum=100)
                if batch_id is not None
                else None
            ),
            lease_seconds=lease_seconds,
            idle_poll_seconds=idle_poll_seconds,
            max_jobs=max_jobs,
        )


class DistributedCompetitorWorker:
    """Claim, collect, renew, and finish jobs without enabling the blue web app."""

    def __init__(
        self,
        settings: DistributedWorkerSettings,
        *,
        engine: Engine | None = None,
        logger: logging.Logger | None = None,
        client_factory: Callable[[], CompetitorPublicClient] | None = None,
        engine_validator: Callable[[Engine], None] | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine or create_distributed_worker_engine(settings.database_url)
        self._owns_engine = engine is None
        self._queue = DistributedCompetitorQueue(self._engine)
        self._logger = logger or configure_collection_logger(settings.project_root)
        self._client_factory = client_factory or (
            lambda: CompetitorPublicClient(proxy_server=settings.proxy_server)
        )
        self._started_at = datetime.now(UTC)
        self._engine_validator = engine_validator or validate_primary_worker_engine
        self._closing = False

    async def run(self) -> int:
        """Run until stopped or until ``max_jobs`` completed; return completed count."""

        self._engine_validator(self._engine)
        client = self._client_factory()
        completed = 0
        self._heartbeat(state="starting")
        try:
            while not self._closing:
                if self._settings.max_jobs is not None and completed >= self._settings.max_jobs:
                    break
                lease = await asyncio.to_thread(
                    self._queue.claim,
                    self._settings.worker_id,
                    lease_seconds=self._settings.lease_seconds,
                    batch_id=self._settings.batch_id,
                )
                if lease is None:
                    self._heartbeat(state="idle")
                    if self._settings.max_jobs is not None:
                        break
                    await asyncio.sleep(self._settings.idle_poll_seconds)
                    continue
                self._heartbeat(
                    state="collecting",
                    current_job_id=lease.job_id,
                    current_plid=lease.plid,
                )
                outcome = await self._collect_with_renewal(client, lease)
                status = await asyncio.to_thread(self._queue.finish, lease, outcome)
                if status is None:
                    self._logger.error(
                        "distributed_job_stale worker=%s job=%s plid=%s",
                        self._settings.worker_id,
                        lease.job_id,
                        lease.plid,
                    )
                    self._heartbeat(
                        state="lease_lost",
                        last_error="任务租约已失效，结果未登记",
                    )
                    continue
                completed += 1
                self._logger.info(
                    "distributed_job_finished worker=%s job=%s batch=%s plid=%s "
                    "attempt=%s status=%s succeeded=%s reason=%s",
                    self._settings.worker_id,
                    lease.job_id,
                    lease.batch_id,
                    lease.plid,
                    lease.attempt,
                    status,
                    outcome.succeeded,
                    _single_line(outcome.message),
                )
                self._heartbeat(state="idle")
        finally:
            try:
                await client.close()
            finally:
                self._heartbeat(state="stopped")
                if self._owns_engine:
                    self._engine.dispose()
        return completed

    def stop(self) -> None:
        self._closing = True

    async def _collect_with_renewal(
        self,
        client: CompetitorPublicClient,
        lease: DistributedJobLease,
    ) -> DistributedJobOutcome:
        lost = asyncio.Event()
        renew_task = asyncio.create_task(self._renew_lease(lease, lost))
        collect_task = asyncio.create_task(self._collect_one(client, lease))
        lost_task = asyncio.create_task(lost.wait())
        try:
            done, _ = await asyncio.wait(
                {collect_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done and lost.is_set() and not collect_task.done():
                collect_task.cancel()
                await asyncio.gather(collect_task, return_exceptions=True)
                return DistributedJobOutcome(
                    succeeded=False,
                    title=None,
                    message="任务租约续期失败，已停止本次采集",
                    failure_kind="lease-lost",
                    retryable=True,
                )
            return await collect_task
        finally:
            lost_task.cancel()
            renew_task.cancel()
            if not collect_task.done():
                collect_task.cancel()
            await asyncio.gather(lost_task, renew_task, collect_task, return_exceptions=True)

    async def _renew_lease(
        self,
        lease: DistributedJobLease,
        lost: asyncio.Event,
    ) -> None:
        interval = max(10.0, self._settings.lease_seconds / 3)
        current = lease
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    self._queue.renew,
                    current,
                    lease_seconds=self._settings.lease_seconds,
                )
            except Exception:
                # Never leave collection running after a renewal task dies on a DB outage.
                # Avoid logging exception text, which can contain a credential-bearing URL.
                lost.set()
                self._logger.error("distributed_lease_renewal_failed job=%s", lease.job_id)
                return
            if renewed is None:
                lost.set()
                return
            current = renewed
            try:
                self._heartbeat(
                    state="collecting",
                    current_job_id=lease.job_id,
                    current_plid=lease.plid,
                )
            except Exception:
                lost.set()
                self._logger.error("distributed_lease_heartbeat_failed job=%s", lease.job_id)
                return

    async def _collect_one(
        self,
        client: CompetitorPublicClient,
        lease: DistributedJobLease,
    ) -> DistributedJobOutcome:
        try:
            async with CompetitorCollector(
                engine=self._engine,
                project_root=self._settings.project_root,
                client=client,
                browser_proxy_server=self._settings.proxy_server,
            ) as collector:
                result = await collector.collect(
                    lease.url,
                    with_stock_probe=lease.with_stock_probe,
                    visible_browser=False,
                    followers_only=lease.followers_only,
                )
            added: tuple[CompetitorDiscoveredTarget, ...] = ()
            if not lease.followers_only and result.discovered_targets:
                added = await asyncio.to_thread(
                    sync_discovered_competitor_targets,
                    self._engine,
                    origin_plid=lease.plid,
                    discovered_targets=result.discovered_targets,
                    actor_username=self._settings.worker_id,
                    actor_display_name=f"分布式爬虫 {self._settings.node_name}",
                )
            message = result.message
            if added:
                message = f"{message}；另发现并加入 {len(added)} 条跟卖链接"
            return DistributedJobOutcome(
                succeeded=result.succeeded,
                title=result.title,
                message=message,
                failure_kind=result.failure_kind,
                retryable=result.retryable,
                discovered_targets=tuple(
                    discovered_target_payload(target)
                    for target in result.discovered_targets
                ),
            )
        except asyncio.CancelledError:
            raise
        except CompetitorNetworkError as exc:
            return DistributedJobOutcome(
                succeeded=False,
                title=None,
                message=_single_line(str(exc)),
                failure_kind="network",
                retryable=True,
            )
        except Exception as exc:
            self._logger.exception(
                "distributed_job_exception worker=%s job=%s plid=%s",
                self._settings.worker_id,
                lease.job_id,
                lease.plid,
            )
            return DistributedJobOutcome(
                succeeded=False,
                title=None,
                message=_single_line(str(exc)) or type(exc).__name__,
                failure_kind="other",
                retryable=True,
            )

    def _heartbeat(
        self,
        *,
        state: str,
        current_job_id: int | None = None,
        current_plid: str | None = None,
        last_error: str | None = None,
    ) -> None:
        self._queue.heartbeat(
            worker_id=self._settings.worker_id,
            node_name=self._settings.node_name,
            egress_label=self._settings.egress_label,
            state=state,
            current_job_id=current_job_id,
            current_plid=current_plid,
            last_error=last_error,
            started_at=self._started_at,
        )


def validate_primary_worker_engine(engine: Engine) -> None:
    """Refuse a replica, unexpected host, wrong database, or incomplete schema."""

    url = make_url(str(engine.url))
    if url.get_backend_name() != "mysql":
        raise RuntimeError("distributed worker database must be MySQL")
    if url.host != EXPECTED_PRIMARY_HOST or (url.port or 3306) != EXPECTED_PRIMARY_PORT:
        raise RuntimeError("distributed worker must use the main Tailscale MySQL endpoint")
    if url.database != "takealot_ops":
        raise RuntimeError("distributed worker database must be takealot_ops")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT @@hostname, @@server_id, @@global.read_only, "
                "@@global.super_read_only"
            )
        ).one()
    if int(row[1]) != EXPECTED_PRIMARY_SERVER_ID or int(row[2]) != 0 or int(row[3]) != 0:
        raise RuntimeError(
            "distributed worker refused a database that is not the writable main"
        )
    inspector = inspect(engine)
    missing = sorted(table_name for table_name in REQUIRED_TABLES if not inspector.has_table(table_name))
    if missing:
        raise RuntimeError(f"distributed worker schema is incomplete: {', '.join(missing)}")


def create_distributed_worker_engine(database_url: str) -> Engine:
    """Create the remote-primary engine with TLS and conservative pooling."""

    url = make_url(database_url)
    if url.drivername != "mysql+pymysql":
        raise ValueError("distributed worker database must use mysql+pymysql")
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"ssl": {"check_hostname": False}},
    )


def _validated_loopback_proxy(raw: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy port must be between 1 and 65535") from exc
    if (
        parsed.scheme.casefold() != "socks5"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("proxy must be an unauthenticated loopback socks5 URL")
    return f"socks5://127.0.0.1:{port}"


def _required_text(value: object, label: str, *, maximum: int) -> str:
    rendered = " ".join(str(value or "").split())
    if not rendered:
        raise ValueError(f"{label} is required")
    if len(rendered) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return rendered


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--egress-label", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--lease-seconds", type=int, default=1800)
    parser.add_argument("--idle-poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-jobs", type=int)
    return parser


def main() -> int:
    args = _parser().parse_args()
    settings = DistributedWorkerSettings.from_environment(
        project_root=args.project_root,
        worker_id=args.worker_id,
        egress_label=args.egress_label,
        proxy_server=args.proxy,
        batch_id=args.batch_id,
        lease_seconds=args.lease_seconds,
        idle_poll_seconds=args.idle_poll_seconds,
        max_jobs=args.max_jobs,
    )
    worker = DistributedCompetitorWorker(settings)
    completed = asyncio.run(worker.run())
    print(f"distributed_worker_completed={completed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
