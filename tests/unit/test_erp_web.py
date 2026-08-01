from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorNetworkError
from takealot_ops.competitors.batch import CollectionBatchBusyError
from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorDiscoveredTarget,
)
from takealot_ops.erp.daily_report import capture_daily_report
from takealot_ops.erp.web import create_app
from takealot_ops.storage.models import CompetitorSnapshot, ErpSession, OfferCurrent


PROJECT_ROOT = Path(__file__).parents[2]
TRUSTED_PRODUCT_IMAGE_URL = (
    "https://takealot.s3.amazonaws.com/covers_images/37b5fc661b694ed5969280cc0cea2ce4/s.file"
)


def _bootstrap(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "kxx",
            "display_name": "KXX Admin",
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


def test_product_thumbnail_is_authenticated_and_rejects_untrusted_hosts(
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
        unauthorized = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL},
        )
        assert unauthorized.status_code == 401
        _bootstrap(client)

        rejected = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": "https://example.com/image.jpg"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "只允许读取 Takealot 官方商品图片"

        invalid_size = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL, "size": 512},
        )
        assert invalid_size.status_code == 422
        assert invalid_size.json()["detail"] == "缩略图尺寸只支持 192、384、640 像素"

        thumbnail = tmp_path / "thumbnail.jpg"
        thumbnail.write_bytes(b"\xff\xd8\xff\xd9")
        requested_urls: list[str] = []

        requested_sizes: list[int] = []

        def fake_thumbnail_path(image_url: str, size: int) -> Path:
            requested_urls.append(image_url)
            requested_sizes.append(size)
            return thumbnail

        monkeypatch.setattr(
            app.state.product_thumbnail_cache,
            "thumbnail_path",
            fake_thumbnail_path,
        )
        response = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL, "size": 640},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == ("private, max-age=604800, immutable")
        assert requested_urls == [TRUSTED_PRODUCT_IMAGE_URL]
        assert requested_sizes == [640]


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
        assert "users.manage" in session["user"]["permissions"]
        assert session["user"]["permissions_customized"] is False
        summary = local.get("/api/erp/summary?as_of=2026-07-20")
        assert summary.status_code == 200
        assert summary.json()["latest_metric_date"] is None
        assert database_path.exists()


def test_store_assignments_scale_and_all_store_accounts_include_future_stores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-store-access.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(PROJECT_ROOT)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        assert session["user"]["all_stores"] is True
        assert len(session["user"]["accessible_stores"]) == 1

        initial_stores = admin.get("/api/auth/stores")
        assert initial_stores.status_code == 200
        current_store = initial_stores.json()["items"][0]
        assert current_store["code"] == "current"
        assert current_store["data_connected"] is True

        planned_stores: list[dict[str, object]] = []
        for number in range(2, 7):
            created = admin.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": csrf},
                json={
                    "code": f"shop-{number:02d}",
                    "display_name": f"店铺 {number}",
                },
            )
            assert created.status_code == 200
            planned_stores.append(created.json()["store"])

        duplicate = admin.post(
            "/api/auth/stores",
            headers={"X-CSRF-Token": csrf},
            json={"code": "shop-02", "display_name": "重复店铺"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "该店铺代码已存在"

        admin_session = admin.get("/api/auth/session")
        assert admin_session.status_code == 200
        assert len(admin_session.json()["user"]["accessible_stores"]) == 6

        current_store_id = int(current_store["id"])
        planned_ids = [int(store["id"]) for store in planned_stores]
        operator_one = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "operator.one",
                "display_name": "运营一",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": [current_store_id, planned_ids[0]],
            },
        )
        assert operator_one.status_code == 200
        operator_one_user = operator_one.json()["user"]
        assert operator_one_user["all_stores"] is False
        assert operator_one_user["assigned_store_ids"] == [
            current_store_id,
            planned_ids[0],
        ]

        operator_two = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "operator.two",
                "display_name": "运营二",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": planned_ids[1:3],
            },
        )
        assert operator_two.status_code == 200

        owner = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "owner.master",
                "display_name": "大师（老板）",
                "password": "owner-password-123",
                "role": "viewer",
                "all_stores": True,
                "store_ids": [],
            },
        )
        assert owner.status_code == 200
        assert len(owner.json()["user"]["accessible_stores"]) == 6

        unknown_store = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "invalid.store",
                "display_name": "无效店铺",
                "password": "invalid-password-123",
                "role": "viewer",
                "all_stores": False,
                "store_ids": [999999],
            },
        )
        assert unknown_store.status_code == 422
        assert unknown_store.json()["detail"] == "店铺不存在：999999"

        protected_current = admin.patch(
            f"/api/auth/stores/{current_store_id}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert protected_current.status_code == 409
        assert protected_current.json()["detail"] == "当前已接入数据的店铺不能停用"

        with TestClient(app, client=("192.168.1.8", 50001)) as first_operator:
            login = first_operator.post(
                "/api/auth/login",
                json={
                    "username": "operator.one",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            accessible_ids = {
                store["id"]
                for store in login.json()["user"]["accessible_stores"]
            }
            assert accessible_ids == {current_store_id, planned_ids[0]}
            assert first_operator.get("/api/erp/freshness").status_code == 200

            reassigned = admin.patch(
                f"/api/auth/users/{operator_one_user['id']}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "all_stores": False,
                    "store_ids": planned_ids[3:5],
                },
            )
            assert reassigned.status_code == 200
            assert first_operator.get("/api/auth/session").status_code == 401

        with TestClient(app, client=("192.168.1.8", 50002)) as second_operator:
            login = second_operator.post(
                "/api/auth/login",
                json={
                    "username": "operator.two",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            denied = second_operator.get("/api/erp/freshness")
            assert denied.status_code == 403
            assert (
                denied.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )

        with TestClient(app, client=("192.168.1.8", 50003)) as owner_client:
            login = owner_client.post(
                "/api/auth/login",
                json={
                    "username": "owner.master",
                    "password": "owner-password-123",
                },
            )
            assert login.status_code == 200
            assert len(login.json()["user"]["accessible_stores"]) == 6
            assert owner_client.get("/api/erp/freshness").status_code == 200

            future = admin.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": csrf},
                json={"code": "shop-07", "display_name": "店铺 7"},
            )
            assert future.status_code == 200
            refreshed_scope = owner_client.get("/api/auth/session")
            assert refreshed_scope.status_code == 200
            assert len(refreshed_scope.json()["user"]["accessible_stores"]) == 7


def test_public_competitor_module_does_not_require_store_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-public-module.db"
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
                "username": "public.competitors",
                "display_name": "公共竞品账号",
                "password": "competitor-password-123",
                "role": "viewer",
                "permissions": [
                    "store.view",
                    "competitors.view",
                    "competitors.collect",
                    "nft102.manage",
                ],
                "all_stores": False,
                "store_ids": [],
            },
        )
        assert created.status_code == 200
        assert created.json()["user"]["accessible_stores"] == []

        with TestClient(app, client=("192.168.1.8", 50001)) as public_user:
            login = public_user.post(
                "/api/auth/login",
                json={
                    "username": "public.competitors",
                    "password": "competitor-password-123",
                },
            )
            assert login.status_code == 200
            assert login.json()["user"]["accessible_stores"] == []
            public_csrf = str(login.json()["csrf_token"])

            assert public_user.get("/api/competitors").status_code == 200
            invalid_collect = public_user.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": public_csrf},
                json={"url": "invalid"},
            )
            assert invalid_collect.status_code == 403
            assert "仅限 kxx 账号" in invalid_collect.json()["detail"]
            assert public_user.get("/api/erp/product-thumbnail").status_code == 422
            assert (
                public_user.post(
                    "/api/erp/nft102/inspect",
                    headers={"X-CSRF-Token": public_csrf},
                ).status_code
                == 422
            )

            store_data = public_user.get("/api/erp/summary?as_of=2026-07-24")
            assert store_data.status_code == 403
            assert (
                store_data.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )
            freshness = public_user.get("/api/erp/freshness")
            assert freshness.status_code == 403
            assert (
                freshness.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )
            logistics = public_user.get("/api/erp/logistics")
            assert logistics.status_code == 403
            assert (
                logistics.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )


def test_session_lasts_seven_days_and_slides_after_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(PROJECT_ROOT)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        issued = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "localadmin",
                "display_name": "Local Admin",
                "password": "pass-123",
            },
        )
        assert issued.status_code == 200
        assert "max-age=604800" in issued.headers["set-cookie"].lower()
        initial_expiry = datetime.fromisoformat(issued.json()["expires_at"])
        assert timedelta(days=6, hours=23) < initial_expiry - datetime.utcnow()

        session_token = client.cookies.get("takealot_erp_session")
        assert session_token
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        previous_expiry = datetime.utcnow() + timedelta(days=1)
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow()
                record.expires_at = previous_expiry

            restored = client.get("/api/auth/session")
            assert restored.status_code == 200
            assert "max-age=604800" in restored.headers["set-cookie"].lower()
            restored_expiry = datetime.fromisoformat(restored.json()["expires_at"])
            assert restored_expiry > previous_expiry + timedelta(days=5)

            immediate = client.get("/api/auth/session")
            assert immediate.status_code == 200
            assert "set-cookie" not in immediate.headers
            assert datetime.fromisoformat(immediate.json()["expires_at"]) == (restored_expiry)

            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow() - timedelta(hours=23)
                record.expires_at = datetime.utcnow() + timedelta(days=6, hours=1)

            before_interval = client.get("/api/erp/freshness")
            assert before_interval.status_code == 200
            assert "set-cookie" not in before_interval.headers

            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow() - timedelta(days=1, minutes=1)
                record.expires_at = datetime.utcnow() + timedelta(days=7)

            protected = client.get("/api/erp/freshness")
            assert protected.status_code == 200
            assert "max-age=604800" in protected.headers["set-cookie"].lower()
        finally:
            engine.dispose()


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
        assert viewer.get("/api/erp/daily-report?business_date=2026-07-24").status_code == 200
        assert viewer.get("/api/erp/daily-report/export?through=2026-07-24").status_code == 200
        denied = viewer.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "当前账号不能采集竞品"
        denied_daily_action = viewer.post(
            "/api/erp/daily-report/2026-07-24/offer-a/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "ordered_units": 1,
                "reason": "platform_delay",
                "note": "查看员不应写入",
            },
        )
        assert denied_daily_action.status_code == 403
        assert denied_daily_action.json()["detail"] == "当前账号可以查看运营日报，但不能处理待办"
        denied_export = viewer.post(
            "/api/erp/daily-report/export",
            headers={"X-CSRF-Token": csrf},
            json={"as_of": "2026-07-24"},
        )
        assert denied_export.status_code == 403
        assert denied_export.json()["detail"] == "当前账号不能生成运营日报 Excel"
        assert viewer.get("/api/auth/users").status_code == 403


def test_selection_template_and_account_permission_overrides(
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
                "username": "selection.one",
                "display_name": "Selection One",
                "password": "selection-password-123",
                "role": "selection",
            },
        )
        assert created.status_code == 200
        selection = created.json()["user"]
        assert selection["role"] == "selection"
        assert selection["permissions_customized"] is False
        assert set(selection["permissions"]) == {
            "competitors.view",
            "competitors.collect",
            "daily_report.view",
        }

        with TestClient(app, client=("192.168.1.8", 50001)) as default_selection:
            login = default_selection.post(
                "/api/auth/login",
                json={
                    "username": "selection.one",
                    "password": "selection-password-123",
                },
            )
            assert login.status_code == 200
            selection_csrf = login.json()["csrf_token"]
            blocked_collect = default_selection.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": selection_csrf},
                json={"url": "invalid"},
            )
            assert blocked_collect.status_code == 403
            assert "仅限 kxx 账号" in blocked_collect.json()["detail"]
            denied_pending = default_selection.post(
                "/api/erp/daily-report/2026-07-24/not-found/manual",
                headers={"X-CSRF-Token": selection_csrf},
                json={
                    "ordered_units": 1,
                    "reason": "platform_delay",
                    "note": "选品模板不能处理待办",
                },
            )
            assert denied_pending.status_code == 403
            assert denied_pending.json()["detail"] == "当前账号可以查看运营日报，但不能处理待办"

        customized = admin.patch(
            f"/api/auth/users/{selection['id']}",
            headers={"X-CSRF-Token": csrf},
            json={
                "permissions": [
                    "competitors.view",
                    "daily_report.manage",
                ]
            },
        )
        assert customized.status_code == 200
        customized_user = customized.json()["user"]
        assert customized_user["permissions_customized"] is True
        assert set(customized_user["permissions"]) == {
            "competitors.view",
            "daily_report.view",
            "daily_report.manage",
        }

        with TestClient(app, client=("192.168.1.8", 50001)) as selection_client:
            login = selection_client.post(
                "/api/auth/login",
                json={
                    "username": "selection.one",
                    "password": "selection-password-123",
                },
            )
            assert login.status_code == 200
            selection_csrf = login.json()["csrf_token"]
            assert selection_client.get("/api/competitors").status_code == 200
            denied_collect = selection_client.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": selection_csrf},
                json={"url": "invalid"},
            )
            assert denied_collect.status_code == 403
            assert (
                selection_client.get("/api/erp/daily-report?business_date=2026-07-24").status_code
                == 200
            )
            allowed_daily_write = selection_client.post(
                "/api/erp/daily-report/2026-07-24/not-found/manual",
                headers={"X-CSRF-Token": selection_csrf},
                json={
                    "ordered_units": 1,
                    "reason": "platform_delay",
                    "note": "自定义权限验证",
                },
            )
            assert allowed_daily_write.status_code != 403
            assert selection_client.get("/api/erp/summary?as_of=2026-07-24").status_code == 403

        reset = admin.patch(
            f"/api/auth/users/{selection['id']}",
            headers={"X-CSRF-Token": csrf},
            json={
                "role": "selection",
                "permissions": [
                    "competitors.view",
                    "competitors.collect",
                    "daily_report.view",
                ],
            },
        )
        assert reset.status_code == 200
        assert reset.json()["user"]["permissions_customized"] is False


def test_competitor_network_failure_returns_retryable_service_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            raise CompetitorNetworkError("Takealot 当前无法访问，请检查梯子或代理连接后重试")

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
    assert response.json()["detail"] == ("Takealot 当前无法访问，请检查梯子或代理连接后重试")


@pytest.mark.parametrize(
    ("failure_kind", "expected_status"),
    [
        ("validation-uncertain", 409),
        ("stock-unprobed", 424),
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
    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(encoding="utf-8")
    assert "link_start batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "link_reused batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "batch_event batch=batch-1 event=auto_resume completed=2 total=5 pending=3" in log_text


def test_erp_reuses_and_recycles_hidden_competitor_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    public_clients: list[object] = []
    collector_clients: list[object] = []
    link_delays: list[float] = []

    async def fake_link_cooldown(seconds: float) -> None:
        link_delays.append(seconds)

    class FakePublicClient:
        def __init__(self) -> None:
            self.close_calls = 0
            public_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    class FakeCollector:
        def __init__(self, *, client: object, **_: object) -> None:
            self.client = client
            collector_clients.append(client)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(
            self,
            url: str,
            **_: object,
        ) -> CompetitorCollectionResult:
            plid = url.rsplit("PLID", 1)[-1]
            if plid == "33333333":
                return CompetitorCollectionResult(
                    plid=plid,
                    title=f"PLID{plid}",
                    succeeded=False,
                    message="临时网络失败",
                    retryable=True,
                    failure_kind="network",
                )
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=True,
                message="采集成功",
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorPublicClient",
        FakePublicClient,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        FakeCollector,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._competitor_link_cooldown_seconds",
        lambda min_seconds, max_seconds: (min_seconds + max_seconds) / 2,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._sleep_competitor_link_cooldown",
        fake_link_cooldown,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        statuses = [
            client.post(
                "/api/competitors/collect",
                headers=headers,
                json={"url": f"https://www.takealot.com/example/PLID{plid}"},
            ).status_code
            for plid in ("11111111", "22222222", "33333333", "44444444")
        ]

    assert statuses == [200, 200, 503, 200]
    assert len(public_clients) == 2
    assert collector_clients[:3] == [public_clients[0]] * 3
    assert collector_clients[3] is public_clients[1]
    assert [client.close_calls for client in public_clients] == [1, 1]
    assert link_delays == [7.5, 7.5, 7.5]


def test_collect_returns_locked_when_another_link_is_still_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )

    def reject_parallel_link(*_: object, **__: object) -> None:
        raise CollectionBatchBusyError(
            "PLID12345678 仍在检测；已阻止另一页面并发启动新链接"
        )

    monkeypatch.setattr(
        "takealot_ops.erp.web.CollectionBatchRegistry.start_link",
        reject_parallel_link,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={
                "url": "https://www.takealot.com/example/PLID87654321",
                "batch_id": "batch-1",
                "client_id": "client-1",
                "request_id": "request-2",
                "item_index": 1,
                "total_items": 2,
            },
        )

    assert response.status_code == 423
    assert "阻止另一页面并发" in response.json()["detail"]


def test_only_kxx_controls_batch_while_other_admin_can_add_and_prioritize(
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
        created_admin = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "username": "admin.two",
                "display_name": "Admin Two",
                "password": "operator-password-123",
                "role": "admin",
            },
        )
        assert created_admin.status_code == 200
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
        options = admin.post(
            "/api/competitors/batch-options",
            headers={"X-CSRF-Token": admin_csrf},
            json={"batch_id": "batch-admin", "visible_browser": True},
        )
        assert options.status_code == 200
        assert options.json()["status"]["visible_browser"] is True
        takeover = admin.post(
            "/api/competitors/batch-takeover",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
            },
        )
        assert takeover.status_code == 200
        assert takeover.json()["ready"] is True

    with TestClient(app, client=("192.168.1.8", 50001)) as other_admin:
        login = other_admin.post(
            "/api/auth/login",
            json={
                "username": "admin.two",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        operator_csrf = str(login.json()["csrf_token"])
        shared = other_admin.get("/api/competitors/batch-status")
        assert shared.status_code == 200
        assert shared.json()["active"] is True
        assert shared.json()["owner_username"] == "kxx"
        blocked = other_admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": operator_csrf},
            json={
                "batch_id": "batch-other-admin",
                "client_id": "client-other-admin",
                "event": "start",
                "completed": 0,
                "total": 3,
                "pending": 3,
            },
        )
        assert blocked.status_code == 403
        assert "仅限 kxx 账号" in blocked.json()["detail"]
        collect_blocked = other_admin.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": operator_csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert collect_blocked.status_code == 403
        created_target = other_admin.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": operator_csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert created_target.status_code == 200
        assert created_target.json()["queued_to_active_batch"] is True
        prioritized = other_admin.post(
            "/api/competitors/targets/12345678/prioritize",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert prioritized.status_code == 200
        stop_blocked = other_admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": operator_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
                "event": "manual_stop",
                "completed": 0,
                "total": 13,
                "pending": 13,
            },
        )
        assert stop_blocked.status_code == 403

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        admin_csrf = str(login.json()["csrf_token"])
        completed = admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
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


def test_collect_auto_adds_and_groups_new_offer_targets_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    origin_url = "https://www.takealot.com/example/PLID12345678"
    offer_url = "https://www.takealot.com/example-offer/PLID87654321"

    class OfferCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            return CompetitorCollectionResult(
                plid="12345678",
                title="Grouped product",
                succeeded=True,
                message="采集成功",
                discovered_targets=(
                    CompetitorDiscoveredTarget(
                        plid="12345678",
                        url=origin_url,
                        title="Grouped product",
                        seller_name="Seller One",
                        price=100.0,
                        selected=True,
                    ),
                    CompetitorDiscoveredTarget(
                        plid="87654321",
                        url=offer_url,
                        title="Grouped product",
                        seller_name="Seller Two",
                        price=110.0,
                        selected=False,
                    ),
                ),
            )

    database_path = tmp_path / "offer-targets.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorCollector", OfferCollector)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "event": "start",
                "completed": 0,
                "total": 1,
                "pending": 1,
            },
        )
        assert started.status_code == 200
        first = client.post(
            "/api/competitors/collect",
            headers=headers,
            json={
                "url": origin_url,
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "request_id": "offer-request-1",
                "item_index": 0,
                "total_items": 1,
            },
        )
        second = client.post(
            "/api/competitors/collect",
            headers=headers,
            json={
                "url": origin_url,
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "request_id": "offer-request-2",
                "item_index": 0,
                "total_items": 2,
            },
        )

        assert first.status_code == 200
        assert first.json()["added_target_count"] == 1
        assert "加入 1 条跟卖链接" in first.json()["message"]
        assert second.status_code == 200
        assert second.json()["added_target_count"] == 0
        listed = client.get("/api/competitors/targets").json()["items"]
        assert {item["plid"] for item in listed} == {"12345678", "87654321"}
        assert {item["offer_group_plid"] for item in listed} == {"12345678"}
        queued = client.get("/api/competitors/batch-status").json()["queued_targets"]
        assert [item["plid"] for item in queued] == ["87654321"]
        audits = client.get("/api/competitors/target-audits").json()["items"]
        assert [item["action"] for item in audits] == ["auto_discover"]


def test_competitor_target_crud_audit_and_active_batch_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    original_url = "https://www.takealot.com/example/PLID12345678"
    updated_url = f"{original_url}?variant=blue"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-1",
                "client_id": "client-1",
                "event": "start",
                "completed": 0,
                "total": 1,
                "pending": 1,
            },
        )
        assert started.status_code == 200

        created = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": original_url},
        )
        assert created.status_code == 200
        assert created.json()["item"]["plid"] == "12345678"
        assert created.json()["queued_to_active_batch"] is True

        shared = client.get("/api/competitors/batch-status").json()
        assert shared["total"] == 2
        assert shared["pending"] == 2
        assert shared["queued_targets"][0]["url"] == original_url
        prioritized = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
        )
        assert prioritized.status_code == 200
        priority_status = prioritized.json()["status"]
        assert priority_status["priority_targets"] == []
        assert priority_status["prioritized_targets"][0]["plid"] == "12345678"
        assert priority_status["prioritized_targets"][0]["source"] == "automatic"
        assert (
            priority_status["prioritized_targets"][0]["requested_by"]
            == "新增链接自动插队"
        )

        listed = client.get("/api/competitors/targets")
        assert listed.status_code == 200
        assert [item["plid"] for item in listed.json()["items"]] == ["12345678"]
        assert listed.json()["items"][0]["has_history"] is False

        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        with Session(engine) as database_session:
            database_session.add(
                CompetitorSnapshot(
                    plid="12345678",
                    collected_at=datetime.now(UTC),
                    url=original_url,
                    title="Example product",
                    image_url=None,
                    stock_quantity=None,
                    stock_exact=False,
                    stock_method="not_probed",
                    review_count=0,
                    fetched_review_count=0,
                    positive_reviews=0,
                    neutral_reviews=0,
                    negative_reviews=0,
                    lifetime_sales_min=0,
                    lifetime_sales_max=0,
                    trend_label="待建立基线",
                    trend_note="首次观测",
                )
            )
            database_session.commit()
        engine.dispose()
        assert client.get("/api/competitors/targets").json()["items"][0]["has_history"] is True

        duplicate = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": original_url},
        )
        assert duplicate.status_code == 409

        updated = client.patch(
            "/api/competitors/targets/12345678",
            headers=headers,
            json={"url": updated_url},
        )
        assert updated.status_code == 200
        assert updated.json()["item"]["url"] == updated_url
        changed_plid = client.patch(
            "/api/competitors/targets/12345678",
            headers=headers,
            json={"url": "https://www.takealot.com/other/PLID87654321"},
        )
        assert changed_plid.status_code == 422
        invalid_host = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://example.com/item/PLID87654321"},
        )
        assert invalid_host.status_code == 422

        deleted = client.delete(
            "/api/competitors/targets/12345678",
            headers=headers,
        )
        assert deleted.status_code == 200
        assert deleted.json()["history_retained"] is True
        assert client.get("/api/competitors/targets").json()["items"] == []

        audits = client.get("/api/competitors/target-audits")
        assert audits.status_code == 200
        audit_payload = audits.json()
        assert [item["action"] for item in audit_payload["items"]] == [
            "delete",
            "update",
            "add",
        ]
        assert all(item["actor_username"] == "kxx" for item in audit_payload["items"])
        available_date = audit_payload["date_range"]["available_start"]
        filtered = client.get(
            "/api/competitors/target-audits",
            params={"start_date": available_date, "end_date": available_date},
        )
        assert len(filtered.json()["items"]) == 3
        first_page = client.get(
            "/api/competitors/target-audits",
            params={
                "start_date": available_date,
                "end_date": available_date,
                "page": 1,
                "page_size": 2,
            },
        ).json()
        second_page = client.get(
            "/api/competitors/target-audits",
            params={
                "start_date": available_date,
                "end_date": available_date,
                "page": 2,
                "page_size": 2,
            },
        ).json()
        assert first_page["total"] == 3
        assert first_page["page"] == 1
        assert len(first_page["items"]) == 2
        assert second_page["page"] == 2
        assert len(second_page["items"]) == 1


def test_competitor_manual_retry_priority_is_audited_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-manual-retry.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    target_url = "https://www.takealot.com/example/PLID12345678"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        created = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": target_url},
        )
        assert created.status_code == 200
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-manual-retry",
                "client_id": "client-manual-retry",
                "event": "start",
                "completed": 1,
                "total": 2,
                "pending": 1,
                "failed": 1,
            },
        )
        assert started.status_code == 200

        retried = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
            json={"source": "manual_retry"},
        )
        duplicate = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
            json={"source": "manual_retry"},
        )

        assert retried.status_code == 200
        assert retried.json()["accepted"] is True
        assert duplicate.status_code == 200
        assert duplicate.json()["accepted"] is False
        status = retried.json()["status"]
        assert status["priority_targets"][0]["source"] == "manual_retry"
        assert status["prioritized_targets"][0]["source"] == "manual_retry"
        audits = client.get("/api/competitors/target-audits").json()["items"]
        assert [item["action"] for item in audits] == ["manual_retry", "add"]


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
            json={"username": "kxx", "password": "pass-123"},
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
        assert (
            client.post(
                "/api/auth/users",
                json={
                    "username": "operator.one",
                    "display_name": "Operator",
                    "password": "operator-password-1",
                    "role": "operator",
                },
            ).status_code
            == 403
        )

        response = client.patch(
            f"/api/auth/users/{admin_id}",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"role": "viewer"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "必须保留至少一个可管理用户权限的启用账号"


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


def test_operator_can_use_all_daily_report_reconciliation_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        admin_session = _bootstrap(client)
        _create_operator(
            client,
            str(admin_session["csrf_token"]),
            username="operator.daily",
        )
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                session.add_all(
                    [
                        OfferCurrent(
                            offer_id="offer-a",
                            sku="9900000000001",
                            title="Product A",
                            captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                            page_views_30_days=10,
                            takealot_available_stock=5,
                        ),
                        OfferCurrent(
                            offer_id="offer-b",
                            sku="9900000000002",
                            title="Product B",
                            captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                            page_views_30_days=12,
                            takealot_available_stock=7,
                        ),
                    ]
                )
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 24),
                    slot=slot,
                    captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator.daily",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "operator"
        csrf = str(login.json()["csrf_token"])

        report = client.get("/api/erp/daily-report?business_date=2026-07-24")
        assert report.status_code == 200
        assert report.headers["content-encoding"] == "gzip"
        assert report.json()["counts"]["ready"] == 2
        assert report.json()["capture_issue_range"]["selected_start"] == "2026-07-22"
        assert report.json()["capture_issue_range"]["selected_end"] == "2026-07-24"
        ranged_report = client.get(
            "/api/erp/daily-report",
            params={
                "business_date": "2026-07-24",
                "capture_start": "2026-07-24",
                "capture_end": "2026-07-24",
            },
        )
        assert ranged_report.status_code == 200
        assert ranged_report.json()["capture_issue_range"]["selected_start"] == (
            "2026-07-24"
        )
        inverted_range = client.get(
            "/api/erp/daily-report",
            params={
                "business_date": "2026-07-24",
                "capture_start": "2026-07-25",
                "capture_end": "2026-07-24",
            },
        )
        assert inverted_range.status_code == 422
        assert client.get("/api/erp/daily-report/reminders").status_code == 200
        assert client.get("/api/erp/daily-report/export?through=2026-07-24").status_code == 200
        noted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/note",
            headers={"X-CSRF-Token": csrf},
            json={
                "note": "运营员追加一条独立备注",
                "issue_type": "general",
            },
        )
        assert noted.status_code == 200
        noted_report = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert noted_report["items"][0]["operator_notes"][0]["note"] == ("运营员追加一条独立备注")
        note_id = noted_report["items"][0]["operator_notes"][0]["id"]
        updated = client.patch(
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": csrf},
            json={
                "note": "运营员修改后的通用备注",
                "issue_type": "general",
            },
        )
        assert updated.status_code == 200
        updated_note = client.get("/api/erp/daily-report?business_date=2026-07-24").json()["items"][
            0
        ]["operator_notes"][0]
        assert updated_note["note"] == "运营员修改后的通用备注"
        assert updated_note["issue_type"] == "general"
        assert updated_note["updated_by"] == "Operator Daily"
        deleted = client.request(
            "DELETE",
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        assert deleted.status_code == 200
        after_delete = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert after_delete["items"][0]["operator_notes"] == []
        assert after_delete["handled_actions"][0]["action_type"] == ("operator_note_deleted")
        assert after_delete["handled_actions"][0]["note"] is None
        assert after_delete["handled_actions"][0]["detail"]["deleted_note"] == (
            "运营员修改后的通用备注"
        )

        manual = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "ordered_units": 1,
                "reason": "platform_delay",
            },
        )
        assert manual.status_code == 200
        manual_report = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert manual_report["items"][0]["manual_note"] is None
        missing_confirm_note = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"source": "manual", "note": ""},
        )
        assert missing_confirm_note.status_code == 422
        missing_stock_note = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/stock-alert",
            headers={"X-CSRF-Token": csrf},
            json={"note": ""},
        )
        assert missing_stock_note.status_code == 422
        confirmed_manual = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"source": "manual", "note": "采用运营员复核后的人工值"},
        )
        assert confirmed_manual.status_code == 200

        confirmed = client.post(
            "/api/erp/daily-report/2026-07-24/confirm-ready",
            headers={"X-CSRF-Token": csrf},
            json={"note": "运营员确认其余早晚一致商品"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["confirmed"] == 1
        assert confirmed.json()["exported"] is True
        generated = client.post(
            "/api/erp/daily-report/export",
            headers={"X-CSRF-Token": csrf},
            json={"as_of": "2026-07-24"},
        )
        assert generated.status_code == 200
        download = client.get("/api/erp/daily-report/export/download?through=2026-07-24")
        assert download.status_code == 200
        assert (
            download.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        reverted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": csrf},
            json={"note": "运营员复核后发现需要重新选择版本"},
        )
        assert reverted.status_code == 200
        reopened = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert reopened["items"][0]["status"] == "needs_review"
        reopened_issue_types = {issue["type"] for issue in reopened["items"][0]["review_issues"]}
        assert reopened_issue_types == {"capture_difference"}
        assert reopened["items"][0]["confirmation_revert"]["reverted_by"] == ("Operator Daily")
        repeated_revert = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": csrf},
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
        admin_session = _bootstrap(client)
        _create_operator(
            client,
            str(admin_session["csrf_token"]),
            username="operator.stock",
        )
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
                    captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
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
                    captured_at=datetime(2026, 7, 26, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator.stock",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "operator"
        csrf = str(login.json()["csrf_token"])

        before = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert before["pending_actions"][0]["offer_id"] == "offer-stock"
        handled = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert",
            headers={"X-CSRF-Token": csrf},
            json={"note": "确认属于平台库存调整"},
        )
        assert handled.status_code == 200
        after = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert after["pending_actions"] == []
        assert after["items"][0]["stock_check"]["mismatch"] is True
        assert after["items"][0]["stock_check"]["dismissed"] is True
        assert after["handled_actions"][0]["active"] is True
        assert after["handled_actions"][0]["handled_by"] == "Operator Stock"

        reopened = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert/reopen",
            headers={"X-CSRF-Token": csrf},
            json={"note": "误操作，恢复待办"},
        )
        assert reopened.status_code == 200
        final = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert final["pending_actions"][0]["offer_id"] == "offer-stock"
        assert final["handled_actions"][0]["action_type"] == ("stock_alert_reopened")
        assert final["handled_actions"][0]["note"] == "误操作，恢复待办"
        original = next(
            row for row in final["handled_actions"] if row["action_type"] == "stock_difference"
        )
        assert original["active"] is False
        assert original["reversal"]["note"] == ("误操作，恢复待办")

        corrected = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "platform_stock": 9,
                "reason": "stock_adjustment",
                "note": "盘点后修正为连续库存9",
            },
        )
        assert corrected.status_code == 200
        corrected_payload = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert (
            corrected_payload["pending_actions"][0]["stock_check"]["resolution_action"]
            == "eliminate"
        )
        eliminated = client.post(
            ("/api/erp/daily-report/2026-07-25/offer-stock/stock-alert/eliminate"),
            headers={"X-CSRF-Token": csrf},
            json={"note": "采用修正库存并消除差异"},
        )
        assert eliminated.status_code == 200
        eliminated_payload = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert eliminated_payload["pending_actions"] == []
        assert eliminated_payload["items"][0]["stock_check"]["mismatch"] is False
        assert eliminated_payload["handled_actions"][0]["action_type"] == ("stock_eliminated")
