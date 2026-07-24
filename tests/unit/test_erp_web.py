from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from takealot_ops.erp.web import create_app


PROJECT_ROOT = Path(__file__).parents[2]


def _bootstrap(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "localadmin",
            "display_name": "Local Admin",
            "password": "pass-123",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_erp_requires_login_and_bootstraps_only_from_loopback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(PROJECT_ROOT)

    with TestClient(app, client=("192.168.1.8", 50000)) as remote:
        status = remote.get("/api/auth/status")
        assert status.json() == {
            "setup_required": True,
            "bootstrap_allowed": False,
        }
        denied = remote.post(
            "/api/auth/bootstrap",
            json={
                "username": "remoteadmin",
                "display_name": "Remote",
                "password": "correct-horse-battery",
            },
        )
        assert denied.status_code == 403
        assert remote.get("/api/erp/summary?as_of=2026-07-20").status_code == 401

    with TestClient(app, client=("127.0.0.1", 50001)) as local:
        too_short = local.post(
            "/api/auth/bootstrap",
            json={
                "username": "localadmin",
                "display_name": "Local Admin",
                "password": "pass123",
            },
        )
        assert too_short.status_code == 422
        assert too_short.json()["detail"] == "密码至少需要 8 个字符"
        session = _bootstrap(local)
        assert session["user"]["role"] == "admin"
        summary = local.get("/api/erp/summary?as_of=2026-07-20")
        assert summary.status_code == 200
        assert summary.json()["latest_metric_date"] is None
        assert database_path.exists()


def test_viewer_can_read_but_cannot_run_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(PROJECT_ROOT)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "viewer.one",
                "display_name": "Viewer One",
                "password": "viewer-password-123",
                "role": "viewer",
            },
        )
        assert created.status_code == 200

    with TestClient(app, client=("192.168.1.8", 50001)) as viewer:
        login = viewer.post(
            "/api/auth/login",
            json={"username": "viewer.one", "password": "viewer-password-123"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert viewer.get("/api/erp/freshness").status_code == 200
        denied = viewer.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "当前账号只有查看权限"
        assert viewer.get("/api/auth/users").status_code == 403


def test_csrf_and_last_admin_protection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(PROJECT_ROOT)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        admin_id = session["user"]["id"]
        assert client.post(
            "/api/auth/users",
            json={
                "username": "operator.one",
                "display_name": "Operator",
                "password": "operator-password-1",
                "role": "operator",
            },
        ).status_code == 403

        response = client.patch(
            f"/api/auth/users/{admin_id}",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"role": "viewer"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "不能停用或降级唯一的管理员"


def test_erp_rejects_unsupported_quadrant_percentile_after_login(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'erp.db').as_posix()}",
    )
    app = create_app(PROJECT_ROOT)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        response = client.get("/api/erp/quadrants?as_of=2026-07-20&percentile=40")

    assert response.status_code == 422
    assert "25" in response.json()["detail"]
