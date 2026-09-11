from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.erp.web import create_app
from takealot_ops.settings import SettingsError
from takealot_ops.storage.models import ErpSession, ErpUser


PROJECT_ROOT = Path(__file__).parents[2]


def _copy_runtime_rules(project_root: Path) -> None:
    config_dir = project_root / "config"
    config_dir.mkdir()
    for config_name in ("anomaly_rules.yaml", "sale_status_rules.yaml"):
        (config_dir / config_name).write_bytes(
            (PROJECT_ROOT / "config" / config_name).read_bytes()
        )


def test_read_only_blue_login_uses_memory_and_blocks_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_runtime_rules(tmp_path)
    database_path = tmp_path / "blue.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    monkeypatch.delenv("TAKEALOT_READ_ONLY_TEST_MODE", raising=False)
    monkeypatch.delenv("TAKEALOT_SESSION_COOKIE_NAME", raising=False)
    writable_app = create_app(tmp_path)
    with TestClient(writable_app, client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "blue.admin",
                "display_name": "Blue Admin",
                "password": "blue-password-123",
            },
        )
        assert created.status_code == 200

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            user_before = session.scalar(
                select(ErpUser).where(ErpUser.username == "blue.admin")
            )
            assert user_before is not None
            last_login_before = user_before.last_login_at
            updated_before = user_before.updated_at
            stored_sessions_before = int(
                session.scalar(select(func.count(ErpSession.token_hash))) or 0
            )

        monkeypatch.setenv("TAKEALOT_READ_ONLY_TEST_MODE", "1")
        monkeypatch.setenv("TAKEALOT_DEPLOYMENT_LABEL", "blue-laptop")
        monkeypatch.setenv("TAKEALOT_SESSION_COOKIE_NAME", "takealot_blue_session")
        blue_app = create_app(tmp_path)
        with TestClient(blue_app, client=("192.168.1.9", 50001)) as blue:
            health = blue.get("/api/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "ok",
                "application": "takealot-erp",
                "mode": "read-only-test",
                "deployment": "blue-laptop",
            }
            assert health.headers["X-Takealot-Deployment"] == "blue-laptop"
            assert health.headers["X-Takealot-Read-Only"] == "true"

            login = blue.post(
                "/api/auth/login",
                json={"username": "blue.admin", "password": "blue-password-123"},
            )
            assert login.status_code == 200
            assert blue.cookies.get("takealot_blue_session")
            assert blue.cookies.get("takealot_erp_session") is None

            restored = blue.get("/api/auth/session")
            assert restored.status_code == 200
            assert restored.json()["user"]["username"] == "blue.admin"

            blocked = blue.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": login.json()["csrf_token"]},
                json={"code": "blocked", "display_name": "Blocked"},
            )
            assert blocked.status_code == 403
            assert blocked.json()["detail"] == "蓝版是只读测试环境，修改和采集操作已禁用"

            logged_out = blue.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": login.json()["csrf_token"]},
            )
            assert logged_out.status_code == 200
            assert blue.get("/api/auth/session").status_code == 401

        with Session(engine) as session:
            user_after = session.scalar(
                select(ErpUser).where(ErpUser.username == "blue.admin")
            )
            assert user_after is not None
            assert user_after.last_login_at == last_login_before
            assert user_after.updated_at == updated_before
            assert int(
                session.scalar(select(func.count(ErpSession.token_hash))) or 0
            ) == stored_sessions_before
    finally:
        engine.dispose()


def test_read_only_test_mode_requires_web_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAKEALOT_READ_ONLY_TEST_MODE", "1")
    monkeypatch.delenv("TAKEALOT_WEB_ONLY", raising=False)

    with pytest.raises(SettingsError, match="TAKEALOT_WEB_ONLY"):
        create_app(tmp_path)
