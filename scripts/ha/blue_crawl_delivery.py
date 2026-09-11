"""Single-writer BLUE result ingestion with transactional, idempotent receipts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, insert, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.mysql import LONGTEXT

from blue_crawl_journal import task_key, task_spec
from blue_full_crawl import follower_product, own_contexts, roster_context, valid_own_scopes
from takealot_ops.competitors.domain import (
    CompetitorProduct, CompetitorReviewRecord, OfferStockObservation, StockProbeResult,
    VariantStockObservation, analyze_sales_signal, estimate_lifetime_sales, summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.storage.models import (
    CompetitorCollectionJob, CompetitorLinkHealth, CompetitorReview, CompetitorTarget,
)


metadata = MetaData()
receipts = Table("blue_crawl_delivery_receipts", metadata,
                 Column("attempt_id", String(32), primary_key=True),
                 Column("task_key", String(64), nullable=False, index=True),
                 Column("node", String(16), nullable=False),
                 Column("sha256", String(64), nullable=False),
                 Column("succeeded", Integer, nullable=False),
                 Column("snapshot_id", Integer),
                 Column("body", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
                 Column("received_at", DateTime, nullable=False))


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: CompetitorProduct
    reviews: list[CompetitorReviewRecord]
    stock: StockProbeResult
    variant_stocks: list[VariantStockObservation]
    offer_stocks: list[OfferStockObservation]
    collected_at: AwareDatetime

    def complete(self, spec: dict) -> bool:
        if self.product.plid != spec["plid"]:
            raise ValueError("Observation PLID differs from task")
        expected = {(v.key, v.sku, v.seller_id) for v in self.product.variants}
        observed = {(v.variant.key, v.variant.sku, v.variant.seller_id) for v in self.variant_stocks}
        if expected != observed:
            raise ValueError("Observation variant scope is incomplete")
        followers_only = spec["followers_only"]
        product = follower_product(self.product, valid_own_scopes(spec)) if followers_only else self.product
        expected_offers = {(o.offer_id, o.sku, o.seller_id, o.variant_key)
                           for o in product.offers if followers_only or o.is_follower_offer}
        observed_offers = {(o.offer.offer_id, o.offer.sku, o.offer.seller_id, o.offer.variant_key)
                           for o in self.offer_stocks}
        if expected_offers != observed_offers:
            raise ValueError("Observation offer scope is incomplete")
        if followers_only:
            if any(p.quantity is not None or p.exact for p in
                   [self.stock, *(v.stock for v in self.variant_stocks)]):
                raise ValueError("Own-store inventory must remain Seller-API-only")
            return not spec["with_stock_probe"] or all(
                o.stock.method not in {"failed", "skipped"} for o in self.offer_stocks)
        probes = [self.stock, *(v.stock for v in self.variant_stocks), *(o.stock for o in self.offer_stocks)]
        return not spec["with_stock_probe"] or bool(expected) and all(p.method != "failed" for p in probes)


def validate_envelope(body: str, digest: str) -> tuple[dict, Observation | None, bool]:
    if len(body.encode()) > 24 * 1024 * 1024 or sha256(body.encode()).hexdigest() != digest:
        raise ValueError("Delivery size or digest invalid")
    value = json.loads(body)
    if (set(value) != {"version", "attempt_id", "node", "task", "lease_token", "observation", "error"}
            or value["version"] != 1 or value["node"] not in {"main", "laptop"}
            or not re.fullmatch(r"[a-f0-9]{32}", value["attempt_id"])
            or value["lease_token"] is not None and not re.fullmatch(r"[a-f0-9]{32}", value["lease_token"])
            or not isinstance(value["error"], str) or len(value["error"]) > 200):
        raise ValueError("Invalid delivery envelope")
    task_spec(value["task"])
    observation = Observation.model_validate(value["observation"]) if value["observation"] is not None else None
    complete = observation.complete(value["task"]) if observation else False
    if observation and observation.collected_at > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("Observation time is in the future; check node clock")
    return value, observation, complete and not value["error"]


class LateSafeRepository(CompetitorRepository):
    """Late delivery must not roll review contents or last-seen times backwards."""
    def _upsert_reviews(self, plid, reviews, seen_at):
        existing = {r.review_id: r for r in self._session.scalars(
            select(CompetitorReview).where(CompetitorReview.plid == plid))}
        accepted = []
        for review in {r.review_id: r for r in reviews}.values():
            row = existing.get(review.review_id)
            if row is None or utc(row.last_seen_at) <= seen_at:
                accepted.append(review)
            if row is not None and utc(row.first_seen_at) > seen_at:
                row.first_seen_at = seen_at
        super()._upsert_reviews(plid, accepted, seen_at)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@contextmanager
def observation_lock(conn, plid: str):
    """Serialize own and competitor observations without write grants on Seller API tables."""
    if conn.dialect.name != "mysql":
        yield  # Isolated SQLite fixtures serialize their writes at database level.
        return
    name = "blue-observation-" + plid
    if conn.scalar(text("SELECT GET_LOCK(:name, 3)"), {"name": name}) != 1:
        raise RuntimeError("Another BLUE result for this product is committing; retain and retry")
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
        conn.commit()


class DeliveryQueue:
    def __init__(self, engine, node: str):
        if node not in {"main", "laptop"}:
            raise ValueError("Unknown BLUE delivery node")
        self.engine, self.node = engine, node

    def roster(self) -> list[dict]:
        jobs = CompetitorCollectionJob.__table__
        from sqlalchemy import or_
        with Session(self.engine) as session:
            accepted = or_(jobs.c.batch_id.like("blue-web-%"), jobs.c.batch_id.like("blue-full-%"))
            recent = select(jobs.c.id).where(accepted).order_by(jobs.c.id.desc()).limit(5000).subquery()
            # Active work must never disappear behind the history window, even
            # when the daily roster exceeds 5,000 products.
            rows = session.connection().execute(select(jobs).where(accepted, or_(
                jobs.c.status.in_(("pending", "retry", "leased")), jobs.c.id.in_(select(recent.c.id))))
                .order_by(jobs.c.id.desc())).mappings().all()
            contexts, invalid, controls = roster_context(session) if any(
                row["batch_id"].startswith("blue-full-") for row in rows) else ({}, set(), [])
        now = datetime.now(UTC)
        result = [dict(job_id=r["id"], batch_id=r["batch_id"], plid=r["plid"], url=r["url"],
                     with_stock_probe=bool(r["with_stock_probe"]), followers_only=bool(r["followers_only"]),
                     status=r["status"], lease_owner=r["lease_owner"],
                     available_after=max(0, (utc(r["available_at"]) - now).total_seconds()) if r["available_at"] else 0,
                     lease_until=utc(r["lease_expires_at"]).timestamp() if r["lease_expires_at"] else 0)
                for r in rows]
        for row in result:
            if row["batch_id"].startswith("blue-full-"):
                row.update(already_invalid=row["plid"] in invalid, validation_controls=controls)
                if row["followers_only"]:
                    row["own_offer_scopes"] = contexts.get(row["plid"], [])
        return result

    def claim(self, spec: dict) -> str | None:
        task_spec(spec)
        now, token = datetime.now(UTC), uuid4().hex
        with Session(self.engine) as session, session.begin():
            job = session.scalar(select(CompetitorCollectionJob)
                                 .where(CompetitorCollectionJob.id == spec["job_id"]).with_for_update())
            self._match(job, spec)
            if job.status not in {"pending", "retry", "leased"}:
                return None
            if job.status == "leased" and job.lease_expires_at and utc(job.lease_expires_at) > now:
                return None
            if job.available_at and utc(job.available_at) > now:
                return None
            job.status, job.lease_token, job.lease_owner = "leased", token, "blue-" + self.node
            job.lease_expires_at, job.claimed_at = now + timedelta(seconds=90), now
            job.attempt_count += 1
            job.updated_at = now
        return token

    def renew(self, spec: dict, token: str) -> None:
        # Best effort only: losing this coordination lease no longer destroys data.
        jobs = CompetitorCollectionJob.__table__
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == spec["job_id"], jobs.c.status == "leased",
                         jobs.c.lease_token == token, jobs.c.lease_owner == "blue-" + self.node,
                         jobs.c.lease_expires_at >= now)
                         .values(lease_expires_at=now + timedelta(seconds=90), updated_at=now))

    @staticmethod
    def _match(job, spec):
        if job is None or task_key(dict(job_id=job.id, batch_id=job.batch_id, plid=job.plid,
                                       url=job.url, with_stock_probe=bool(job.with_stock_probe),
                                       followers_only=bool(job.followers_only))) != task_key(spec):
            raise ValueError("Result does not match the authoritative task")

    def submit(self, body: str, digest: str) -> dict:
        value, observation, succeeded = validate_envelope(body, digest)
        if value["node"] != self.node:
            raise ValueError("Result belongs to a different node")
        spec, now = value["task"], datetime.now(UTC)
        with self.engine.connect() as conn, observation_lock(conn, spec["plid"]), Session(bind=conn) as session, session.begin():
            # The immutable receipt, business snapshot and task completion commit together.
            job = session.scalar(select(CompetitorCollectionJob)
                                 .where(CompetitorCollectionJob.id == spec["job_id"]).with_for_update())
            self._match(job, spec)
            prior = session.execute(select(receipts).where(
                receipts.c.attempt_id == value["attempt_id"])).mappings().first()
            if prior:
                if prior["sha256"] != digest or prior["task_key"] != task_key(spec) or prior["node"] != self.node:
                    raise ValueError("An attempt ID was reused with different contents")
                return {"attempt_id": value["attempt_id"], "sha256": digest,
                        "succeeded": bool(prior["succeeded"]), "reused": True}
            snapshot_id = None
            if observation:
                # Serialize same-product review insertion across different batches/nodes.
                target = session.scalar(select(CompetitorTarget).where(
                    CompetitorTarget.plid == spec["plid"]).with_for_update())
                if target is None and not spec["followers_only"]:
                    raise ValueError("Authoritative target missing; retain local delivery")
                if spec["followers_only"]:
                    current_scopes = own_contexts(session).get(spec["plid"])
                    if not current_scopes:
                        raise ValueError("Own-store identity no longer available; retain local delivery")
                    filtered = follower_product(observation.product, set(current_scopes))
                    allowed = {(o.offer_id, o.sku, o.seller_id, o.variant_key) for o in filtered.offers}
                    stocks = [row for row in observation.offer_stocks if
                        (row.offer.offer_id, row.offer.sku, row.offer.seller_id, row.offer.variant_key) in allowed]
                    observed = {(row.offer.offer_id, row.offer.sku, row.offer.seller_id, row.offer.variant_key)
                                for row in stocks}
                    # An identity change must not label our new Offer as a
                    # competitor or turn a newly missing probe into zero stock.
                    succeeded = succeeded and allowed == observed
                    observation = observation.model_copy(update={"product": filtered, "offer_stocks": stocks})
                elif spec["plid"] in own_contexts(session):
                    raise ValueError("Target became own-store; retain evidence without competitor inventory writes")
                repo = LateSafeRepository(session)
                previous = repo.latest_compatible_snapshot(observation.product)
                if previous and utc(previous.collected_at) >= observation.collected_at:
                    previous = None  # Never derive stock movement backwards from a future sample.
                signal = analyze_sales_signal(previous,
                    current_stock_quantity=observation.stock.quantity,
                    current_stock_exact=observation.stock.exact,
                    current_review_count=observation.product.review_count)
                snapshot = repo.save_observation(
                    product=observation.product, reviews=observation.reviews,
                    review_summary=summarize_reviews(observation.reviews), stock=observation.stock,
                    variant_stocks=observation.variant_stocks, offer_stocks=observation.offer_stocks,
                    lifetime_sales=estimate_lifetime_sales(observation.product.review_count), signal=signal,
                    collected_at=observation.collected_at, register_target=False)
                snapshot_id = snapshot.id
                health = session.get(CompetitorLinkHealth, spec["plid"])
                if health is None or health.last_checked_at is None or utc(health.last_checked_at) <= observation.collected_at:
                    repo.mark_link_healthy(plid=spec["plid"], url=spec["url"], checked_at=observation.collected_at)
            confirmed_invalid = False
            if value["error"].startswith("not-found|") and not observation:
                _, checked_text, control = value["error"].split("|", 2)
                checked_at = datetime.fromisoformat(checked_text)
                if checked_at.tzinfo is None or checked_at > now + timedelta(minutes=5):
                    raise ValueError("Invalid not-found evidence time")
                repo = LateSafeRepository(session)
                health = session.get(CompetitorLinkHealth, spec["plid"])
                if health is None or health.last_success_at is None or utc(health.last_success_at) < checked_at:
                    decision = repo.record_not_found(plid=spec["plid"], url=spec["url"],
                        checked_at=checked_at, control_plid=control or None,
                        control_check_ok=bool(control and re.fullmatch(r"[0-9]{1,30}", control)
                                              and control != spec["plid"]))
                    confirmed_invalid = decision.status == "confirmed_invalid"
            session.execute(insert(receipts).values(attempt_id=value["attempt_id"], task_key=task_key(spec),
                node=self.node, sha256=digest, succeeded=int(succeeded), snapshot_id=snapshot_id,
                body=body, received_at=now))
            # A late success joins the completed union even after a different lease.
            # Failures and late partial results never undo another worker's success.
            if succeeded and job.status != "cancelled":
                job.status, job.finished_at, job.updated_at = "succeeded", now, now
                job.title = observation.product.title
                job.message, job.failure_kind, job.retryable = "结果已可靠入库；同轮成功记录已合并", None, False
                job.lease_owner = job.lease_token = job.lease_expires_at = None
            elif confirmed_invalid and job.status not in {"succeeded", "cancelled"}:
                job.status, job.finished_at, job.updated_at = "terminal", now, now
                job.message, job.failure_kind, job.retryable = "链接已按既有404复核规则确认失效", "confirmed-invalid", False
                job.lease_owner = job.lease_token = job.lease_expires_at = None
            elif (not succeeded and job.status not in {"succeeded", "cancelled"}
                  and (job.status != "leased" or job.lease_token == value["lease_token"])):
                job.status, job.updated_at = "retry", now
                job.available_at = now + timedelta(seconds=min(1800, 60 * 2 ** min(job.attempt_count, 5)))
                job.message = "未完成，保留待重试：" + (value["error"] or "库存探测不完整")
                job.failure_kind, job.retryable = value["error"] or "stock-unprobed", True
                job.lease_owner = job.lease_token = job.lease_expires_at = None
        return {"attempt_id": value["attempt_id"], "sha256": digest, "succeeded": succeeded, "reused": False}
