"""Short-lived local release lease and complete HTTP request draining."""
from __future__ import annotations

import time
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class ReleaseGate:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.active_requests = 0

    def held(self) -> bool:
        try:
            return 0 <= time.time() - self.path.stat().st_mtime < 15
        except FileNotFoundError:
            return False


class ReleaseDrainMiddleware:
    def __init__(self, app: ASGIApp, *, gate: ReleaseGate) -> None:
        self.app, self.gate = app, gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path in {
            "/api/health", "/api/internal/release-status", "/api/erp/daily-report/events",
        }:
            await self.app(scope, receive, send)
            return
        if self.gate.held():
            await JSONResponse({"detail": "服务正在切换，请稍后重试"}, status_code=503,
                               headers={"Retry-After": "3"})(scope, receive, send)
            return
        self.gate.active_requests += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.active_requests -= 1
