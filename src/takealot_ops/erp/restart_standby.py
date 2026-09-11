"""Prepared read-only bridge used only during the formal GREEN restart."""
from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from datetime import UTC, datetime
import hashlib

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from takealot_ops.erp.radar_code_version import materialized_code_fingerprint
from takealot_ops.erp.auth import AuthManager, SessionIdentity, _identity
from takealot_ops.storage.models import ErpSession, ErpUser
from takealot_ops.erp.release_gate import ReleaseDrainMiddleware, ReleaseGate
from takealot_ops.erp.release_cache import copy_radar_cache


class ReleaseReadAuthManager(AuthManager):
    """Read current production sessions without renewing or copying them."""
    def __init__(self, root: Path) -> None:
        super().__init__(root, read_only_test_mode=True)

    def resolve_session(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        now = datetime.now(UTC).replace(tzinfo=None)
        with Session(self._get_engine()) as session:
            record = session.get(ErpSession, hashlib.sha256(token.encode()).hexdigest())
            if record is None or record.expires_at <= now:
                return None
            user = session.get(ErpUser, record.user_id)
            if user is None or not user.active:
                return None
            return SessionIdentity(user=_identity(session, user), csrf_token=record.csrf_token,
                                   expires_at=record.expires_at, renewed=False)


def create_standby() -> FastAPI:
    if os.environ.get("TAKEALOT_WEB_ONLY") != "1" or os.environ.get("TAKEALOT_READ_ONLY_TEST_MODE") != "1":
        raise RuntimeError("Restart standby requires WEB_ONLY and read-only mode")
    from takealot_ops.erp.web import create_app

    root = Path(os.environ["TAKEALOT_PROJECT_ROOT"])
    ready_path = Path(os.environ["TAKEALOT_RELEASE_READY"])
    namespace = materialized_code_fingerprint(root)
    prepared_directory = ready_path.parent / "radar-cache"
    copy_radar_cache(root / "data" / "runtime-cache", prepared_directory, namespace)
    app = create_app(root, auth_manager=ReleaseReadAuthManager(root), radar_directory=prepared_directory)
    reader_gate = ReleaseGate(ready_path.with_suffix(".reader-lease"))
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(application):
            namespace = materialized_code_fingerprint(root)
            started = time.time()
            results = await run_in_threadpool(app.state.prepare_radar_release)
            if namespace != materialized_code_fingerprint(root):
                raise RuntimeError("Source changed during preparation; keep the old service")
            report = {"namespace": namespace, "prepared_at": time.time(), "pid": os.getpid(),
                      "cache_directory": str(prepared_directory),
                      "seconds": time.time() - started, "scopes": results}
            temporary = ready_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(ready_path)
            yield

    @app.middleware("http")
    async def bridge_reads_only(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path == "/api/health" and request.method in {"GET", "HEAD"}:
            return JSONResponse({"status": "ok", "application": "takealot-erp",
                                 "mode": "release-read-bridge", "deployment": "green",
                                 "active_requests": reader_gate.active_requests},
                                headers={"X-ERP-Release-Bridge": "1"})
        # Some status handlers manage in-process jobs. Only known read routes
        # belong on a temporary instance without those job owners.
        allowed = request.url.path in {"/", "/index.html", "/api/auth/status", "/api/auth/session",
                                       "/api/competitors", "/api/competitors/own-store", "/api/competitors/date-range"}
        allowed = allowed or request.url.path.startswith("/assets/")
        if request.method not in {"GET", "HEAD"} or not allowed:
            return JSONResponse({"detail": "服务正在切换，请稍后重试"}, status_code=503,
                                headers={"Retry-After": "3"})
        response = await call_next(request)
        response.headers["X-ERP-Release-Bridge"] = "1"
        return response

    app.router.lifespan_context = lifespan
    app.add_middleware(ReleaseDrainMiddleware, gate=reader_gate)
    return app
