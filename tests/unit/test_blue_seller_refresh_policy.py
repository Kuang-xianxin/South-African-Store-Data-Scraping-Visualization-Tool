from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ha"))
    return importlib.import_module("blue_web_router")


def sample(node, *, busy=False, free=10, release="b" * 64):
    return {
        "node": node, "frontend_sha256": release, "available_bytes": free,
        "total_bytes": 100, "available_commit_bytes": 100, "routing_version": 2,
        "files": {}, "workflows": {"refresh": {"busy": busy}},
    }


def router(module, clock, path=None, initial_owner=None):
    result = module.Router(
        "a" * 64, "b" * 64, clock=lambda: clock[0], wall=lambda: clock[0],
        state_path=path, initial_owner=initial_owner,
    )
    result.update("main", sample("main", free=1), 2)
    result.update("laptop", sample("laptop", free=80), 0)
    return result


def lose_main(result, clock):
    clock[0] += 13
    result.update("laptop", sample("laptop", free=80), 0)
    result.probe_failed("main")
    result.probe_failed("main")


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/erp/refresh-status"), ("POST", "/api/erp/refresh"),
    ("POST", "/api%2Ferp/refresh"), ("POST", "/api/erp/refresh?all=true"),
])
def test_main_wins_despite_laptop_resources_and_browser_affinity(module, method, path):
    result = router(module, [1000], initial_owner="laptop")
    _, cookie = result.choose("GET", "/api/erp/summary")
    assert "laptop." in cookie
    assert result.choose(method, path, cookie)[0] == "main"
    assert result.status()["seller_refresh_policy"] == "main-first-v1"


def test_general_pages_keep_resource_routing(module):
    result = router(module, [1000])
    assert result.choose("GET", "/api/erp/summary")[0] == "laptop"


def test_idle_refresh_fails_over_only_after_two_failed_probes_and_expiry(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    result.probe_failed("main")
    result.probe_failed("main")
    assert result.choose("GET", "/api/erp/refresh-status")[0] == "main"
    clock[0] += 13
    result.update("laptop", sample("laptop"), 0)
    result.probe_failures["main"] = 1
    assert result.choose("POST", "/api/erp/refresh")[0] is None
    result.probe_failed("main")
    assert result.choose("POST", "/api/erp/refresh")[0] == "laptop"


def test_pending_start_never_replays_on_another_exit_even_after_timeout(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    assert result.choose("POST", "/api/erp/refresh")[0] == "main"
    clock[0] += 200
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] is None
    assert result.ownership.owners["refresh"]["settled"] is False


def test_idle_inventory_before_reservation_expiry_does_not_acknowledge_start(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    result.choose("POST", "/api/erp/refresh")
    clock[0] += 119
    result.update("main", sample("main"), 0)
    assert result.ownership.owners["refresh"]["settled"] is False
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] is None


def test_fresh_idle_confirmation_allows_next_batch_to_fail_over(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    result.choose("POST", "/api/erp/refresh")
    clock[0] += 121
    result.update("main", sample("main"), 0)
    assert result.ownership.owners["refresh"]["settled"] is True
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] == "laptop"


def test_laptop_batch_stays_on_laptop_when_main_recovers_then_returns_after_idle(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] == "laptop"
    clock[0] += 121
    result.update("main", sample("main"), 0)
    result.update("laptop", sample("laptop", busy=True), 0)
    assert result.choose("GET", "/api/erp/refresh-status")[0] == "laptop"
    result.update("laptop", sample("laptop"), 0)
    assert result.choose("POST", "/api/erp/refresh")[0] == "main"


def test_busy_offline_owner_blocks_fallback(module):
    clock = [1000]
    result = router(module, clock, initial_owner="main")
    result.update("main", sample("main", busy=True), 0)
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] is None


def test_repeated_starts_extend_reservation_before_any_forward(module, tmp_path):
    clock = [1000]
    path = tmp_path / "ownership.json"
    result = router(module, clock, path, initial_owner="main")
    result.choose("POST", "/api/erp/refresh")
    clock[0] += 119
    result.update("main", sample("main"), 0)
    result.choose("POST", "/api/erp/refresh")
    assert json.loads(path.read_text())["owners"]["refresh"]["until"] == 1239
    assert json.loads(path.read_text())["owners"]["refresh"]["settled"] is False


@pytest.mark.parametrize("pending", [False, True])
def test_restart_preserves_idle_evidence_and_unconfirmed_owner(module, tmp_path, pending):
    clock = [1000]
    path = tmp_path / "ownership.json"
    result = router(module, clock, path, initial_owner="main")
    if pending:
        result.choose("POST", "/api/erp/refresh")
    restarted = module.Router(
        "a" * 64, "b" * 64, clock=lambda: clock[0], wall=lambda: clock[0], state_path=path,
    )
    lose_main(restarted, clock)
    expected = None if pending else "laptop"
    assert restarted.choose("POST", "/api/erp/refresh")[0] == expected


def test_legacy_ownership_needs_authenticated_idle_evidence(module, tmp_path):
    path = tmp_path / "ownership.json"
    path.write_text(json.dumps({"version": 2, "revision": 1,
                               "owners": {"refresh": {"node": "main", "until": 0}}}))
    clock = [1000]
    result = module.Router("a" * 64, "b" * 64, clock=lambda: clock[0], wall=lambda: clock[0], state_path=path)
    lose_main(result, clock)
    assert result.choose("POST", "/api/erp/refresh")[0] is None
    result.update("main", sample("main"), 0)
    assert result.choose("POST", "/api/erp/refresh")[0] == "main"


def test_release_mismatch_is_not_a_main_outage(module):
    result = router(module, [1000], initial_owner="main")
    result.update("laptop", sample("laptop", release="c" * 64), 0)
    assert result.choose("POST", "/api/erp/refresh")[0] is None


def test_conflicting_active_refreshes_fail_closed(module):
    result = router(module, [1000], initial_owner="main")
    for name in ("main", "laptop"):
        result.update(name, sample(name, busy=True), 0)
    assert result.choose("POST", "/api/erp/refresh")[0] is None


def test_failed_reservation_is_not_forwarded(module, monkeypatch):
    result = router(module, [1000], initial_owner="main")
    def fail():
        raise OSError("read-only state directory")
    monkeypatch.setattr(result.ownership, "save", fail)
    with pytest.raises(OSError, match="read-only state directory"):
        result.choose("POST", "/api/erp/refresh")
