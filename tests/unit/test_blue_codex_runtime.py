from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import sys

import pytest

from takealot_ops.search_ranking import codex_cli as cli
from takealot_ops.search_ranking import service, title_optimization


def load_runtime():
    path = Path(__file__).resolve().parents[2] / "scripts/ha/blue_codex_runtime.py"
    spec = importlib.util.spec_from_file_location("blue_codex_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def window(used, reset=2_000_000_000):
    return cli.CodexRateLimitWindow("codex", "primary", used, 10080, reset)


def test_two_nodes_share_budget_and_rollover_without_rebaselining(tmp_path, monkeypatch):
    runtime = load_runtime()
    original_guard, original_client = cli.CodexWeeklyQuotaGuard, cli.CodexAppServerClient
    for module in (cli, service, title_optimization):
        monkeypatch.setattr(module, "CodexWeeklyQuotaGuard", original_guard)
        monkeypatch.setattr(module, "CodexAppServerClient", original_client)
    lock = Lock()
    saved = {"state": original_guard(tmp_path / "seed.json").observe(window(31))}
    saved["state"].update({"budget_percent": 10, "ceiling_used_percent": 41,
                          "current_used_percent": 91, "status": "exhausted"})
    saved["state"].pop("system_budget_enforced")

    @contextmanager
    def transaction(*, write):
        with lock:
            def save(value):
                assert write
                saved["state"] = json.loads(json.dumps(value))
            yield saved["state"], save

    monkeypatch.setattr(runtime, "quota_transaction", transaction)
    runtime.install()
    main = cli.CodexWeeklyQuotaGuard(tmp_path / "main-never-created.json")
    laptop = cli.CodexWeeklyQuotaGuard(tmp_path / "laptop-never-created.json")
    assert main.observe(window(35))["baseline_used_percent"] == 31
    assert laptop.observe(window(40))["remaining_percentage_points"] == 60
    assert main.observe(window(91))["status"] == "active"
    assert laptop.status()["ceiling_used_percent"] == 100
    assert main.observe(window(100))["status"] == "exhausted"
    with ThreadPoolExecutor(2) as pool:
        states = list(pool.map(lambda guard: guard.observe(window(2, 2_000_604_800)), [main, laptop]))
    assert all(s["baseline_used_percent"] == 2 and s["ceiling_used_percent"] == 100 for s in states)
    assert not (tmp_path / "main-never-created.json").exists()
    assert not (tmp_path / "laptop-never-created.json").exists()


def test_child_environment_is_private_and_does_not_change_erp_environment(tmp_path, monkeypatch):
    runtime = load_runtime()
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "blue_node", SimpleNamespace(config=lambda: {"node": "laptop"}))
    monkeypatch.setenv("HTTP_PROXY", "http://existing-proxy.invalid:123")
    monkeypatch.setenv("ALL_PROXY", "socks5://existing-proxy.invalid:456")
    with pytest.raises(RuntimeError, match="login"):
        runtime.child_environment()
    home = tmp_path / "secrets/codex-home"
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    env = runtime.child_environment()
    assert env["CODEX_HOME"] == str(home)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "ALL_PROXY" not in env
    import os
    assert os.environ["HTTP_PROXY"] == "http://existing-proxy.invalid:123"


def test_shared_budget_database_failure_cannot_create_a_local_budget(tmp_path, monkeypatch):
    runtime = load_runtime()
    original_guard, original_client = cli.CodexWeeklyQuotaGuard, cli.CodexAppServerClient
    for module in (cli, service, title_optimization):
        monkeypatch.setattr(module, "CodexWeeklyQuotaGuard", original_guard)
        monkeypatch.setattr(module, "CodexAppServerClient", original_client)

    @contextmanager
    def unavailable(*, write):
        raise ConnectionError("primary offline")
        yield

    monkeypatch.setattr(runtime, "quota_transaction", unavailable)
    runtime.install()
    guard = cli.CodexWeeklyQuotaGuard(tmp_path / "local.json")
    with pytest.raises(ConnectionError):
        guard.observe(window(50))
    assert not (tmp_path / "local.json").exists()


def test_preloaded_title_review_uses_blue_identity_and_shared_budget(tmp_path, monkeypatch):
    runtime = load_runtime()
    original_guard, original_client = cli.CodexWeeklyQuotaGuard, cli.CodexAppServerClient
    for module in (cli, service, title_optimization):
        monkeypatch.setattr(module, "CodexWeeklyQuotaGuard", original_guard)
        monkeypatch.setattr(module, "CodexAppServerClient", original_client)
    expected_env = {"CODEX_HOME": str(tmp_path / "private-home")}
    monkeypatch.setattr(runtime, "child_environment", lambda: expected_env)

    @contextmanager
    def transaction(*, write):
        assert not write
        yield {"baseline_used_percent": 49, "ceiling_used_percent": 59}, None

    monkeypatch.setattr(runtime, "quota_transaction", transaction)
    runtime.install()
    guard = title_optimization.CodexWeeklyQuotaGuard(tmp_path / "never-created.json")
    client = title_optimization.CodexAppServerClient(
        tmp_path / "codex.exe", project_root=tmp_path, quota_guard=guard, timeout_seconds=60,
    )
    assert client.subprocess_env == expected_env
    assert guard.status()["ceiling_used_percent"] == 59
    assert title_optimization.CodexAppServerClient is service.CodexAppServerClient
    assert not (tmp_path / "never-created.json").exists()
