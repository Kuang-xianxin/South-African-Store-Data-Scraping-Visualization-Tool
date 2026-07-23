from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import ApiResponseError, AuthenticationError, RateLimitError
from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.settings import Settings


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
API_KEY = "fixture-api-key"
COLLECTION_ENVELOPE_FIELDS = {"items", "limit", "continuation_token"}
DOCUMENTED_DEFAULT_OFFER_FIELDS = {
    "offer_id",
    "tsin_id",
    "sku",
    "barcode",
    "product_label",
    "selling_price",
    "rrp",
    "minimum_leadtime_days",
    "status",
    "title",
    "discount_percentage",
    "storage_fee_eligible",
    "created_at",
    "updated_at",
    "affected_by_vacation",
    "disabled_by_seller",
    "disabled_by_takealot",
    "conversion_percentage_30_days",
    "conversion_percentage_previous_30_days",
    "page_views_30_days",
    "quantity_returned_30_days",
    "condition",
    "image_url",
    "productline_id",
    "width_cm",
    "length_cm",
    "height_cm",
    "weight_grams",
    "is_conveyable",
    "benchmark_price",
    "total_wishlist",
    "wishlist_30_days",
    "listing_quality",
}
DOCUMENTED_SALE_FIELDS = {
    "order_item_id",
    "order_id",
    "order_date",
    "sale_status",
    "offer_id",
    "tsin_id",
    "sku",
    "selling_price",
    "quantity",
    "success_fee",
    "fulfillment_fee",
    "courier_collection_fee",
    "total_fees",
    "stock_transfer_fee",
    "sales_region",
    "stock_source_region",
}


def _settings() -> Settings:
    return Settings(
        project_root=Path("."),
        api_key=API_KEY,
        base_url="https://api.example.test/v1",
        database_url="sqlite:///data/takealot.db",
        request_timeout_seconds=30.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _client(
    handler: Callable[[httpx.Request], httpx.Response], sleep: Callable[[float], None] | None = None
) -> TakealotClient:
    return TakealotClient(_settings(), transport=httpx.MockTransport(handler), sleep=sleep)


def _assert_exception_drops_api_key_references(error: BaseException) -> None:
    assert API_KEY not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert API_KEY not in "".join(traceback.format_exception(error))


def _assert_documented_collection_envelope(payload: dict[str, Any]) -> None:
    assert set(payload) == COLLECTION_ENVELOPE_FIELDS
    assert isinstance(payload["items"], list)
    assert type(payload["limit"]) is int
    assert isinstance(payload["continuation_token"], str)


def test_offer_fixtures_match_documented_collection_schema() -> None:
    integer_fields = {
        "offer_id",
        "tsin_id",
        "selling_price",
        "rrp",
        "minimum_leadtime_days",
        "page_views_30_days",
        "quantity_returned_30_days",
        "productline_id",
        "benchmark_price",
        "total_wishlist",
        "wishlist_30_days",
        "listing_quality",
    }
    number_fields = {
        "discount_percentage",
        "conversion_percentage_30_days",
        "conversion_percentage_previous_30_days",
        "width_cm",
        "length_cm",
        "height_cm",
        "weight_grams",
    }
    boolean_fields = {
        "storage_fee_eligible",
        "affected_by_vacation",
        "disabled_by_seller",
        "disabled_by_takealot",
        "is_conveyable",
    }
    string_fields = {
        "sku",
        "barcode",
        "product_label",
        "status",
        "title",
        "created_at",
        "updated_at",
        "condition",
        "image_url",
    }

    for fixture_name in ("offers_page_1.json", "offers_page_2.json"):
        page = _fixture(fixture_name)
        _assert_documented_collection_envelope(page)
        offer = page["items"][0]
        assert set(offer) == DOCUMENTED_DEFAULT_OFFER_FIELDS
        assert all(type(offer[field]) is int for field in integer_fields)
        assert all(
            isinstance(offer[field], (int, float)) and not isinstance(offer[field], bool)
            for field in number_fields
        )
        assert all(type(offer[field]) is bool for field in boolean_fields)
        assert all(isinstance(offer[field], str) for field in string_fields)


def test_default_offer_fixtures_omit_unrequested_expands() -> None:
    expanded_fields = {
        "seller_warehouse_stock",
        "takealot_warehouse_stock",
        "offer_charges",
        "replenishment_blocks",
    }

    for fixture_name in ("offers_page_1.json", "offers_page_2.json"):
        offer = _fixture(fixture_name)["items"][0]
        assert expanded_fields.isdisjoint(offer)


def test_sales_fixture_matches_documented_collection_schema() -> None:
    page = _fixture("sales_page.json")
    _assert_documented_collection_envelope(page)
    sale = page["items"][0]

    assert set(sale) == DOCUMENTED_SALE_FIELDS
    assert all(
        type(sale[field]) is int
        for field in ("order_item_id", "order_id", "offer_id", "tsin_id", "selling_price", "quantity")
    )
    assert all(
        isinstance(sale[field], (int, float)) and not isinstance(sale[field], bool)
        for field in (
            "success_fee",
            "fulfillment_fee",
            "courier_collection_fee",
            "total_fees",
            "stock_transfer_fee",
        )
    )
    assert all(
        isinstance(sale[field], str)
        for field in (
            "order_date",
            "sale_status",
            "sku",
            "sales_region",
            "stock_source_region",
        )
    )


def test_continuation_page_fixture_omits_count_without_include_count() -> None:
    page = _fixture("offers_page_1.json")

    assert page["continuation_token"].strip()
    assert "count" not in page


def test_client_sends_api_key_header_without_putting_it_in_url() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json=_fixture("offers_page_2.json"))

    client = _client(handler)
    try:
        list(client.list_offers())
    finally:
        client.close()

    assert captured_requests[0].headers["X-API-Key"] == API_KEY
    assert API_KEY not in str(captured_requests[0].url)


def test_list_offers_requests_seller_and_takealot_stock_expands() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"items": []})

    client = _client(handler)
    try:
        assert list(client.list_offers()) == []
    finally:
        client.close()

    assert captured_requests[0].url.params.get_list("expands") == [
        "seller_warehouse_stock",
        "takealot_warehouse_stock",
    ]


def test_iter_items_follows_continuation_token_until_empty() -> None:
    requests: list[httpx.Request] = []
    pages = [_fixture("offers_page_1.json"), _fixture("offers_page_2.json")]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = _client(handler)
    try:
        items = list(client.iter_items("/offers", {"limit": 100}))
    finally:
        client.close()

    assert [item["offer_id"] for item in items] == [100001, 100002]
    assert [request.url.params.get("continuation_token") for request in requests] == [
        None,
        "eyJvZmZzZXQiOiAxMDB9",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {"items": [], "continuation_token": ""},
        {"items": [], "continuation_token": "   "},
    ],
    ids=["absent", "empty", "whitespace"],
)
def test_iter_items_stops_when_continuation_token_is_absent_or_blank(
    payload: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = _client(handler)
    try:
        assert list(client.iter_items("/offers", {"limit": 100})) == []
    finally:
        client.close()

    assert len(requests) == 1


@pytest.mark.parametrize("continuation_token", [None, 0, False, []])
def test_iter_items_rejects_present_non_string_continuation_token(
    continuation_token: object,
) -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"items": [], "continuation_token": continuation_token},
        )
    )
    try:
        with pytest.raises(ApiResponseError, match="continuation_token"):
            list(client.iter_items("/offers", {"limit": 100}))
    finally:
        client.close()


def test_sales_query_uses_inclusive_sast_dates_and_limit_100() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture("sales_page.json"))

    client = _client(handler)
    try:
        sales = list(client.list_sales(date(2026, 7, 1), date(2026, 7, 20)))
    finally:
        client.close()

    assert isinstance(sales[0], SaleRecord)
    assert dict(requests[0].url.params) == {
        "order_date__gte": "2026-07-01",
        "order_date__lte": "2026-07-20",
        "limit": "100",
    }


def test_returns_query_uses_inclusive_dates_and_limit_100() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    client = _client(handler)
    try:
        assert list(client.list_returns(date(2026, 7, 1), date(2026, 7, 20))) == []
    finally:
        client.close()

    assert dict(requests[0].url.params) == {
        "return_date__gte": "2026-07-01",
        "return_date__lte": "2026-07-20",
        "limit": "100",
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json={"items": ["not-an-object"]}),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"not-json"),
    ],
    ids=["missing-items", "non-object-item", "non-object-payload", "invalid-json"],
)
def test_client_rejects_malformed_collection_payloads(response: httpx.Response) -> None:
    client = _client(lambda request: response)
    try:
        with pytest.raises(ApiResponseError):
            list(client.iter_items("/offers", {"limit": 100}))
    finally:
        client.close()


def test_429_exhaustion_raises_typed_rate_limit_error() -> None:
    client = _client(
        lambda request: httpx.Response(429, json={"title": "rate limited"}),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(RateLimitError):
            list(client.list_offers())
    finally:
        client.close()


def test_non_retryable_http_error_raises_api_response_error() -> None:
    client = _client(lambda request: httpx.Response(404, json={"title": "missing"}))
    try:
        with pytest.raises(ApiResponseError):
            list(client.list_offers())
    finally:
        client.close()


def test_403_raises_authentication_error_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403,
            json={
                "errors": [
                    {
                        "status": 403,
                        "title": f"Forbidden for key {API_KEY}",
                    }
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(AuthenticationError) as error:
            list(client.list_offers())
    finally:
        client.close()

    assert len(requests) == 1
    assert API_KEY not in str(error.value)


def test_401_raises_authentication_error_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            401,
            json={
                "errors": [
                    {
                        "status": 401,
                        "title": f"Unauthorized for key {API_KEY}",
                    }
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(AuthenticationError) as error:
            list(client.list_offers())
    finally:
        client.close()

    assert len(requests) == 1
    assert API_KEY not in str(error.value)


def test_429_uses_retry_after_then_retries() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"errors": [{"status": 429, "title": "Too Many Requests"}]},
            )
        return httpx.Response(200, json=_fixture("offers_page_2.json"))

    client = _client(handler, sleep=sleeps.append)
    try:
        offers = list(client.list_offers())
    finally:
        client.close()

    assert isinstance(offers[0], OfferRecord)
    assert offers[0].created_at is not None
    assert offers[0].created_at.isoformat() == "2026-02-15T12:34:56+02:00"
    assert calls == 2
    assert sleeps == [7.0]


def test_500_retries_three_times_then_raises() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"title": f"failure for {API_KEY}"})

    client = _client(handler, sleep=sleeps.append)
    try:
        with pytest.raises(ApiResponseError) as error:
            list(client.list_offers())
    finally:
        client.close()

    assert calls == 4
    assert sleeps == [2.0, 5.0, 15.0]
    assert API_KEY not in str(error.value)


def test_typed_record_conversion_error_redacts_api_key() -> None:
    payload = _fixture("sales_page.json")
    payload["items"][0]["quantity"] = API_KEY

    client = _client(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ApiResponseError) as error:
            list(client.list_sales(date(2026, 7, 1), date(2026, 7, 20)))
    finally:
        client.close()

    assert API_KEY not in str(error.value)


def test_typed_record_conversion_error_does_not_leak_api_key_through_exception_chain() -> None:
    payload = _fixture("sales_page.json")
    payload["items"][0]["quantity"] = API_KEY

    client = _client(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ApiResponseError) as error:
            list(client.list_sales(date(2026, 7, 1), date(2026, 7, 20)))
    finally:
        client.close()

    _assert_exception_drops_api_key_references(error.value)


def test_transport_error_does_not_leak_api_key_through_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"transport failed for key {API_KEY}", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ApiResponseError) as error:
            list(client.list_offers())
    finally:
        client.close()

    _assert_exception_drops_api_key_references(error.value)


def test_client_rejects_non_get_requests() -> None:
    client = _client(lambda request: httpx.Response(200, json={"items": []}))
    try:
        with pytest.raises(ValueError, match="GET"):
            client._request("POST", "/offers", {})
    finally:
        client.close()

    public_methods = {
        name
        for name, value in vars(TakealotClient).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {"iter_items", "list_offers", "list_sales", "list_returns", "close"}
