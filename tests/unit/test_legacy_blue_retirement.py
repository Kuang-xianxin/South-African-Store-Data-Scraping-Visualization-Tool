from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ha"))
    loaded = []
    for name in ("legacy_blue_preflight", "blue_web", "run_laptop_blue_web"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ha/{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    yield loaded
    # The helper module was imported via a temporary sys.path entry.
    sys.modules.pop("blue_node", None)


def test_legacy_guard_accepts_only_readonly_laptop_3306(modules):
    legacy, _, _ = modules
    laptop = "LAPTOP-2T5MN8EU"
    row = (laptop, 2, 3306, "D:\\TakealotMySQLReplica\\data\\", 1, 1)
    legacy.validate_legacy_identity(row, laptop)
    for changed in [
        ("DESKTOP-NTRMANG", 1, 3306, "C:/ProgramData/MySQL/Data", 0, 0),
        (laptop, 102, 3307, "D:/TakealotBlue/mysql/data", 1, 1),
        (laptop, 2, 3306, "D:/TakealotMySQLReplica/data-other", 1, 1),
        (*row[:4], 0, 0),
    ]:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            legacy.validate_legacy_identity(changed, laptop)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        legacy.validate_legacy_identity(row, "DESKTOP-NTRMANG")


def test_main_refused_before_any_database_connection(modules, monkeypatch):
    legacy, _, _ = modules
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-NTRMANG")
    monkeypatch.setattr(legacy.blue_node, "connection", lambda: pytest.fail("No main connection"))
    with pytest.raises(RuntimeError, match="laptop-only"):
        legacy.audit()


def test_retired_runner_refuses_before_loading_database_settings(modules, tmp_path, monkeypatch):
    _, _, old_web = modules
    marker = tmp_path / "retired.json"
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(old_web, "RETIREMENT_MARKER", marker)
    monkeypatch.setattr(old_web.DashboardSettings, "from_env", lambda *_: pytest.fail("No settings read"))
    with pytest.raises(old_web.BluePreflightError, match="retired"):
        old_web._prepare_blue_environment()


def test_new_web_trusts_https_only_from_tencent_not_alias_loopback(modules):
    _, web, _ = modules
    assert web.proxy_options("laptop") == {
        "proxy_headers": True, "forwarded_allow_ips": "100.72.100.10",
    }
    assert web.proxy_options("main") == {"proxy_headers": False}


@pytest.mark.parametrize("peer,secure", [
    ("100.72.100.10", True), ("127.0.0.1", False), ("192.168.110.20", False),
])
def test_https_cookie_cannot_be_asserted_by_untrusted_alias_clients(modules, peer, secure):
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from takealot_ops.erp.web import _set_session_cookie

    _, web, _ = modules

    async def login(request):
        response = Response("fixture")
        _set_session_cookie(response, request, "fixture-not-a-real-session",
                            cookie_name="takealot_blue_stage_session")
        return response

    app = ProxyHeadersMiddleware(Starlette(routes=[Route("/", login)]),
                                 trusted_hosts=web.proxy_options("laptop")["forwarded_allow_ips"])
    with TestClient(app, client=(peer, 50000)) as client:
        response = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert ("; Secure" in response.headers["set-cookie"]) == secure


def test_retirement_script_preserves_shared_runtime_and_requires_preflight():
    script = (ROOT / "scripts/ha/retire_laptop_legacy_blue.ps1").read_text(encoding="utf-8")
    assert "if ($env:COMPUTERNAME -cne 'LAPTOP-2T5MN8EU')" in script
    assert "param([switch]$Apply)" in script
    assert "$ArchiveNames = @('data', 'binlog', 'relay', 'logs', 'my.ini')" in script
    assert "Move-Item -LiteralPath $Source -Destination $Destination" in script
    assert "Assert-PhysicalPath $Source" in script
    assert "ReparsePoint" in script
    assert script.index('if (-not $Apply)') < script.index('Disable-ScheduledTask')
    assert script.index('if (-not $AliasHealthy)') < script.index('Stop-Service -Name MySQL80')
    assert "$Pending.Count -eq 0 -and $Listening.Count -eq 0" in script
    assert "listenport=8502 connectaddress=127.0.0.1 connectport=8503" in script
    for forbidden in ("Remove-Item", "Stop-Service -Name TakealotBlueMySQL", "-LocalPort 8501", "DROP DATABASE"):
        assert forbidden not in script
    for name in ("run_laptop_blue_web.ps1", "install_laptop_blue_web_task.ps1"):
        assert "legacy-blue-retired.json" in (ROOT / "scripts/ha" / name).read_text(encoding="utf-8")


def test_public_blue_never_falls_back_to_old_blue_or_green():
    import re

    config = (ROOT / "scripts/ha/takealot_erp_blue_public_nginx.conf").read_text(encoding="utf-8")
    upstreams = dict(re.findall(r"upstream\s+(\w+)\s*\{([^}]+)\}", config))
    assert set(upstreams) == {"takealot_blue_main_web", "takealot_blue_laptop_web"}
    for name, endpoint in {
        "takealot_blue_main_web": "127.0.0.1:18505",
        "takealot_blue_laptop_web": "127.0.0.1:18506",
    }.items():
        assert re.findall(r"\bserver\s+([^;\s]+)", upstreams[name]) == [endpoint]
        assert 'keepalive_timeout 2s;' in upstreams[name]
    routing_map = re.search(r"map\s+\$blue_selected_node\s+\$blue_web_upstream\s*\{([^}]+)\}", config)
    assert routing_map is not None
    assert dict(re.findall(r"(\w+)\s+(\w+)\s*;", routing_map.group(1))) == {
        "default": "takealot_blue_laptop_web", "main": "takealot_blue_main_web",
        "laptop": "takealot_blue_laptop_web",
    }
    assert 'X-Takealot-Environment "blue-stage-$blue_selected_node"' in config
    assert set(re.findall(r"proxy_pass\s+([^;\s]+)", config)) == {
        "http://127.0.0.1:18504/route", "http://$blue_web_upstream",
    }
    assert re.findall(r"\blisten\s+([^;\s]+)", config) == ["10.1.0.12:8443"]
    assert "proxy_next_upstream off;" in config
    assert ":8502" not in config
    assert ":8501" not in config
    assert ":18503" not in config  # Formal GREEN's existing tunnel stays separate.
    assert "ssl http2;" in config
    assert "backup" not in config
