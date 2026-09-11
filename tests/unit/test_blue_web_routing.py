from __future__ import annotations
import asyncio
import importlib
from pathlib import Path
import pytest

@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ha"))
    return tuple(importlib.import_module(name) for name in
                 ("blue_web_router", "blue_read_replica", "blue_web_capacity"))

def sample(node, free=20, release="a" * 64):
    return {"node": node, "frontend_sha256": release, "available_bytes": free,
            "total_bytes": 100, "available_commit_bytes": 80, "active_requests": 0,
            "routing_version": 2, "files": {}, "workflows": {name: {'busy': False, 'present': False, 'updated': 0}
                for name in importlib.import_module('blue_web_routes').FAMILIES}}

def test_memory_selection_hold_then_move_and_stale_failover(modules):
    module, _, _ = modules
    clock = [1000.0]
    router = module.Router("x" * 64, "a" * 64, clock=lambda: clock[0], wall=lambda: clock[0])
    router.update("main", sample("main", 5), .1)
    router.update("laptop", sample("laptop", 25), .1)
    node, cookie = router.choose("GET", "/api/competitors?page=2")
    assert node == "laptop"
    router.update("main", sample("main", 40), .1)
    router.update("laptop", sample("laptop", 5), .1)
    assert router.choose("GET", "/api/competitors", cookie)[0] == "laptop"
    clock[0] += 61
    router.update("main", sample("main", 40), .1)
    router.update("laptop", sample("laptop", 5), .1)
    assert router.choose("GET", "/api/competitors", cookie)[0] == "main"
    clock[0] += 13
    assert router.choose("GET", "/api/competitors")[0] is None
    router.update("main", sample("main", 40), .1)
    assert router.choose("GET", "/api/competitors", cookie)[0] == "main"

@pytest.mark.parametrize("method,path", [("POST", "/api/competitors/collect"),
    ("GET", "/api/erp/search-ranking/batch/status"), ("GET", "/api/erp/exports/download"),
    ("GET", "/api/competitors/batch-status")])
def test_stateful_operations_choose_an_owner_and_do_not_take_over_it(modules, method, path):
    module, _, _ = modules
    router = module.Router("x" * 64, "a" * 64)
    router.update("main", sample("main", 90), 0)
    assert router.choose(method, path)[0] is None
    router.update("laptop", sample("laptop", 5), 0)
    assert router.choose(method, path)[0] == "main"
    router.update('main', sample('main', 1), 0)
    router.update('laptop', sample('laptop', 90), 0)
    assert router.choose(method, path)[0] == 'main'

def test_mismatched_releases_and_forged_affinity_are_not_accepted(modules):
    module, _, _ = modules
    router = module.Router("x" * 64, "a" * 64)
    router.update("main", sample("main", 80), 0)
    router.update("laptop", sample("laptop", 20, "b" * 64), 0)
    assert router.choose("GET", "/api/erp/summary")[0] == "laptop"
    assert router.cookie_node("takealot_blue_web_route=main.123.fake") is None
    with pytest.raises(ValueError):
        router.update("main", sample("laptop"), 0)

class Connection:
    def __init__(self, subset=1):
        self.subset, self.closed, self.readonly = subset, False, False
        self.queries = []
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def execute(self, query, args=None):
        self.queries.append((query, args))
        if query == "SET SESSION TRANSACTION READ ONLY":
            self.readonly = True
    def fetchone(self):
        return (self.subset,) if "GTID_SUBSET" in self.queries[-1][0] else ("uuid:1-42",)
    def rollback(self):
        pass
    def select_db(self, name):
        assert name == "takealot_ops"
    def autocommit(self, flag):
        assert flag is False
    def close(self):
        self.closed = True

@pytest.mark.parametrize("subset", [0, 1])
def test_replica_must_cover_fresh_primary_boundary_and_fallback_is_readonly(modules, subset):
    _, module, _ = modules
    source, replica = Connection(), Connection(subset)
    result = module.choose_connection(lambda: source, lambda: replica, lambda _: None)
    assert result is (replica if subset else source)
    assert result.readonly and not result.closed
    assert (source if subset else replica).closed
    assert ("SELECT GTID_SUBSET(%s, @@GLOBAL.gtid_executed)", ("uuid:1-42",)) in replica.queries

def test_invalid_replica_falls_back_but_primary_failure_does_not_serve_backup(modules):
    _, module, _ = modules
    source, replica = Connection(), Connection()
    def fail(*_):
        raise RuntimeError("unverified")
    result = module.choose_connection(lambda: source, lambda: replica, fail)
    assert result is source and source.readonly and replica.closed
    with pytest.raises(RuntimeError):
        module.choose_connection(fail, lambda: replica, lambda _: None)

def test_capacity_requires_key_and_only_trusted_ingress_can_assert_https(modules, tmp_path):
    _, _, module = modules
    seen = []
    async def app(scope, receive, send):
        seen.append(scope["scheme"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    middleware = module.CapacityMiddleware(app, root=tmp_path, node="main", secret="x" * 64)
    async def run(path, headers):
        messages = []
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "method": "GET", "path": path, "scheme": "http",
                          "headers": headers}, None, send)
        return messages
    assert asyncio.run(run("/__blue/capacity", []))[0]["status"] == 404
    asyncio.run(run("/", [(b"x-forwarded-proto", b"https")]))
    asyncio.run(run("/", [(b"x-forwarded-proto", b"https"), (b"x-blue-routing-key", b"x" * 64)]))
    assert seen == ["http", "https"]
    assert middleware.active == 0
