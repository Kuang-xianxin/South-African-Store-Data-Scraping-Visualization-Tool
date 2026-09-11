"""BLUE web-only capacity and trusted ingress; no crawler resource policy."""
from __future__ import annotations

import ctypes
import asyncio
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import threading
from urllib.request import ProxyHandler, Request, build_opener

from blue_web_routes import policy


def memory_snapshot() -> dict:
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in ("total", "available", "commit_total",
                "commit_available", "virtual_total", "virtual_available", "extended")]
    value = Memory()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise OSError("Windows capacity unavailable")
    return {"total_bytes": value.total, "available_bytes": value.available,
            "available_commit_bytes": value.commit_available, "memory_used_percent": value.load}


class CapacityMiddleware:
    def __init__(self, app, *, root: Path, node: str, secret: str, memory=memory_snapshot, inventory=None):
        self.app, self.root, self.node, self.secret, self.memory = app, root, node, secret, memory
        self.active = 0
        self.lock = threading.Lock()
        self.inventory = inventory

    def push(self):
        payload = {'node': self.node, **self.inventory.snapshot()}
        request = Request('http://100.72.100.10:18504/snapshot', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json', 'X-Blue-Routing-Key': self.secret})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
                response.read(1024)
        except Exception:
            pass  # Next heartbeat publishes the committed metadata. Never retry the business request.

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value.decode("latin1") for key, value in scope["headers"]}
        trusted = hmac.compare_digest(headers.get(b"x-blue-routing-key", ""), self.secret)
        if scope["path"].startswith("/__blue/"):
            if not trusted or scope["method"] != "GET":
                status, payload = 404, {"detail": "Not Found"}
            elif scope["path"] == "/__blue/capacity":
                from blue_read_replica import read_status
                with self.lock:
                    active = self.active
                index = self.root / "app/frontend/competitor/dist/index.html"
                status, payload = 200, {"node": self.node, **self.memory(), "active_requests": active,
                    "frontend_sha256": hashlib.sha256(index.read_bytes()).hexdigest(), "reads": read_status()}
                if self.inventory:
                    payload.update(self.inventory.snapshot())
            else:
                status, payload = 404, {"detail": "Not Found"}
            body = json.dumps(payload).encode()
            await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": body})
        if trusted and headers.get(b"x-forwarded-proto") == "https":
            scope = dict(scope)
            scope["scheme"] = "https"
            try:
                address = str(ipaddress.ip_address(headers.get(b"x-real-ip", "")))
                scope["client"] = (address, 0)
            except ValueError:
                pass
        family, new = policy(scope['method'], scope['path'])
        mutated = scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        if self.inventory:
            # Public ingress coordinates local controllers. Direct LAN writes to
            # those controllers cannot silently create a second owner.
            if family and family != 'login' and mutated and not trusted:
                await send({'type': 'http.response.start', 'status': 409,
                            'headers': [(b'content-type', b'application/json')]})
                return await send({'type': 'http.response.body', 'body': json.dumps({
                    'detail': '请通过蓝版公网入口操作此任务，以保持双机任务归属一致'
                }, ensure_ascii=False).encode()})
            if trusted:
                self.inventory.invalidate(headers.get(b'x-blue-revision'))
            self.inventory.enter(family)
        with self.lock:
            self.active += 1
        released = False
        async def labeled(message):
            nonlocal released
            if message["type"] == "http.response.start":
                if self.inventory and (mutated or new):
                    self.inventory.leave(family, mutated and message['status'] < 400)
                    released = True
                    await asyncio.to_thread(self.push)
                message = {**message, "headers": [*message.get("headers", []),
                    (b"x-blue-web-node", self.node.encode())]}
            await send(message)
        try:
            await self.app(scope, receive, labeled)
        finally:
            if self.inventory and not released:
                self.inventory.leave(family, mutated)
                if mutated or new:
                    await asyncio.to_thread(self.push)
            with self.lock:
                self.active -= 1


def install(app, root: Path, node: str) -> None:
    secret = (root / "secrets/web-routing.key").read_text().strip()
    if len(secret) != 64 or any(c not in "0123456789abcdef" for c in secret):
        raise RuntimeError("Invalid BLUE routing key")
    from blue_web_inventory import Inventory
    inventory = Inventory(app, root)
    # Fail deployment preflight if a business release changed the controller contract.
    inventory.snapshot()
    app.add_middleware(CapacityMiddleware, root=root, node=node, secret=secret, inventory=inventory)
