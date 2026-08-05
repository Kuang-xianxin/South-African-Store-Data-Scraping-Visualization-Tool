from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.logistics.links import (
    build_high_confidence_candidates,
    build_logistics_candidates,
    confirm_candidate_link,
    list_confirmed_links,
    revoke_confirmed_link,
)
from takealot_ops.logistics.service import LogisticsOverviewService
from takealot_ops.logistics.w8 import W8ApiError, W8Client
from takealot_ops.settings import W8Settings
from takealot_ops.storage.migrations import create_engine_for_database_url, create_schema
from takealot_ops.storage.models import (
    LogisticsProviderSnapshot,
    LogisticsShipmentLinkAudit,
    OfferCurrent,
)
from takealot_ops.storage.store_context import store_scope


def _w8_response(data: Any, *, code: int = 0, message: str = "success") -> httpx.Response:
    return httpx.Response(200, json={"code": code, "msg": message, "data": data})


def test_w8_client_sends_token_in_header_and_only_allows_query_paths(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _w8_response([{"houseId": 1, "houseCode": "CRZA"}])

    client = W8Client(
        W8Settings(tmp_path, "secret-token", "https://crgyl.w8soft.net/prod-api/w8", 10),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.warehouses()[0]["houseCode"] == "CRZA"
        with pytest.raises(ValueError, match="只读"):
            client._post("/commonApi/dropshipping/createOrder", {})
    finally:
        client.close()

    assert requests[0].method == "POST"
    assert requests[0].headers["token"] == "secret-token"
    assert "secret-token" not in str(requests[0].url)


def test_w8_client_redacts_token_from_business_error(tmp_path: Path) -> None:
    token = "do-not-leak-this-token"
    client = W8Client(
        W8Settings(tmp_path, token, "https://crgyl.w8soft.net/prod-api/w8", 10),
        transport=httpx.MockTransport(
            lambda request: _w8_response(None, code=401, message=f"invalid {token}")
        ),
    )
    try:
        with pytest.raises(W8ApiError) as error:
            client.warehouses()
    finally:
        client.close()

    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_logistics_overview_aggregates_both_read_only_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("W8_API_TOKEN", "fixture-w8-token")
    monkeypatch.setenv("W8_BASE_URL", "https://crgyl.w8soft.net/prod-api/w8")
    monkeypatch.setenv("TAKEALOT_API_KEY", "fixture-takealot-key")
    monkeypatch.setenv("TAKEALOT_BASE_URL", "https://marketplace-api.takealot.com/v1")
    database_url = f"sqlite:///{(tmp_path / 'logistics.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-a",
                sku="SKU-A",
                captured_at=datetime(2026, 7, 31, tzinfo=UTC),
            )
        )
    engine.dispose()
    request_counts: Counter[str] = Counter()

    def w8_handler(request: httpx.Request) -> httpx.Response:
        request_counts[request.url.path] += 1
        path = request.url.path
        body = json.loads(request.content)
        if path.endswith("/commonApi/inner/getHouseList"):
            return _w8_response(
                [
                    {
                        "houseId": 7,
                        "houseCode": "CRZA",
                        "houseCnname": "南非仓",
                        "hrcountry": "ZA",
                    }
                ]
            )
        if path.endswith("/commonApi/inner/getChannelList"):
            assert body["houseCode"] == "CRZA"
            assert body["type"] == "1"
            return _w8_response([{"channelCode": "EXP", "channelName": "快递"}])
        if path.endswith("/queryProducts"):
            return _w8_response({"records": [{"sku": "A"}], "total": 399})
        if path.endswith("/queryStocks"):
            return _w8_response(
                {
                    "records": [
                        {
                            "stockNum": 12,
                            "usableStockNum": 8,
                            "lockNum": 4,
                            "outboundNum": 2,
                            "transitNum": 3,
                            "defectiveNum": 1,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/queryInBoundOrders"):
            return _w8_response(
                {
                    "records": [
                        {
                            "orderNo": "IB-001",
                            "statusName": "已上架",
                            "createDateStr": "2026-07-30 10:00:00",
                            "shelfDateStr": "2026-07-31 10:00:00",
                            "headwayNo": "TRACK12345",
                            "shippingMark": "MARK-01",
                            "skuTypeCount": 2,
                            "skuForecastTotalNum": 12,
                            "items": [{"sku": "SKU-A", "forecastNum": 12}],
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/queryOutboundOrders"):
            return _w8_response(
                {
                    "records": [
                        {
                            "orderNo": "OB-001",
                            "statusName": "已出库",
                            "createDateStr": "2026-07-31 11:00:00",
                            "waybillNo": "WB-001",
                            "logisticTypeName": "快递",
                            "skuTypeCount": 1,
                            "totalQty": 3,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/queryReBoundOrders"):
            return _w8_response({"records": [{"statusName": "已退仓"}], "total": 2})
        raise AssertionError(f"unexpected W8 path: {path}")

    def takealot_handler(request: httpx.Request) -> httpx.Response:
        request_counts[request.url.path] += 1
        assert request.url.path.endswith("/shipments")
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "shipment_id": 88,
                        "reference": "July replenishment",
                        "purchase_order_number": "PO-88",
                        "destination_region": "CPT",
                        "purchase_order_state": "Open",
                        "shipment_type": "replenishment",
                        "shipped": True,
                        "cancelled": False,
                        "created_at": "2026-07-31T08:00:00Z",
                        "date_unloaded": "2026-08-01T08:00:00Z",
                        "tracking_info": "Carrier TRACK12345",
                        "shipment_items": [
                            {
                                "offer_id": "offer-a",
                                "quantity_sending": 12,
                                "purchase_order_quantity_received": 11,
                                "purchase_order_quantity_damaged": 1,
                            }
                        ],
                    }
                ]
            },
        )

    service = LogisticsOverviewService(
        tmp_path,
        cache_ttl_seconds=60,
        w8_transport=httpx.MockTransport(w8_handler),
        takealot_transport=httpx.MockTransport(takealot_handler),
    )
    payload = service.load(force=True)

    assert payload["w8"]["connected"] is True
    assert payload["w8"]["warehouse"]["code"] == "CRZA"
    assert payload["w8"]["summary"] == {
        "products": 399,
        "stock_records": 1,
        "stock_total": 12,
        "usable_stock": 8,
        "locked_stock": 4,
        "outbound_allocated": 2,
        "transit_stock": 3,
        "defective_stock": 1,
        "inbound_orders": 1,
        "outbound_orders": 1,
        "returned_records": 1,
    }
    assert "total=2" in payload["w8"]["warnings"][0]
    assert payload["takealot"]["summary"]["quantity_received"] == 11
    assert payload["takealot"]["summary"]["quantity_damaged"] == 1
    assert payload["matching"]["direct_match_count"] == 1
    assert payload["matching"]["items"][0]["takealot_shipment_id"] == 88
    assert payload["matching"]["high_confidence_candidate_count"] == 0
    assert payload["matching"]["medium_confidence_candidate_count"] == 0
    assert payload["matching"]["low_confidence_candidate_count"] == 0
    assert payload["matching"]["split_batch_group_count"] == 0
    assert payload["matching"]["confirmed_link_count"] == 0
    assert payload["matching"]["warnings"] == []
    assert payload["w8"]["data_source"] == "live_api"
    assert payload["takealot"]["data_source"] == "live_api"
    assert payload["w8"]["snapshot_saved"] is True
    assert payload["takealot"]["snapshot_saved"] is True

    engine = create_engine_for_database_url(database_url)
    try:
        with Session(engine) as session:
            snapshots = session.scalars(
                select(LogisticsProviderSnapshot).order_by(
                    LogisticsProviderSnapshot.provider
                )
            ).all()
        assert [snapshot.provider for snapshot in snapshots] == ["takealot", "w8"]
        assert snapshots[1].payload["_raw_inbound"][0]["orderNo"] == "IB-001"
        assert snapshots[0].payload["_raw_shipments"][0]["shipment_id"] == 88
    finally:
        engine.dispose()

    first_request_count = request_counts.total()
    forced_too_soon = service.load(force=True)
    assert request_counts.total() == first_request_count
    assert forced_too_soon["cache_age_seconds"] >= 0
    cached = service.load()
    assert request_counts.total() == first_request_count
    assert cached["cache_age_seconds"] == 0
    assert cached["automatic_page_refresh"] is False
    assert cached["w8"]["data_source"] == "local_database"
    assert cached["takealot"]["data_source"] == "local_database"
    assert cached["w8"]["refresh_attempted"] is False
    assert cached["takealot"]["refresh_attempted"] is False

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture provider offline", request=request)

    offline_service = LogisticsOverviewService(
        tmp_path,
        cache_ttl_seconds=0,
        w8_transport=httpx.MockTransport(unavailable),
        takealot_transport=httpx.MockTransport(unavailable),
    )
    offline = offline_service.load(force=True)
    assert offline["w8"]["connected"] is True
    assert offline["w8"]["live_connected"] is False
    assert offline["w8"]["data_source"] == "local_database"
    assert offline["w8"]["summary"]["inbound_orders"] == 1
    assert offline["takealot"]["connected"] is True
    assert offline["takealot"]["live_connected"] is False
    assert offline["takealot"]["data_source"] == "local_database"
    assert offline["takealot"]["summary"]["shipments"] == 1
    assert offline["matching"]["direct_match_count"] == 1


def test_logistics_cache_and_worker_credentials_are_isolated_per_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'multi-store-logistics.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TAKEALOT_STORES",
        "current|VoltTech ZA|STORE_KEY_1;store-02|VeldBox|STORE_KEY_2",
    )
    monkeypatch.setenv("STORE_KEY_1", "key-one")
    monkeypatch.setenv("STORE_KEY_2", "key-two")
    monkeypatch.setenv("TAKEALOT_BASE_URL", "https://marketplace-api.takealot.com/v1")
    monkeypatch.delenv("W8_API_TOKEN", raising=False)
    seen_keys: list[str] = []

    def takealot_handler(request: httpx.Request) -> httpx.Response:
        api_key = request.headers["x-api-key"]
        seen_keys.append(api_key)
        count = 1 if api_key == "key-one" else 2
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "shipment_id": index,
                        "purchase_order_number": f"{api_key}-PO-{index}",
                        "created_at": f"2026-08-0{index}T08:00:00+02:00",
                        "shipment_items": [],
                    }
                    for index in range(1, count + 1)
                ]
            },
        )

    service = LogisticsOverviewService(
        tmp_path,
        cache_ttl_seconds=60,
        takealot_transport=httpx.MockTransport(takealot_handler),
    )
    with store_scope("current"):
        current_payload = service.load(force=True)
    with store_scope("store-02"):
        second_payload = service.load(force=True)
    with store_scope("current"):
        current_cached = service.load()
    service._invalidate_cache()
    with store_scope("current"):
        current_after_invalidation = service.load()

    assert current_payload["takealot"]["summary"]["shipments"] == 1
    assert second_payload["takealot"]["summary"]["shipments"] == 2
    assert current_cached["takealot"]["summary"]["shipments"] == 1
    assert current_after_invalidation["takealot"]["summary"]["shipments"] == 1
    assert current_after_invalidation["takealot"]["data_source"] == "local_database"
    assert seen_keys == ["key-one", "key-two"]

    engine = create_engine_for_database_url(database_url)
    try:
        with store_scope("current"), Session(engine) as session:
            current_snapshot = session.get(
                LogisticsProviderSnapshot,
                {"store_code": "current", "provider": "takealot"},
            )
        with store_scope("store-02"), Session(engine) as session:
            second_snapshot = session.get(
                LogisticsProviderSnapshot,
                {"store_code": "store-02", "provider": "takealot"},
            )
        assert current_snapshot is not None
        assert second_snapshot is not None
        assert current_snapshot.payload["summary"]["shipments"] == 1
        assert second_snapshot.payload["summary"]["shipments"] == 2
    finally:
        engine.dispose()


def test_high_confidence_candidates_require_exact_mutually_unique_sku_quantities() -> None:
    inbound = [
        {
            "orderNo": "CR260716002374",
            "createDateStr": "2026-07-16 15:52:06",
            "headwayNo": "HEAD-1",
            "shippingMark": "MARK-1",
            "statusName": "已上架",
            "items": [{"sku": "SELLER-SKU", "forecastNum": 100}],
        }
    ]
    shipment = {
        "shipment_id": 8434254,
        "purchase_order_number": "182828696",
        "reference": "PO-REFERENCE",
        "purchase_order_state": "received_full_quantity",
        "created_at": "2026-07-28T07:10:39+02:00",
        "shipment_items": [{"offer_id": "offer-1", "quantity_sending": 100}],
    }

    candidates = build_high_confidence_candidates(
        inbound,
        [shipment],
        {"offer-1": "seller-sku"},
    )

    assert len(candidates) == 1
    assert candidates[0] == {
        "confidence": "high",
        "method": "完整卖家SKU及各SKU发送数量一致，双方候选唯一，建单日期相差不超过30天",
        "w8_order_no": "CR260716002374",
        "w8_headway_no": "HEAD-1",
        "w8_shipping_mark": "MARK-1",
        "w8_status": "已上架",
        "w8_created_at": "2026-07-16 15:52:06",
        "takealot_shipment_id": 8434254,
        "takealot_purchase_order_number": "182828696",
        "takealot_reference": "PO-REFERENCE",
        "takealot_state": "received_full_quantity",
        "takealot_created_at": "2026-07-28T07:10:39+02:00",
        "sku_lines": 1,
        "w8_sku_lines": 1,
        "takealot_sku_lines": 1,
        "shared_sku_lines": 1,
        "overlap_ratio": 1.0,
        "quantity": 100,
        "w8_quantity": 100,
        "takealot_quantity": 100,
        "quantity_delta": 0,
        "date_gap_days": 12,
        "w8_candidate_count": 1,
        "takealot_candidate_count": 1,
        "ambiguous": False,
    }
    assert build_high_confidence_candidates(
        inbound,
        [shipment, {**shipment, "shipment_id": 8434255}],
        {"offer-1": "seller-sku"},
    ) == []
    assert build_high_confidence_candidates(
        inbound,
        [
            {
                **shipment,
                "shipment_items": [{"offer_id": "offer-1", "quantity_sending": 99}],
            }
        ],
        {"offer-1": "seller-sku"},
    ) == []


def test_candidate_tiers_include_quantity_mismatch_partial_overlap_and_split_groups() -> None:
    inbound = [
        {
            "orderNo": "W8-100",
            "createDateStr": "2026-07-01 10:00:00",
            "items": [
                {"sku": "SKU-A", "forecastNum": 60},
                {"sku": "SKU-B", "forecastNum": 40},
            ],
        }
    ]
    shipments = [
        {
            "shipment_id": 1,
            "created_at": "2026-07-10T08:00:00+02:00",
            "shipment_items": [
                {"offer_id": "offer-a", "quantity_sending": 30},
                {"offer_id": "offer-b", "quantity_sending": 20},
            ],
        },
        {
            "shipment_id": 2,
            "created_at": "2026-07-12T08:00:00+02:00",
            "shipment_items": [
                {"offer_id": "offer-a", "quantity_sending": 30},
                {"offer_id": "offer-b", "quantity_sending": 20},
            ],
        },
        {
            "shipment_id": 3,
            "created_at": "2026-07-13T08:00:00+02:00",
            "shipment_items": [{"offer_id": "offer-a", "quantity_sending": 10}],
        },
    ]

    tiers = build_logistics_candidates(
        inbound,
        shipments,
        {"offer-a": "sku-a", "offer-b": "sku-b"},
    )

    assert tiers["high"] == []
    assert [item["takealot_shipment_id"] for item in tiers["medium"]] == [1, 2]
    assert tiers["medium"][0]["quantity_delta"] == -50
    assert tiers["medium"][0]["ambiguous"] is True
    assert [item["takealot_shipment_id"] for item in tiers["low"]] == [3]
    assert tiers["low"][0]["overlap_ratio"] == 0.5
    assert tiers["split_groups"][0]["takealot_shipment_ids"] == [1, 2]


def test_confirmed_logistics_links_are_idempotent_revocable_and_audited(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'links.db').as_posix()}"
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    candidate = {
        "w8_order_no": "CR260716002374",
        "takealot_shipment_id": 8434254,
        "takealot_purchase_order_number": "182828696",
        "takealot_reference": "PO-REFERENCE",
        "sku_lines": 1,
        "quantity": 100,
        "date_gap_days": 12,
    }
    try:
        confirmed = confirm_candidate_link(
            engine,
            candidate,
            actor_user_id=None,
            actor_username="operator.one",
        )
        repeated = confirm_candidate_link(
            engine,
            candidate,
            actor_user_id=None,
            actor_username="operator.one",
        )
        assert confirmed["id"] == repeated["id"]
        assert list_confirmed_links(engine)[0]["quantity"] == 100

        revoked = revoke_confirmed_link(
            engine,
            confirmed["id"],
            actor_user_id=None,
            actor_username="operator.one",
            note="货件关系核对错误",
        )
        assert revoked["active"] is False
        assert list_confirmed_links(engine) == []

        reconfirmed = confirm_candidate_link(
            engine,
            candidate,
            actor_user_id=None,
            actor_username="operator.two",
        )
        assert reconfirmed["id"] == confirmed["id"]
        with Session(engine) as session:
            actions = session.scalars(
                select(LogisticsShipmentLinkAudit.action).order_by(
                    LogisticsShipmentLinkAudit.id
                )
            ).all()
        assert actions == ["confirmed", "revoked", "reconfirmed"]
    finally:
        engine.dispose()
