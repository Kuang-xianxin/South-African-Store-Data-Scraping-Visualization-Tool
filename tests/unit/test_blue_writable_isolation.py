from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ha"))
    spec = importlib.util.spec_from_file_location("blue_test_db", ROOT / "scripts/ha/blue_test_db.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_writable_primary_guard_rejects_green_and_secondary(db_module):
    primary = ("DESKTOP-NTRMANG", 101, 3307, "D:/TakealotBlue/mysql/data/",
               "49de5abf-a8cc-11f1-9fc1-00e2699c0656", 0, 0)
    db_module.validate_primary(primary, writable=True)
    for index, value in [(0, "LAPTOP-2T5MN8EU"), (1, 1), (1, 102), (2, 3306),
                         (3, "D:/TakealotMySQLReplica/data"), (4, "unknown-uuid")]:
        row = list(primary)
        row[index] = value
        with pytest.raises(RuntimeError, match="identity"):
            db_module.validate_primary(tuple(row), writable=True)
    with pytest.raises(RuntimeError, match="not writable"):
        db_module.validate_primary((*primary[:5], 1, 1), writable=True)


def test_tls_requires_the_dedicated_ca_and_hostname_verification(db_module, monkeypatch):
    seen = []
    monkeypatch.setattr(db_module.ssl, "create_default_context", lambda **args: seen.append(args))
    db_module.tls_context()
    assert seen == [{"cafile": str(db_module.ROOT / "secrets/blue-primary-ca.pem")}]
    source = (ROOT / "scripts/ha/blue_test_db.py").read_text(encoding="utf-8")
    assert "SOURCE_SSL_VERIFY_SERVER_CERT=1" in source
    assert "check_hostname=False" not in source
    assert "REQUIRE SSL" in source
    assert "SET SESSION sql_log_bin=0" in source


def test_only_laptop_can_import_credentials_before_reading_stdin(db_module, monkeypatch):
    monkeypatch.setattr(db_module.blue_node, "config", lambda: {"node": "main"})
    with pytest.raises(RuntimeError, match="laptop-only"):
        db_module.import_laptop()


def test_loopback_bridge_has_only_blue_destinations():
    source = (ROOT / "scripts/ha/blue_db_bridge.py").read_text(encoding="utf-8")
    assert 'PORT = 13317' in source
    assert 'asyncio.start_server(handle, "127.0.0.1", PORT)' in source
    for forbidden in ("3306", "13306", "119.91.117.232", "0.0.0.0"):
        assert forbidden not in source


@pytest.fixture
def runtime_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ha"))
    spec = importlib.util.spec_from_file_location("blue_runtime", ROOT / "scripts/ha/blue_runtime.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_integrations_exclude_green_database_backups_and_portal_sessions(runtime_module):
    for allowed in ("TAKEALOT_STORES", "TAKEALOT_API_KEY", "TAKEALOT_STORE_02_API_KEY", "W8_API_TOKEN"):
        assert runtime_module.allowed_integration(allowed)
    for denied in ("TAKEALOT_DATABASE_URL", "TAKEALOT_BACKUP_DATABASE_URL", "TAKEALOT_PORTAL_BFF_ENABLED",
                   "TAKEALOT_PROJECT_ROOT", "TAKEALOT_SESSION_COOKIE_NAME", "MYSQL_PWD", "PATH"):
        assert not runtime_module.allowed_integration(denied)


def test_blue_application_urls_have_verified_tls_and_only_blue_credentials(runtime_module, monkeypatch):
    from sqlalchemy.engine import make_url

    for node, port in (("main", 3307), ("laptop", 13317)):
        monkeypatch.setattr(runtime_module.blue_node, "config", lambda n=node: {"node": n})
        monkeypatch.setattr(runtime_module.blue_test_db, "credentials", lambda n=node: {
            f"blue_web_{n}": "dummy", f"blue_worker_{n}": "dummy"})
        for worker in (False, True):
            url = make_url(runtime_module.database_url(worker=worker))
            assert (url.host, url.port, url.database) == ("127.0.0.1", port, "takealot_ops")
            assert url.username == f"blue_{'worker' if worker else 'web'}_{node}"
            assert url.query["ssl_check_hostname"] == "true"
            assert url.query["ssl_ca"].endswith("blue-primary-ca.pem")


def test_global_connection_guard_rejects_fallbacks_and_checks_seed(runtime_module, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="forbids SQLite"):
        runtime_module.validate_dbapi_connection(object(), None)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/seed.json").write_text(json.dumps({"seed_sha256": "expected"}), encoding="utf-8")
    monkeypatch.setattr(runtime_module.blue_node, "ROOT", tmp_path)
    identity = ("DESKTOP-NTRMANG", 101, 3307, "D:/TakealotBlue/mysql/data/",
                "49de5abf-a8cc-11f1-9fc1-00e2699c0656", 0, 0)

    class Cursor:
        def __init__(self):
            self.rows = iter([identity, ("blue", "wrong-seed")])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, _):
            pass

        def fetchone(self):
            return next(self.rows)

    connection_class = type("Connection", (), {"__module__": "pymysql.connections", "cursor": lambda _: Cursor()})
    with pytest.raises(RuntimeError, match="seed identity mismatch"):
        runtime_module.validate_dbapi_connection(connection_class(), None)


def test_replication_password_generation_respects_mysql_8_limit():
    import secrets

    assert len(secrets.token_urlsafe(24)) == 32
    source = (ROOT / "scripts/ha/blue_test_db.py").read_text(encoding="utf-8")
    assert '24 if user == "blue_replication" else 36' in source


def test_installed_pool_guard_rejects_actual_sqlite_connection(runtime_module):
    from sqlalchemy import Engine, create_engine, event

    runtime_module.install_database_guard()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="forbids SQLite"):
            engine.connect()
    finally:
        engine.dispose()
        event.remove(Engine, "connect", runtime_module.validate_dbapi_connection)
