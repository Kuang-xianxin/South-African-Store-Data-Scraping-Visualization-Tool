from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from takealot_ops.logistics.service import LogisticsOverviewService
from takealot_ops.logistics.w8 import W8ApiError, W8Client
from takealot_ops.settings import W8Settings


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

    first_request_count = request_counts.total()
    forced_too_soon = service.load(force=True)
    assert request_counts.total() == first_request_count
    assert forced_too_soon["cache_age_seconds"] >= 0
    cached = service.load()
    assert request_counts.total() == first_request_count
    assert cached["cache_age_seconds"] >= 0
