"""BLUE worker: cached tasks, disk-first results, duplicate-tolerant delivery.

This process never promotes MySQL. Its SQL engine still verifies the fixed BLUE
writer. Offline work is limited to a previously downloaded roster for 30 minutes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil

from blue_crawl_delivery import DeliveryQueue, Observation
from blue_crawl_journal import Journal
from blue_full_crawl import follower_product, valid_own_scopes
from takealot_ops.competitors.api import CompetitorNotFoundError, CompetitorPublicClient
from takealot_ops.competitors.domain import VariantStockObservation
from takealot_ops.competitors.distributed_queue import DistributedCompetitorQueue
from takealot_ops.competitors.service import CompetitorCollector, _aggregate_variant_stock
from takealot_ops.competitors.stock import skipped_stock_probe


@contextmanager
def exclusive_worker(path: Path):
    """OS lock is released after a crash; never infer ownership from file age."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


async def collect_observation(spec: dict, root: Path, proxy: str, persist) -> dict | None:
    """Fetch public competitor evidence without opening any SQL connection."""
    followers_only = spec["followers_only"]
    scopes = valid_own_scopes(spec) if followers_only else set()
    client = CompetitorPublicClient(proxy_server=proxy)
    # A distinct profile prevents collision with the unchanged ordinary BLUE page.
    collector = CompetitorCollector(engine=None, project_root=root / "durable-crawler",
                                    client=client, browser_proxy_server=proxy)
    try:
        async with collector:
            try:
                product = await client.fetch_product(spec["url"])
            except CompetitorNotFoundError:
                control = None
                if not spec.get("already_invalid"):
                    controls = [tuple(row) for row in spec.get("validation_controls", [])
                                if row[0] != spec["plid"]]
                    if controls:
                        control = await client.confirm_product_page_absent(spec["url"], controls)
                # Preserve the actual observation time and control evidence in
                # the immutable envelope before cleanup or network submission.
                persist(None, "not-found|" + datetime.now(UTC).isoformat() + "|" + (control or ""))
                return None
            if product.plid != spec["plid"]:
                raise ValueError("Platform returned a different PLID")
            reviews = await client.fetch_all_reviews(product.plid)
            probe_product = follower_product(product, scopes) if followers_only else product
            variants, offers = await collector._collect_product_stocks(
                probe_product, enabled=spec["with_stock_probe"], visible_browser=False,
                followers_only=followers_only)
            if followers_only:
                variants = [VariantStockObservation(variant=variant, stock=skipped_stock_probe())
                            for variant in product.variants]
            value = Observation(product=product, reviews=reviews,
                stock=skipped_stock_probe() if followers_only else _aggregate_variant_stock(variants),
                variant_stocks=variants,
                offer_stocks=offers, collected_at=datetime.now(UTC)).model_dump(mode="json")
            persist(value)  # Durable before browser cleanup, heartbeat or network upload.
            return value
    finally:
        await client.close()


class ResilientWorker:
    def __init__(self, *, journal: Journal, queue: DeliveryQueue, collect,
                 heartbeat=None, write_status=None):
        self.journal, self.queue, self.collect = journal, queue, collect
        self.heartbeat = heartbeat or (lambda **_: None)
        self.write_status = write_status or (lambda _: None)
        self.online = False

    async def synchronize(self) -> bool:
        try:
            # Upload comes first: only a committed matching receipt allows a local ACK.
            for row in self.journal.pending():
                try:
                    reply = await asyncio.to_thread(self.queue.submit, row["body"], row["digest"])
                except ValueError:
                    # A quarantined/oversized payload must not starve other results.
                    self.journal.block(row["attempt_id"], row["digest"])
                    self.write_status({"state": "delivery-needs-attention", "attempt_id": row["attempt_id"],
                                       "journal": self.journal.counts()})
                    continue
                if reply["attempt_id"] != row["attempt_id"] or reply["sha256"] != row["digest"]:
                    raise ValueError("Central ACK mismatch")
                self.journal.acknowledge(row["attempt_id"], row["digest"], succeeded=reply["succeeded"])
            self.journal.sync(await asyncio.to_thread(self.queue.roster))
            self.online = True
        except Exception as exc:
            self.online = False
            self.write_status({"state": "offline-or-delivery-blocked", "error_type": type(exc).__name__,
                               "journal": self.journal.counts()})
        return self.online

    async def tick(self) -> bool:
        await self.synchronize()
        spec = self.journal.candidate()
        if spec is None:
            await self.report("idle" if self.online else "offline-waiting")
            return False
        token = None
        if self.online:
            try:
                token = await asyncio.to_thread(self.queue.claim, spec)
                if token is None:
                    return False
            except Exception:
                # A lost claim reply is safe: persist a new local attempt, accept duplication.
                self.online = False
        identity = self.journal.begin(spec)
        await self.report("collecting", spec)
        renewal = asyncio.create_task(self.renew(spec, token))
        persisted = False

        def persist(observation, error=""):
            nonlocal persisted
            self.journal.save(dict(version=1, attempt_id=identity, node=self.journal.node,
                task=spec, lease_token=token, observation=observation, error=error))
            persisted = True

        try:
            observation = await asyncio.wait_for(self.collect(spec, persist), timeout=420)
            error = ""
        except asyncio.CancelledError:
            # The durably recorded running attempt is recovered on restart.
            raise
        except Exception as exc:
            observation, error = None, type(exc).__name__
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        # This FULL local commit happens BEFORE any network or MySQL submission.
        if not persisted:
            persist(observation, error)
        await self.synchronize()
        await self.report("idle" if self.online else "result-buffered")
        return True

    async def renew(self, spec, token):
        while True:
            await asyncio.sleep(25)
            if token:
                try:
                    await asyncio.to_thread(self.queue.renew, spec, token)
                except Exception:
                    self.online = False
            await self.report("collecting", spec)

    async def report(self, state: str, spec: dict | None = None):
        counts = self.journal.counts()
        self.write_status({"state": state, "online": self.online, "journal": counts,
                           "current_plid": spec["plid"] if spec else None})
        try:
            await asyncio.to_thread(self.heartbeat, state=state,
                current_job_id=spec["job_id"] if spec else None,
                current_plid=spec["plid"] if spec else None,
                last_error=f"durable-v1 full-v1 pending_uploads={counts.get('pending', 0)} blocked_uploads={counts.get('blocked', 0)}")
        except Exception:
            self.online = False


async def run(root: Path, engine, node: str, computer: str, proxy: str) -> None:
    spool = root / "state/crawl-outbox"
    with exclusive_worker(spool / "worker.lock"):
        journal = Journal(spool / "journal.sqlite3", node)
        journal.recover()
        queue = DeliveryQueue(engine, node)
        heartbeats = DistributedCompetitorQueue(engine)
        started = datetime.now(UTC)

        def heartbeat(**values):
            heartbeats.heartbeat(worker_id="blue-" + node, node_name=computer,
                egress_label=f"blue-{node}-local-proxy", started_at=started, **values)

        def status(values):
            path = root / "state/durable-worker.json"
            temporary = path.with_suffix(".next")
            temporary.write_text(json.dumps(dict(observed_at=datetime.now(UTC).isoformat(),
                node=node, automatic_failover=False, **values)), encoding="utf-8")
            os.replace(temporary, path)

        async def collect(spec, persist):
            return await collect_observation(spec, root, proxy, persist)

        worker = ResilientWorker(journal=journal, queue=queue, collect=collect,
                                  heartbeat=heartbeat, write_status=status)
        while True:
            try:
                ordinary = root / "state/ordinary-crawl-state.json"
                legacy_active = ordinary.exists() and json.loads(ordinary.read_text(encoding="utf-8")).get("active")
                if legacy_active:
                    # Finish/retain pending delivery, but never add a second
                    # browser workload beside this node's original manual run.
                    await worker.synchronize()
                    await worker.report("legacy-busy")
                elif shutil.disk_usage(spool).free < 512 * 1024 * 1024:
                    # Upload existing evidence, but don't risk collecting into a full disk.
                    await worker.synchronize()
                    status({"state": "disk-space-low", "journal": journal.counts()})
                else:
                    await worker.tick()
            except Exception as exc:
                status({"state": "needs-attention", "error_type": type(exc).__name__,
                        "journal": journal.counts()})
            await asyncio.sleep(5)
