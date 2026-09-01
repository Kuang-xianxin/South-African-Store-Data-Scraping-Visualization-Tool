from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from takealot_ops.platform_warehouse.portal import (
    PortalAmbiguousWriteError,
    PortalAuthenticationError,
    PortalDisabledError,
    PortalError,
    PortalSessionRegistry,
    TakealotPortalClient,
)
from takealot_ops.platform_warehouse.credentials import PortalCredential
from takealot_ops.platform_warehouse.service import PlatformWarehouseService
from takealot_ops.settings import TakealotPortalSettings
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import LogisticsProviderSnapshot, OfferCurrent
from takealot_ops.storage.store_context import store_scope


def _portal_settings() -> TakealotPortalSettings:
    return TakealotPortalSettings(
        enabled=True,
        base_url="https://seller-api.takealot.com",
        request_timeout_seconds=5,
        task_timeout_seconds=5,
        max_total_quantity=500,
        enabled_store_codes=frozenset({"current"}),
    )


def _mock_factory(handler):
    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url="https://seller-api.takealot.com",
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    return factory


def test_portal_client_uses_review_and_create_contract_without_retry() -> None:
    requests: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = __import__("json").loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/v2/shipment/shipments_review":
            return httpx.Response(200, json={"task_id": 41})
        if request.url.path == "/v2/task/41/status":
            return httpx.Response(200, json={"task_status_type_id": 4})
        if request.url.path == "/v2/task/41/shipment/download":
            return httpx.Response(
                200,
                json={"JHB": [{"offer_id": 235133257, "quantity": 5}]},
            )
        if request.url.path == "/v1/task/shipment":
            return httpx.Response(200, json={"task_id": 42})
        if request.url.path == "/v1/shipment/task/42/status":
            return httpx.Response(200, json={"task_status_type_id": 4})
        if request.url.path == "/v1/shipment/task/42/result":
            return httpx.Response(200, json={"success": True, "result": {}})
        raise AssertionError(request.url)

    client = TakealotPortalClient(
        _portal_settings(),
        client_factory=_mock_factory(handler),
        sleep=lambda _: None,
    )
    task_id, review = client.review_shipments(
        "token",
        [{"offer_id": 235133257, "region": "JHB", "quantity": 5}],
    )
    create_task_id, _ = client.create_replenishment(
        "token",
        {"shipment_summaries": [], "replenishment_list": []},
    )

    assert task_id == 41
    assert create_task_id == 42
    assert review["JHB"][0]["quantity"] == 5
    review_post = next(row for row in requests if row[1].endswith("shipments_review"))
    assert review_post[2] == {
        "data": {
            "shipment_items": [
                {"offer_id": 235133257, "region": "JHB", "quantity": 5}
            ]
        }
    }
    create_post = next(row for row in requests if row[1] == "/v1/task/shipment")
    assert create_post[2]["task_type_id"] == 22


def test_portal_client_limits_full_removal_module_reads_to_exact_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [], "total": 0})

    client = TakealotPortalClient(
        _portal_settings(),
        client_factory=_mock_factory(handler),
    )

    client.removal_orders(
        "token",
        "pickup_ready",
        page_number=2,
        page_size=100,
    )
    client.removal_order_items(
        "token",
        "closed",
        "88-safe",
        page_number=1,
        page_size=500,
    )

    assert requests[0].url.path == "/v2/removal_order/pickup_ready"
    assert "order_type_ids" not in requests[0].url.params
    assert requests[0].url.params["page_number"] == "2"
    assert requests[1].url.path == "/v2/removal_order/closed/88-safe/items"
    assert requests[1].url.params["page_size"] == "500"
    with pytest.raises(PortalError, match="removal_order_id"):
        client.removal_order_items(
            "token",
            "closed",
            "88/unsafe",
            page_number=1,
            page_size=100,
        )
    with pytest.raises(PortalError, match="状态路径"):
        client.removal_orders(  # type: ignore[arg-type]
            "token",
            "arbitrary",
            page_number=1,
            page_size=100,
        )


def test_portal_write_network_failure_is_ambiguous_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection dropped", request=request)

    client = TakealotPortalClient(
        _portal_settings(),
        client_factory=_mock_factory(handler),
        sleep=lambda _: None,
    )

    with pytest.raises(PortalAmbiguousWriteError, match="不会自动重试"):
        client.create_replenishment("token", {"shipment_summaries": []})
    assert calls == 1


class _FakePortalClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int | None]] = []
        self.login_count = 0
        self.whoami_failures = 0
        self.removal_reads: list[str] = []

    def login(self, email: str, password: str) -> dict[str, Any]:
        assert email == "seller@example.com"
        assert password == "secret"
        self.login_count += 1
        return {"api_key": "memory-only-token", "expires": 4_102_444_800}

    def whoami(self, token: str) -> dict[str, Any]:
        assert token == "memory-only-token"
        if self.whoami_failures:
            self.whoami_failures -= 1
            raise PortalAuthenticationError("expired")
        return {"account_id": 7, "email": "seller@example.com"}

    def logout(self, token: str) -> None:
        assert token == "memory-only-token"

    def verify_login_otp(self, session_id: str, otp: str) -> dict[str, Any]:
        raise AssertionError("OTP not required in this fixture")

    def review_shipments(
        self, token: str, shipment_items: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any]]:
        assert token == "memory-only-token"
        assert shipment_items == [
            {"offer_id": 235133257, "region": "JHB", "quantity": 5}
        ]
        return 101, {"JHB-DC": [{"offer_id": 235133257, "quantity": 5}]}

    def facilities(self, token: str) -> list[dict[str, Any]]:
        return [
            {
                "code": "JHB-DC",
                "enabled": True,
                "facility_id": 11,
                "region": {"code": "JHB", "region_id": 2},
            }
        ]

    def default_reference(self, token: str, facility_code: str) -> str:
        assert facility_code == "JHB-DC"
        return "ERP-JHB-001"

    def removal_orders(
        self,
        token: str,
        stage: str,
        *,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        assert token == "memory-only-token"
        assert page_number == 1
        assert page_size == 100
        self.removal_reads.append(stage)
        return {"results": [], "total": 0}

    def removal_order_items(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("empty removal-order fixture has no item calls")

    def create_replenishment(
        self, token: str, request_params: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        assert request_params["replenishment_list"] == [
            {
                "seller_listing_id": 235133257,
                "quantities_sending": [
                    {"facility_id": 11, "quantity": 5, "warehouse_id": 2}
                ],
            }
        ]
        self.writes.append(("create", None))
        return 102, {
            "success": True,
            "result": {
                "products_in_draft": [],
                "shipment_summaries": [
                    {
                        "shipment_id": 9001,
                        "quantity_added": 5,
                        "reference": "ERP-JHB-001",
                        "destination_warehouse": {"name": "JHB", "warehouse_id": 2},
                        "destination_facility": {
                            "facility_code": "JHB-DC",
                            "facility_id": 11,
                        },
                    }
                ],
            },
        }

    def confirm_preview(self, token: str, shipment_id: int) -> dict[str, Any]:
        return {"shipment_id": shipment_id, "result_string": "ready"}

    def confirm_po(
        self,
        token: str,
        shipment_id: int,
        *,
        my_soh_decrease_warehouse_id: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.writes.append(("confirm_po", shipment_id))
        return 103, {"success": True, "result": {"po_number": "PO-9001"}}

    def update_tracking(self, token: str, shipment_id: int, tracking_info: str) -> None:
        assert tracking_info == "TRACK-9001"
        self.writes.append(("tracking", shipment_id))

    def mark_shipped(self, token: str, shipment_id: int) -> None:
        self.writes.append(("shipped", shipment_id))

    def archive(self, token: str, shipment_id: int) -> None:
        self.writes.append(("archive", shipment_id))


class _FakeCredentialStore:
    def __init__(self, credential: PortalCredential | None) -> None:
        self.credential = credential

    def get(self, store_code: str) -> PortalCredential | None:
        assert store_code == "current"
        return self.credential

    def set(self, store_code: str, credential: PortalCredential) -> None:
        self.credential = credential

    def delete(self, store_code: str) -> bool:
        existed = self.credential is not None
        self.credential = None
        return existed


class _FakeTwoFactorPortalClient(_FakePortalClient):
    def __init__(self) -> None:
        super().__init__()
        self.verified = False

    def login(self, email: str, password: str) -> dict[str, Any]:
        assert email == "seller@example.com"
        assert password == "secret"
        return {
            "requires_2fa": True,
            "session_id": "otp-session-1",
            "destination": "s***@example.com",
        }

    def verify_login_otp(self, session_id: str, otp: str) -> dict[str, Any]:
        assert session_id == "otp-session-1"
        assert otp == "123456"
        self.verified = True
        return {"api_key": "memory-only-token", "expires": 4_102_444_800}


def test_guarded_service_review_create_and_manual_actions_use_exact_confirmations(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "guarded-platform-warehouse.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "current")
    monkeypatch.setenv("TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED", "true")
    engine = create_engine_for_database_url(database_url)
    try:
        create_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="235133257",
                    sku="SKU-1",
                    tsin_id="103996414",
                    title="Guarded product",
                    captured_at=datetime.now(UTC),
                )
            )
    finally:
        engine.dispose()

    fake = _FakePortalClient()
    registry = PortalSessionRegistry(fake)  # type: ignore[arg-type]
    service = PlatformWarehouseService(tmp_path, portal_registry=registry)
    status = service.portal_login("seller@example.com", "secret")
    assert status["authenticated"] is True

    draft = service.create_draft(
        [{"offer_id": "235133257", "jhb_quantity": 5}],
        actor_user_id=None,
        actor_username="operator",
    )
    reviewed = service.review_draft(
        draft["id"], actor_user_id=None, actor_username="operator"
    )
    created = service.create_upstream(
        draft["id"],
        approval_token=reviewed["approval_token"],
        confirmation_text=draft["draft_number"],
        actor_user_id=None,
        actor_username="operator",
    )["draft"]
    assert created["status"] == "platform_draft"
    assert created["shipments"][0]["shipment_id"] == 9001

    po_approval = service.prepare_shipment_action(9001, "confirm_po")
    po = service.execute_shipment_action(
        9001,
        "confirm_po",
        approval_token=po_approval["approval_token"],
        confirmation_text="9001",
        actor_user_id=None,
        actor_username="operator",
    )
    assert po["shipments"][0]["status"] == "po_confirmed"

    shipped_approval = service.prepare_shipment_action(9001, "confirm_shipped")
    service.execute_shipment_action(
        9001,
        "confirm_shipped",
        approval_token=shipped_approval["approval_token"],
        confirmation_text="9001",
        tracking_reference="TRACK-9001",
        actor_user_id=None,
        actor_username="operator",
    )
    archive_approval = service.prepare_shipment_action(9001, "archive")
    archived = service.execute_shipment_action(
        9001,
        "archive",
        approval_token=archive_approval["approval_token"],
        confirmation_text="9001",
        actor_user_id=None,
        actor_username="operator",
    )
    assert archived["status"] == "archived"
    assert fake.writes == [
        ("create", None),
        ("confirm_po", 9001),
        ("tracking", 9001),
        ("shipped", 9001),
        ("archive", 9001),
    ]


def test_direct_create_reuses_valid_session_and_request_id(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "direct-platform-warehouse.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "current")
    engine = create_engine_for_database_url(database_url)
    try:
        create_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="235133257",
                    sku="SKU-1",
                    tsin_id="103996414",
                    title="Direct product",
                    captured_at=datetime.now(UTC),
                )
            )
    finally:
        engine.dispose()

    fake = _FakePortalClient()
    service = PlatformWarehouseService(
        tmp_path,
        portal_registry=PortalSessionRegistry(fake),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(
            PortalCredential("seller@example.com", "secret")
        ),
    )
    request_id = "6b5c9d09-cc2d-48ad-b1ab-4ec79ca650b2"
    first = service.create_platform_draft_direct(
        [{"offer_id": "235133257", "jhb_quantity": 5}],
        client_request_id=request_id,
        actor_user_id=None,
        actor_username="operator",
    )
    repeated = service.create_platform_draft_direct(
        [{"offer_id": "235133257", "jhb_quantity": 5}],
        client_request_id=request_id,
        actor_user_id=None,
        actor_username="operator",
    )

    assert first["state"] == "created"
    assert first["draft"]["status"] == "platform_draft"
    assert repeated["draft"]["id"] == first["draft"]["id"]
    assert fake.writes == [("create", None)]


def test_direct_create_reauthenticates_when_memory_session_is_rejected(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "direct-platform-warehouse-reauth.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "current")
    engine = create_engine_for_database_url(database_url)
    try:
        create_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="235133257",
                    sku="SKU-1",
                    tsin_id="103996414",
                    title="Reauth product",
                    captured_at=datetime.now(UTC),
                )
            )
    finally:
        engine.dispose()

    fake = _FakePortalClient()
    service = PlatformWarehouseService(
        tmp_path,
        portal_registry=PortalSessionRegistry(fake),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(
            PortalCredential("seller@example.com", "secret")
        ),
    )
    service.portal_login("seller@example.com", "secret")
    fake.whoami_failures = 1

    created = service.create_platform_draft_direct(
        [{"offer_id": "235133257", "jhb_quantity": 5}],
        client_request_id="6fe60ad5-d937-4c9b-a8b0-2a515f52d366",
        actor_user_id=None,
        actor_username="operator",
    )

    assert created["state"] == "created"
    assert fake.login_count == 2
    assert fake.writes == [("create", None)]


def test_direct_create_pauses_for_2fa_then_resumes_same_draft(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "direct-platform-warehouse-2fa.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "current")
    engine = create_engine_for_database_url(database_url)
    try:
        create_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="235133257",
                    sku="SKU-1",
                    tsin_id="103996414",
                    title="2FA product",
                    captured_at=datetime.now(UTC),
                )
            )
    finally:
        engine.dispose()

    fake = _FakeTwoFactorPortalClient()
    service = PlatformWarehouseService(
        tmp_path,
        portal_registry=PortalSessionRegistry(fake),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(
            PortalCredential("seller@example.com", "secret")
        ),
    )
    pending = service.create_platform_draft_direct(
        [{"offer_id": "235133257", "jhb_quantity": 5}],
        client_request_id="14cd0b2a-ef48-429c-8a6e-09d04f23fba4",
        actor_user_id=None,
        actor_username="operator",
    )
    assert pending["state"] == "need_2fa"
    assert pending["draft"]["status"] == "awaiting_2fa"
    assert pending["otp_destination"] == "s***@example.com"
    assert fake.writes == []

    created = service.verify_otp_and_continue_create(
        pending["draft"]["id"],
        "123456",
        actor_user_id=None,
        actor_username="operator",
    )
    assert fake.verified is True
    assert created["state"] == "created"
    assert created["draft"]["id"] == pending["draft"]["id"]
    assert created["draft"]["status"] == "platform_draft"
    assert fake.writes == [("create", None)]


def test_portal_store_allowlist_blocks_other_stores_before_login(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "store-03")
    fake = _FakePortalClient()
    service = PlatformWarehouseService(
        tmp_path,
        portal_registry=PortalSessionRegistry(fake),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(None),
    )

    assert service.portal_status()["enabled"] is False
    assert service.portal_status()["globally_enabled"] is True
    with pytest.raises(PortalDisabledError, match="current 未启用约平台仓"):
        service.portal_login("seller@example.com", "secret")
    assert fake.login_count == 0

    with store_scope("store-03"):
        assert service.portal_settings.is_store_enabled() is True
        assert service.portal_login("seller@example.com", "secret")["authenticated"] is True
    assert fake.login_count == 1


def test_removal_order_sync_authenticates_reads_and_persists_safe_snapshot(
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'removal-sync.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "true")
    monkeypatch.setenv("TAKEALOT_PORTAL_ENABLED_STORES", "current")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    engine.dispose()
    fake = _FakePortalClient()
    service = PlatformWarehouseService(
        tmp_path,
        portal_registry=PortalSessionRegistry(fake),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(
            PortalCredential("seller@example.com", "secret")
        ),
    )

    with store_scope("current"):
        result = service.sync_return_removal_orders()

    assert result["state"] == "synced"
    assert result["order_count"] == 0
    assert fake.removal_reads == ["submitted", "pickup_ready", "closed"]
    engine = create_engine_for_database_url(database_url)
    try:
        with store_scope("current"), Session(engine) as session:
            snapshot = session.get(
                LogisticsProviderSnapshot,
                ("current", "takealot_removal_orders"),
            )
        assert snapshot is not None
        assert snapshot.payload["connected"] is True
        assert snapshot.payload["order_type_filter"] == "All"
        assert snapshot.payload["order_type_ids"] == [1, 2, 3]
    finally:
        engine.dispose()
