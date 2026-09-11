"""Authenticated BLUE-only daily shared crawler and small acceptance panel.

No new database, schema migration, background scheduler or promotion is installed.
The existing workers consume the existing BLUE job table independently of HTTP.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.orm import Session

from blue_full_crawl import ACTIVE_JOB_STATES, FULL_WORKER_VERSION, full_targets, own_contexts

from takealot_ops.competitors.own_store import connected_store_plids
from takealot_ops.erp.permissions import COMPETITORS_COLLECT, COMPETITORS_VIEW
from takealot_ops.storage.models import CompetitorCollectionJob, CompetitorTarget, CompetitorWorkerHeartbeat


class StartRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    plids: list[str] = Field(min_length=1, max_length=20)


class StopRequest(BaseModel):
    batch_id: str = Field(pattern=r"^blue-(?:web|full)-[a-f0-9]{32}$")


class FullStartRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def normalized_plids(values: list[str]) -> list[str]:
    if not 1 <= len(values) <= 20:
        raise ValueError("每次只允许测试1至20条已监控竞品")
    if any(not re.fullmatch(r"[0-9]{1,30}", value) for value in values):
        raise ValueError("请输入纯数字PLID，每行一条")
    return list(dict.fromkeys(values))


@contextmanager
def dispatch_lock(conn):
    """Serialize dispatch on the one BLUE writer, not a process-local mutex."""
    if conn.dialect.name != "mysql":
        raise RuntimeError("Shared BLUE dispatch requires MySQL")
    acquired = conn.execute(text("SELECT GET_LOCK('blue-small-crawl-dispatch', 3)")).scalar()
    if acquired != 1:
        raise HTTPException(409, "另一端正在提交任务，请用同一请求编号重试")
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(text("SELECT RELEASE_LOCK('blue-small-crawl-dispatch')"))
        conn.commit()


class BlueSharedCrawl:
    def __init__(self, engine, *, durable: bool = False):
        self.engine = engine
        self.durable = durable

    def require_full_workers(self) -> None:
        with self.engine.connect() as conn:
            versions = dict(conn.execute(select(CompetitorWorkerHeartbeat.worker_id,
                CompetitorWorkerHeartbeat.last_error).where(
                    CompetitorWorkerHeartbeat.worker_id.in_(("blue-main", "blue-laptop")))).all())
        if not self.durable or not all(FULL_WORKER_VERSION in str(versions.get(node) or "").split()
                                      for node in ("blue-main", "blue-laptop")):
            raise HTTPException(409, "两台蓝版爬虫尚未全部加载全量版本，请等待同步完成")

    def preview_full(self, store_codes: set[str]) -> dict:
        with Session(self.engine) as session:
            targets = full_targets(session, store_codes=store_codes)
        own = sum(row["followers_only"] for row in targets)
        return {"total": len(targets), "competitors": len(targets) - own, "own": own}

    def start_full(self, payload: FullStartRequest, store_codes: set[str]) -> dict:
        self.require_full_workers()
        batch_id = "blue-full-" + payload.request_id
        jobs = CompetitorCollectionJob.__table__
        with self.engine.connect() as conn, dispatch_lock(conn), conn.begin():
            existing = conn.scalar(select(func.count()).select_from(jobs).where(jobs.c.batch_id == batch_id))
            if existing:
                return {"batch_id": batch_id, "total": existing, "reused": True}
            if conn.execute(select(jobs.c.id).where(jobs.c.status.in_(ACTIVE_JOB_STATES)).limit(1)).first():
                raise HTTPException(409, "已有双机批次未完成，请查看或继续原批次")
            with Session(bind=conn) as session:
                targets = full_targets(session, store_codes=store_codes)
            if not targets:
                raise HTTPException(422, "当前没有可采集的真正竞品或有权店铺自有链接")
            now = datetime.now(UTC)
            rows = [dict(batch_id=batch_id, item_index=index, **target, with_stock_probe=True,
                status="pending", priority=0, attempt_count=0, max_attempts=2147483647,
                available_at=now, created_at=now, updated_at=now) for index, target in enumerate(targets)]
            for offset in range(0, len(rows), 500):
                conn.execute(insert(jobs), rows[offset:offset + 500])
        return {"batch_id": batch_id, "total": len(rows), "reused": False}

    def resume(self, batch_id: str, store_codes: set[str] | None = None) -> dict:
        if batch_id.startswith("blue-full-"):
            self.require_full_workers()
        from blue_crawl_delivery import receipts
        from blue_crawl_journal import task_key
        jobs = CompetitorCollectionJob.__table__
        with self.engine.connect() as conn, dispatch_lock(conn), conn.begin():
            if conn.execute(select(jobs.c.id).where(jobs.c.batch_id != batch_id,
                jobs.c.status.in_(ACTIVE_JOB_STATES)).limit(1)).first():
                raise HTTPException(409, "另一双机批次仍在运行，请先处理当前批次")
            # Serialize receipt-union inspection with submit(), which locks the
            # same job before committing a late receipt and its observation.
            rows = conn.execute(select(jobs).where(jobs.c.batch_id == batch_id)
                                .order_by(jobs.c.id).with_for_update()).mappings().all()
            if not rows:
                raise HTTPException(404, "没有找到该双机批次")
            if store_codes is not None:
                with Session(bind=conn) as session:
                    allowed = own_contexts(session, store_codes=store_codes)
                if any(row["followers_only"] and row["status"] == "cancelled"
                       and row["plid"] not in allowed for row in rows):
                    raise HTTPException(409, "原批次部分自有链接已不在当前有权店铺范围，请重新核对全量清单")
            now = datetime.now(UTC)
            pending_rows = [row for row in rows if row["status"] == "cancelled"]
            completed_keys: set[str] = set()
            keys = {row["id"]: task_key(dict(job_id=row["id"], **{
                key: row[key] for key in ("batch_id", "plid", "url", "followers_only", "with_stock_probe")
            })) for row in pending_rows}
            if self.durable:
                key_values = list(keys.values())
                for offset in range(0, len(key_values), 500):
                    completed_keys.update(conn.scalars(select(receipts.c.task_key).where(
                        receipts.c.succeeded == 1, receipts.c.task_key.in_(key_values[offset:offset + 500]))))
            resumed = 0
            for row in pending_rows:
                succeeded = keys[row["id"]] in completed_keys
                resumed += not succeeded
                conn.execute(update(jobs).where(jobs.c.id == row["id"], jobs.c.status == "cancelled").values(
                    status="succeeded" if succeeded else "pending", available_at=now, updated_at=now,
                    finished_at=now if succeeded else None, lease_owner=None, lease_token=None, lease_expires_at=None,
                    message="已补交成功，保留原结果" if succeeded else "继续原批次未完成链接"))
        return {"batch_id": batch_id, "resumed": resumed, "message": "已继续原批次，保留成功和确认失效结果"}

    def start(self, payload: StartRequest) -> dict:
        if self.durable:
            # Deployment gate, not a liveness quorum: after both workers have loaded
            # v1, a temporarily offline peer must not prevent single-node collection.
            with self.engine.connect() as conn:
                deployed = dict(conn.execute(select(
                    CompetitorWorkerHeartbeat.worker_id, CompetitorWorkerHeartbeat.last_error,
                ).where(CompetitorWorkerHeartbeat.worker_id.in_(("blue-main", "blue-laptop")))).all())
            if not all(str(deployed.get(node) or "").startswith("durable-v1 ")
                       for node in ("blue-main", "blue-laptop")):
                raise HTTPException(409, "两台蓝版worker尚未全部加载暂存补交版本，请先完成蓝版进程重载；原页面手动采集不受影响")
        plids = normalized_plids(payload.plids)
        batch_id = "blue-web-" + payload.request_id
        with self.engine.connect() as conn, dispatch_lock(conn):
            with conn.begin():
                return self._start_locked(conn, batch_id, plids)

    def _start_locked(self, conn, batch_id: str, plids: list[str]) -> dict:
        jobs = CompetitorCollectionJob.__table__
        existing = list(conn.scalars(select(jobs.c.plid).where(
            jobs.c.batch_id == batch_id).order_by(jobs.c.item_index)))
        if existing:
            if existing != plids:
                raise HTTPException(409, "同一请求编号不能修改商品清单")
            return {"batch_id": batch_id, "total": len(existing), "reused": True}
        if conn.execute(select(jobs.c.id).where(
            jobs.c.status.in_(("pending", "retry", "leased"))).limit(1)).first():
            raise HTTPException(409, "蓝版还有未完成任务，请先查看或停止原批次")
        targets = CompetitorTarget.__table__
        found = dict(conn.execute(select(targets.c.plid, targets.c.url).where(
            targets.c.plid.in_(plids), targets.c.active.is_(True))).all())
        if set(found) != set(plids):
            raise HTTPException(422, "仅支持蓝版已启用的监控链接，请先在蓝版添加商品")
        with Session(bind=conn) as session:
            own = connected_store_plids(session)
        # This first acceptance surface deliberately excludes own-store/permission
        # scope expansion and does not silently turn a test into an all-store crawl.
        if set(plids) & own:
            raise HTTPException(422, "此小批量入口暂只测试真正竞品，不采集自有店铺链接")
        now = datetime.now(UTC)
        conn.execute(insert(jobs), [dict(
            batch_id=batch_id, item_index=index, plid=plid, url=found[plid],
            followers_only=False, with_stock_probe=True, status="pending", priority=0,
            attempt_count=0, max_attempts=2147483647 if self.durable else 1,
            available_at=now, created_at=now, updated_at=now,
        ) for index, plid in enumerate(plids)])
        return {"batch_id": batch_id, "total": len(plids), "reused": False}

    def stop(self, batch_id: str) -> dict:
        # Drain in-flight work instead of pretending that a SQL status update has
        # instantly closed a browser on another PC. Claim and cancel race via CAS.
        with self.engine.begin() as conn:
            jobs = CompetitorCollectionJob.__table__
            result = conn.execute(update(jobs).where(
                jobs.c.batch_id == batch_id,
                jobs.c.status.in_(("pending", "retry", "leased") if self.durable else ("pending", "retry")),
            ).values(status="cancelled", finished_at=datetime.now(UTC),
                     updated_at=datetime.now(UTC), message="用户停止：未领取任务已取消"))
        return {"batch_id": batch_id, "cancelled_pending": result.rowcount,
                "message": ("未领取任务已取消；已领取商品继续完成。断线节点收到取消前，最多仍会按30分钟内缓存继续采集；已采结果保留"
                            if self.durable else "未领取任务已取消；已经领取的当前商品继续完成，不会撤销已采集数据")}

    def status(self) -> dict:
        with self.engine.connect() as conn:
            workers = conn.execute(text(
                "SELECT worker_id,node_name,egress_label,state,current_plid,last_seen_at,last_error,"
                "(last_seen_at >= UTC_TIMESTAMP() - INTERVAL 45 SECOND) AS online "
                "FROM competitor_worker_heartbeats WHERE worker_id IN ('blue-main','blue-laptop')"
            )).mappings().all()
            batches = conn.execute(text(
                "SELECT batch_id,COUNT(*) total,SUM(status='pending') pending,"
                "SUM(status='leased') running,SUM(status='retry') retry,"
                "SUM(status='succeeded') succeeded,SUM(status='terminal') failed,"
                "SUM(status='cancelled') cancelled,MAX(updated_at) updated_at "
                "FROM competitor_collection_jobs GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT 10"
            )).mappings().all()
            rows = conn.execute(text(
                "SELECT batch_id,plid,status,lease_owner,attempt_count,title,message,followers_only "
                "FROM competitor_collection_jobs ORDER BY updated_at DESC,id DESC LIMIT 100"
            )).mappings().all()
        return {"workers": [dict(row) for row in workers],
                "batches": [dict(row) for row in batches], "items": [dict(row) for row in rows],
                "max_targets": 20, "automatic_failover": False, "writer": "main-blue-3307",
                "durable_delivery": self.durable,
                "durable_cluster_ready": self.durable and len(workers) == 2 and all(
                    str(row["last_error"] or "").startswith("durable-v1 ") for row in workers),
                "offline_cache_seconds": 1800 if self.durable else 0,
                "full_crawl": self.durable and len(workers) == 2 and all(
                    FULL_WORKER_VERSION in str(row["last_error"] or "").split() for row in workers)}


def require_operator(request: Request, *, write: bool = False) -> None:
    user = getattr(request.state, "erp_user", None)
    if user is None:
        raise HTTPException(401, "请先在同一蓝版地址登录")
    permission = COMPETITORS_COLLECT if write else COMPETITORS_VIEW
    if user.username.casefold() != "kxx" or not user.can(permission):
        raise HTTPException(403, "双机采集仅限具备相应权限的kxx账号")


def install(app, root: Path, engine) -> None:
    if not getattr(app.state, "session_cookie_name", "") == "takealot_blue_stage_session":
        raise RuntimeError("Refusing shared crawler routes outside BLUE")
    from blue_crawl_journal import enabled

    controller = BlueSharedCrawl(engine, durable=enabled(root))
    app.state.blue_shared_crawl = controller
    old_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with old_lifespan(application) as state:
                yield state
        finally:
            engine.dispose()

    app.router.lifespan_context = lifespan
    # Keep only automatic 09:00 startup disabled. Manual routes below remain
    # the original BLUE application's routes, with its own local batch journal.
    runner = app.state.scheduled_competitor_runner
    runner.start = lambda: None

    # Put specific routes ahead of the existing SPA StaticFiles mount.
    previous_routes = list(app.router.routes)
    app.router.routes.clear()
    local_status = next((route.endpoint for route in previous_routes
                         if getattr(route, "path", "") == "/api/competitors/batch-status"), None)

    def publish_local_state() -> bool:
        if local_status is None:
            return False
        value = local_status(include_details=False, result_page=1, error_page=1,
                             terminal_error_page=1, page_size=50)
        active = bool(value.get("active"))
        path = root / "state/ordinary-crawl-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"active": active, "batch_id": value.get("batch_id"),
            "observed_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
        os.replace(temporary, path)
        return active

    @app.middleware("http")
    async def separate_ordinary_and_distributed(request: Request, call_next):
        legacy_paths = {"/api/competitors/collect", "/api/competitors/batch-events",
                        "/api/competitors/batch-resume", "/api/competitors/batch-status",
                        "/api/competitors/batch-stop"}
        path = request.url.path
        starts_manual = path in {"/api/competitors/collect", "/api/competitors/batch-resume"}
        if path == "/api/competitors/batch-events" and request.method == "POST":
            try:
                starts_manual = (await request.json()).get("event") in {"start", "resume"}
            except (ValueError, AttributeError):
                pass  # Preserve the original endpoint's schema error.
        if starts_manual and request.method == "POST":
            def shared_active():
                with engine.connect() as conn:
                    return conn.execute(select(CompetitorCollectionJob.id).where(
                        CompetitorCollectionJob.batch_id.like("blue-full-%"),
                        CompetitorCollectionJob.status.in_(ACTIVE_JOB_STATES)).limit(1)).first() is not None
            if await run_in_threadpool(shared_active):
                return JSONResponse(status_code=409, content={
                    "detail": "双机队列正在运行，请使用双机进度面板；原单机断点保留"})
        response = await call_next(request)
        if path in legacy_paths and response.status_code < 400:
            await run_in_threadpool(publish_local_state)
        return response

    @app.get("/blue-crawl")
    def page():
        return FileResponse(root / "blue_shared_crawl.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/competitors/distributed/status")
    def status(request: Request):
        require_operator(request)
        publish_local_state()
        return controller.status()

    @app.post("/api/competitors/distributed/start")
    def start(request: Request, payload: StartRequest):
        require_operator(request, write=True)
        try:
            return controller.start(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/competitors/distributed/stop")
    def stop(request: Request, payload: StopRequest):
        require_operator(request, write=True)
        return controller.stop(payload.batch_id)

    def authorized_store_codes(request: Request) -> set[str]:
        return {store.code for store in getattr(request.state.erp_user, "accessible_stores", ())
                if getattr(store, "data_connected", False)}

    @app.get("/api/competitors/distributed/preview")
    def full_preview(request: Request):
        require_operator(request)
        return controller.preview_full(authorized_store_codes(request))

    @app.post("/api/competitors/distributed/start-full")
    def start_full(request: Request, payload: FullStartRequest):
        require_operator(request, write=True)
        if publish_local_state():
            raise HTTPException(409, "本机原单机批次仍在运行，请先停止；原断点会保留")
        return controller.start_full(payload, authorized_store_codes(request))

    @app.post("/api/competitors/distributed/resume")
    def resume(request: Request, payload: StopRequest):
        require_operator(request, write=True)
        if publish_local_state():
            raise HTTPException(409, "本机原单机批次仍在运行，请先停止；原断点会保留")
        return controller.resume(payload.batch_id, authorized_store_codes(request))

    def scheduled_disabled(request: Request):
        # The old loopback trigger is a public middleware exemption. Reject it
        # unconditionally; do not require an identity that middleware never sets.
        raise HTTPException(409, "蓝版09:00自动采集已停用；请在竞品雷达启动双机采集")

    app.add_api_route("/api/internal/competitors/scheduled-trigger", scheduled_disabled, methods=["POST"])

    app.router.routes.extend(previous_routes)
