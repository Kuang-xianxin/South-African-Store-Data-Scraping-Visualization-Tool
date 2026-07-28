from __future__ import annotations

from pathlib import Path
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorNetworkError
from takealot_ops.competitors.service import CompetitorCollectionResult
from takealot_ops.erp.daily_report import capture_daily_report
from takealot_ops.erp.web import create_app
from takealot_ops.storage.models import OfferCurrent


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


def _create_operator(
    client: TestClient,
    csrf: str,
    *,
    username: str,
) -> None:
    response = client.post(
        "/api/auth/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": username,
            "display_name": username.replace(".", " ").title(),
            "password": "operator-password-123",
            "role": "operator",
        },
    )
    assert response.status_code == 200


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


def test_competitor_network_failure_returns_retryable_service_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            raise CompetitorNetworkError(
                "Takealot 当前无法访问，请检查梯子或代理连接后重试"
            )

        async def __aexit__(self, *_: object) -> None:
            pass

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        FailingCollector,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Takealot 当前无法访问，请检查梯子或代理连接后重试"
    )


@pytest.mark.parametrize(
    ("failure_kind", "expected_status"),
    [
        ("validation-uncertain", 409),
        ("suspected-invalid", 404),
        ("confirmed-invalid", 410),
    ],
)
def test_competitor_link_validation_returns_distinct_status(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
    expected_status: int,
) -> None:
    class LinkStateCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            return CompetitorCollectionResult(
                plid="12345678",
                title="PLID12345678",
                succeeded=False,
                message="链接复核状态",
                failure_kind=failure_kind,
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        LinkStateCollector,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"] == "链接复核状态"


def test_competitor_batch_metadata_is_idempotent_and_logged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    class SuccessfulCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            nonlocal calls
            calls += 1
            return CompetitorCollectionResult(
                plid="12345678",
                title="Example product",
                succeeded=True,
                message="采集成功",
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        SuccessfulCollector,
    )
    app = create_app(tmp_path)
    payload = {
        "url": "https://www.takealot.com/example/PLID12345678",
        "batch_id": "batch-1",
        "request_id": "request-1",
        "item_index": 2,
        "total_items": 5,
    }

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        first = client.post("/api/competitors/collect", headers=headers, json=payload)
        second = client.post("/api/competitors/collect", headers=headers, json=payload)
        event = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-1",
                "event": "auto_resume",
                "completed": 2,
                "total": 5,
                "pending": 3,
                "reason": "page reload",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert event.status_code == 200
    assert calls == 1
    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(
        encoding="utf-8"
    )
    assert "link_start batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "link_reused batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "batch_event batch=batch-1 event=auto_resume completed=2 total=5 pending=3" in log_text


def test_competitor_batch_status_is_shared_and_blocks_another_operator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        admin_csrf = str(session["csrf_token"])
        _create_operator(admin, admin_csrf, username="operator.two")
        started = admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin",
                "event": "start",
                "completed": 0,
                "total": 12,
                "pending": 12,
                "succeeded": 0,
                "failed": 0,
                "terminal": 0,
            },
        )
        assert started.status_code == 200

    with TestClient(app, client=("192.168.1.8", 50001)) as operator:
        login = operator.post(
            "/api/auth/login",
            json={
                "username": "operator.two",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        operator_csrf = str(login.json()["csrf_token"])
        shared = operator.get("/api/competitors/batch-status")
        assert shared.status_code == 200
        assert shared.json()["active"] is True
        assert shared.json()["owner_username"] == "localadmin"
        blocked = operator.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": operator_csrf},
            json={
                "batch_id": "batch-operator",
                "client_id": "client-operator",
                "event": "start",
                "completed": 0,
                "total": 3,
                "pending": 3,
            },
        )
        assert blocked.status_code == 409
        assert "Local Admin 正在采集竞品" in blocked.json()["detail"]

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "localadmin", "password": "pass-123"},
        )
        admin_csrf = str(login.json()["csrf_token"])
        completed = admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin",
                "event": "completed",
                "completed": 12,
                "total": 12,
                "pending": 0,
                "succeeded": 11,
                "failed": 1,
                "terminal": 1,
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"]["active"] is False


def test_refresh_cooldown_is_shared_for_operators_and_admin_is_exempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def successful_refresh(_: Path):
        nonlocal calls
        calls += 1
        return SimpleNamespace(succeeded=True, message="刷新成功")

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.run_dashboard_refresh",
        successful_refresh,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        admin_csrf = str(session["csrf_token"])
        _create_operator(admin, admin_csrf, username="operator.one")

    with TestClient(app, client=("192.168.1.8", 50001)) as operator:
        login = operator.post(
            "/api/auth/login",
            json={
                "username": "operator.one",
                "password": "operator-password-123",
            },
        )
        operator_csrf = str(login.json()["csrf_token"])
        first = operator.post(
            "/api/erp/refresh",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert first.status_code == 200
        assert first.json()["refresh_status"]["can_refresh"] is False
        blocked = operator.post(
            "/api/erp/refresh",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert blocked.status_code == 429
        assert "全员冷却" in blocked.json()["detail"]

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "localadmin", "password": "pass-123"},
        )
        admin_csrf = str(login.json()["csrf_token"])
        status = admin.get("/api/erp/refresh-status")
        assert status.json()["admin_exempt"] is True
        assert status.json()["can_refresh"] is True
        override = admin.post(
            "/api/erp/refresh",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert override.status_code == 200

    assert calls == 2


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


def test_daily_report_api_reads_versions_and_bulk_confirms_ready_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        issued = _bootstrap(client)
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                session.add(
                    OfferCurrent(
                        offer_id="offer-a",
                        sku="9900000000001",
                        title="Product A",
                        captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                        page_views_30_days=10,
                        takealot_available_stock=5,
                    )
                )
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 24),
                    slot=slot,
                    captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        report = client.get(
            "/api/erp/daily-report?business_date=2026-07-24"
        )
        assert report.status_code == 200
        assert report.json()["counts"]["ready"] == 1
        noted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/note",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={
                "note": "管理员追加一条独立备注",
                "issue_type": "general",
            },
        )
        assert noted.status_code == 200
        noted_report = client.get(
            "/api/erp/daily-report?business_date=2026-07-24"
        ).json()
        assert noted_report["items"][0]["operator_notes"][0]["note"] == (
            "管理员追加一条独立备注"
        )
        note_id = noted_report["items"][0]["operator_notes"][0]["id"]
        updated = client.patch(
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={
                "note": "管理员修改后的库存备注",
                "issue_type": "stock_continuity",
            },
        )
        assert updated.status_code == 200
        updated_note = client.get(
            "/api/erp/daily-report?business_date=2026-07-24"
        ).json()["items"][0]["operator_notes"][0]
        assert updated_note["note"] == "管理员修改后的库存备注"
        assert updated_note["issue_type"] == "stock_continuity"
        assert updated_note["updated_by"] == "Local Admin"
        deleted = client.request(
            "DELETE",
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "该备注已过期，确认删除"},
        )
        assert deleted.status_code == 200
        after_delete = client.get(
            "/api/erp/daily-report?business_date=2026-07-24"
        ).json()
        assert after_delete["items"][0]["operator_notes"] == []
        assert after_delete["handled_actions"][0]["action_type"] == (
            "operator_note_deleted"
        )
        assert after_delete["handled_actions"][0]["note"] == (
            "该备注已过期，确认删除"
        )
        assert after_delete["handled_actions"][0]["detail"]["deleted_note"] == (
            "管理员修改后的库存备注"
        )
        confirmed = client.post(
            "/api/erp/daily-report/2026-07-24/confirm-ready",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "早晚值一致，批量确认"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["confirmed"] == 1
        assert confirmed.json()["exported"] is True
        reverted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "复核后发现需要重新选择版本"},
        )
        assert reverted.status_code == 200
        reopened = client.get(
            "/api/erp/daily-report?business_date=2026-07-24"
        ).json()
        assert reopened["items"][0]["status"] == "needs_review"
        assert reopened["items"][0]["review_issues"] == [
            {"type": "confirmation_reverted", "fields": []}
        ]
        assert reopened["items"][0]["confirmation_revert"]["reverted_by"] == (
            "Local Admin"
        )
        repeated_revert = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "重复撤销"},
        )
        assert repeated_revert.status_code == 409
        exported = tmp_path / "exports" / "operations-daily" / "2026-07-24"
        assert any(exported.glob("*.xlsx"))


def test_daily_report_stock_difference_can_be_confirmed_logged_and_reopened(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-stock-audit.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        issued = _bootstrap(client)
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                session.add(
                    OfferCurrent(
                        offer_id="offer-stock",
                        sku="9900000000099",
                        title="Stock Audit Product",
                        captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                        page_views_30_days=10,
                        takealot_available_stock=9,
                    )
                )
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 24),
                    slot=slot,
                    captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
                )
            with Session(engine) as session, session.begin():
                session.get(
                    OfferCurrent,
                    "offer-stock",
                ).takealot_available_stock = 8
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 25),
                    slot=slot,
                    captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        before = client.get(
            "/api/erp/daily-report?business_date=2026-07-25"
        ).json()
        assert before["pending_actions"][0]["offer_id"] == "offer-stock"
        handled = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "确认属于平台库存调整"},
        )
        assert handled.status_code == 200
        after = client.get(
            "/api/erp/daily-report?business_date=2026-07-25"
        ).json()
        assert after["pending_actions"] == []
        assert after["items"][0]["stock_check"]["mismatch"] is True
        assert after["items"][0]["stock_check"]["dismissed"] is True
        assert after["handled_actions"][0]["active"] is True
        assert after["handled_actions"][0]["handled_by"] == "Local Admin"

        reopened = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert/reopen",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"note": "误操作，恢复待办"},
        )
        assert reopened.status_code == 200
        final = client.get(
            "/api/erp/daily-report?business_date=2026-07-25"
        ).json()
        assert final["pending_actions"][0]["offer_id"] == "offer-stock"
        assert final["handled_actions"][0]["action_type"] == (
            "stock_alert_reopened"
        )
        assert final["handled_actions"][0]["note"] == "误操作，恢复待办"
        original = next(
            row
            for row in final["handled_actions"]
            if row["action_type"] == "stock_difference"
        )
        assert original["active"] is False
        assert original["reversal"]["note"] == (
            "误操作，恢复待办"
        )
