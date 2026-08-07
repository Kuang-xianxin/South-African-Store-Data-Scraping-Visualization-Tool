from __future__ import annotations

from fastapi.testclient import TestClient

from takealot_ops.erp.web import create_app


def _bootstrap(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "warehouse-admin",
            "display_name": "Warehouse Admin",
            "password": "pass-123",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_portal_credentials_and_writes_are_loopback_only_and_default_off(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "platform-warehouse-web.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL", f"sqlite:///{database_path.as_posix()}"
    )
    monkeypatch.delenv("TAKEALOT_PORTAL_BFF_ENABLED", raising=False)
    monkeypatch.setattr(
        "takealot_ops.platform_warehouse.credentials.WindowsPortalCredentialStore.get",
        lambda self, store_code: None,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        issued = _bootstrap(local)
        csrf = str(issued["csrf_token"])
        disabled = local.post(
            "/api/erp/platform-warehouse/create-direct",
            headers={"X-CSRF-Token": csrf},
            json={
                "client_request_id": "dc9c15f9-b967-48ef-bf19-dd7a4d711b88",
                "lines": [{"offer_id": "offer-1", "jhb_quantity": 1}],
            },
        )
        assert disabled.status_code == 503
        assert "总开关" in disabled.json()["detail"]
        overview = local.get("/api/erp/platform-warehouse")
        assert overview.status_code == 200
        assert overview.json()["portal"]["enabled"] is False
        assert overview.json()["portal"]["credentials_persisted"] is False
        assert local.post(
            "/api/erp/platform-warehouse/portal/login",
            headers={"X-CSRF-Token": csrf},
            json={"email": "seller@example.com", "password": "must-not-enter-browser"},
        ).status_code == 404
        assert local.post(
            "/api/erp/platform-warehouse/drafts",
            headers={"X-CSRF-Token": csrf},
            json={"lines": [{"offer_id": "offer-1", "jhb_quantity": 1}]},
        ).status_code == 404
        assert local.post(
            "/api/erp/platform-warehouse/drafts/1/review",
            headers={"X-CSRF-Token": csrf},
        ).status_code == 404
        assert local.post(
            "/api/erp/platform-warehouse/drafts/1/create-upstream",
            headers={"X-CSRF-Token": csrf},
            json={"approval_token": "x" * 64, "confirmation_text": "PW-1"},
        ).status_code == 404
    with TestClient(app, client=("192.168.1.8", 50001)) as remote:
        login = remote.post(
            "/api/auth/login",
            json={"username": "warehouse-admin", "password": "pass-123"},
        )
        assert login.status_code == 200
        csrf = str(login.json()["csrf_token"])
        rejected = remote.post(
            "/api/erp/platform-warehouse/create-direct",
            headers={"X-CSRF-Token": csrf},
            json={
                "client_request_id": "a04cf738-5d4f-4f23-9f7a-5f7552f5caa3",
                "lines": [{"offer_id": "offer-1", "jhb_quantity": 1}],
            },
        )
        assert rejected.status_code == 403
        assert "服务器本机" in rejected.json()["detail"]
