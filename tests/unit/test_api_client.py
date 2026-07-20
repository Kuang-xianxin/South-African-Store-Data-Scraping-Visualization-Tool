from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import ApiResponseError, AuthenticationError
from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.settings import Settings


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
API_KEY = "fixture-api-key"


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

    assert [item["offer_id"] for item in items] == ["offer-1", "offer-2"]
    assert [request.url.params.get("continuation_token") for request in requests] == [None, "next-page"]


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


def test_403_raises_authentication_error_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, json={"detail": f"Key {API_KEY} is invalid"})

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
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=_fixture("offers_page_2.json"))

    client = _client(handler, sleep=sleeps.append)
    try:
        offers = list(client.list_offers())
    finally:
        client.close()

    assert isinstance(offers[0], OfferRecord)
    assert calls == 2
    assert sleeps == [7.0]


def test_500_retries_three_times_then_raises() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"detail": f"failure for {API_KEY}"})

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
