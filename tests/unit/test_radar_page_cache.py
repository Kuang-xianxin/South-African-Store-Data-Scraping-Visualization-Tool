"""Complete previews, isolated authorization and shared fresh reads."""
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Event

import pytest
from starlette.requests import Request

from takealot_ops.erp.radar_page_cache import RadarPageCache


def test_preview_returns_complete_old_read_and_fresh_reader_joins_one_job(tmp_path):
    cache = RadarPageCache(directory=tmp_path)
    started, finish = Event(), Event()
    calls = []
    original = {"items": [{"plid": "1", "销量": None, "报价": [{"id": "x", "qty": 0}]}]}
    try:
        old, refreshing = cache.get_or_load("all", version="a", boundary="user1/today", loader=lambda: original)
        assert not refreshing and json.loads(old.body) == original

        def refresh():
            calls.append(1)
            started.set()
            assert finish.wait(4)
            return {"items": [{"plid": "1", "销量": 2}]}

        preview, refreshing = cache.get_or_load("all", version="b", boundary="user1/today", loader=refresh, prefer_cached=True)
        assert refreshing and preview is old and started.wait(2)
        with ThreadPoolExecutor() as pool:
            waiting = pool.submit(cache.get_or_load, "all", version="b", boundary="user1/today", loader=refresh)
            finish.set()
            fresh, refreshing = waiting.result(timeout=4)
        assert not refreshing and json.loads(fresh.body)["items"][0]["销量"] == 2
        assert len(calls) == 1
    finally:
        finish.set()
        cache.close()


def test_auth_scope_date_and_code_boundaries_never_return_an_old_preview(tmp_path):
    cache = RadarPageCache(directory=tmp_path, namespace="code1")
    try:
        cache.get_or_load(("all", "store-a"), version="v", boundary="user1/day1", loader=lambda: {"secret": "a"})
        for key, boundary in [
            (("all", "store-b"), "user1/day1"),
            (("all", "store-a"), "user2/day1"),
            (("all", "store-a"), "user1/day2"),
        ]:
            entry, refreshing = cache.get_or_load(key, version="v", boundary=boundary, loader=lambda: {"secret": "b"}, prefer_cached=True)
            assert not refreshing and json.loads(entry.body) == {"secret": "b"}
    finally:
        cache.close()
    changed = RadarPageCache(directory=tmp_path, namespace="code2")
    try:
        entry, refreshing = changed.get_or_load(("all", "store-a"), version="v", boundary="user1/day1", loader=lambda: {"secret": "new"}, prefer_cached=True)
        assert not refreshing and json.loads(entry.body) == {"secret": "new"}
    finally:
        changed.close()


def test_restart_reuses_verified_serialized_result_and_honors_preview_age(tmp_path):
    now = [1000.0]
    options = dict(directory=tmp_path, clock=lambda: now[0], fresh_seconds=20, preview_seconds=60)
    cache = RadarPageCache(**options)
    entry, _ = cache.get_or_load("all", version="v", boundary="u", loader=lambda: {"items": [1]})
    cache.close()
    restored = RadarPageCache(**options)
    try:
        same, refreshing = restored.get_or_load("all", version="v", boundary="u", loader=lambda: pytest.fail("must reuse disk result"))
        assert same.body == entry.body and not refreshing
        now[0] += 61
        fresh, refreshing = restored.get_or_load("all", version="v2", boundary="u", loader=lambda: {"items": [2]}, prefer_cached=True)
        assert json.loads(fresh.body) == {"items": [2]} and not refreshing
    finally:
        restored.close()


def test_worker_preserves_store_context_and_failures_do_not_replace_good_results():
    store = ContextVar("test-store", default="none")
    cache = RadarPageCache()
    try:
        token = store.set("store-a")
        first, _ = cache.get_or_load("key", version="1", boundary="u", loader=lambda: {"store": store.get()})
        store.reset(token)
        assert json.loads(first.body) == {"store": "store-a"}
        def broken():
            raise RuntimeError("read failed")
        with pytest.raises(RuntimeError, match="read failed"):
            cache.get_or_load("key", version="2", boundary="u", loader=broken)
        old, _ = cache.get_or_load("key", version="1", boundary="u", loader=lambda: pytest.fail("good result was lost"))
        assert old is first
    finally:
        cache.close()


def test_response_is_lossless_private_conditional_and_not_double_compressed():
    cache = RadarPageCache()
    try:
        entry, _ = cache.get_or_load("k", version="v", boundary="u", loader=lambda: {"items": [{"销量": None, "价格": 0}]})
        request = Request({"type": "http", "headers": [(b"accept-encoding", b"gzip")]})
        response = entry.response(request, refreshing=True)
        assert gzip.decompress(response.body) == entry.body
        assert response.headers["cache-control"] == "private, no-cache"
        assert response.headers["x-erp-refreshing"] == "1"
        assert "Cookie" in response.headers["vary"] and "X-Store-Code" in response.headers["vary"]
        conditional = Request({"type": "http", "headers": [(b"if-none-match", entry.etag.encode())]})
        assert entry.response(conditional, refreshing=False).status_code == 304
        no_gzip = Request({"type": "http", "headers": [(b"accept-encoding", b"gzip;q=0")]})
        assert entry.response(no_gzip, refreshing=False).body == entry.body
    finally:
        cache.close()


def test_datetime_fields_and_repeated_app_lifespans_remain_compatible():
    cache = RadarPageCache()
    stamp = datetime(2026, 9, 7, 1, 2, tzinfo=UTC)
    try:
        first, _ = cache.get_or_load("key", version="1", boundary="u", loader=lambda: {"at": stamp})
        assert json.loads(first.body) == {"at": stamp.isoformat()}
        cache.close()
        second, _ = cache.get_or_load("key", version="2", boundary="u", loader=lambda: {"at": None})
        assert json.loads(second.body) == {"at": None}
    finally:
        cache.close()


def test_brotli_is_lossless_negotiated_and_survives_outer_gzip_middleware():
    brotli = pytest.importorskip("brotli")
    from fastapi import FastAPI
    from starlette.middleware.gzip import GZipMiddleware
    from starlette.testclient import TestClient

    cache = RadarPageCache()
    try:
        entry, _ = cache.get_or_load("k", version="v", boundary="u", loader=lambda: {"items": [{"销量": None, "价格": 0}] * 100})
        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=500)

        @app.get("/")
        def prepared(request: Request):
            return entry.response(request, refreshing=False)

        raw = entry.response(Request({"type": "http", "headers": [(b"accept-encoding", b"gzip, br")]}), refreshing=False)
        assert raw.headers["content-encoding"] == "br"
        assert brotli.decompress(raw.body) == entry.body
        with TestClient(app) as client:
            response = client.get("/", headers={"Accept-Encoding": "gzip, br"})
            assert response.headers["content-encoding"] == "br"
            assert response.content == entry.body
            unchanged = client.get("/", headers={"If-None-Match": response.headers["etag"]})
            assert unchanged.status_code == 304 and unchanged.content == b""
            fallback = client.get("/", headers={"Accept-Encoding": "gzip, br;q=0"})
            assert fallback.headers["content-encoding"] == "gzip" and fallback.content == entry.body
    finally:
        cache.close()


def test_cold_true_partition_does_not_wait_for_blocked_own_partition():
    cache = RadarPageCache()
    started, finish = Event(), Event()

    def own_read():
        started.set()
        assert finish.wait(5)
        return {"store_items": ["own"]}

    try:
        with ThreadPoolExecutor(max_workers=2) as clients:
            own = clients.submit(cache.get_or_load, "own", version="v", boundary="u", loader=own_read)
            assert started.wait(2)
            true = clients.submit(cache.get_or_load, "true", version="v", boundary="u", loader=lambda: {"items": ["true"]})
            try:
                page, refreshing = true.result(timeout=2)
                assert json.loads(page.body) == {"items": ["true"]}
                assert not refreshing and not own.done()
            finally:
                finish.set()
            assert json.loads(own.result(timeout=2)[0].body) == {"store_items": ["own"]}
    finally:
        finish.set()
        cache.close()


def test_many_crawl_revisions_coalesce_to_one_latest_follow_up_with_context():
    cache = RadarPageCache()
    started, finish = Event(), Event()
    calls = []
    context_value = ContextVar("radar-revision-test", default="unset")
    try:
        original, _ = cache.get_or_load("all", version="seed", boundary="u", loader=lambda: {"items": [0]})

        def blocked_read():
            calls.append("running")
            started.set()
            assert finish.wait(5)
            return {"items": [1]}

        cache.get_or_load("all", version="first", boundary="u", loader=blocked_read, prefer_cached=True)
        assert started.wait(2)
        for revision in range(2, 52):
            def latest_read(value=revision):
                calls.append(value)
                return {"items": [value], "context": context_value.get()}
            token = context_value.set(str(revision))
            try:
                preview, refreshing = cache.get_or_load("all", version=str(revision), boundary="u", loader=latest_read, prefer_cached=True)
            finally:
                context_value.reset(token)
            assert preview is original and refreshing
        with ThreadPoolExecutor() as clients:
            fresh = clients.submit(cache.get_or_load, "all", version="51", boundary="u", loader=latest_read)
            finish.set()
            page, refreshing = fresh.result(timeout=3)
        assert not refreshing and page.version == "51"
        assert json.loads(page.body) == {"items": [51], "context": "51"}
        assert calls == ["running", 51]
    finally:
        finish.set()
        cache.close()
