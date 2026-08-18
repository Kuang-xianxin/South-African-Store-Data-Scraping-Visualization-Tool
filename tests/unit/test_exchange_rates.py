from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from takealot_ops.exchange_rates import (
    CnyZarRateService,
    ExchangeRateQuote,
    ExchangeRateUnavailable,
    product_cost_conversion_payload,
)


def test_latest_cny_zar_rate_uses_the_fixed_provider_and_one_hour_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "date": "2026-08-17",
                "base": "CNY",
                "quote": "ZAR",
                "rate": 2.3971,
            },
        )

    service = CnyZarRateService(
        transport=httpx.MockTransport(handler),
        now=lambda: datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
    )
    try:
        first = service.latest()
        second = service.latest()
    finally:
        service.close()

    assert first == second
    assert first.rate == Decimal("2.3971")
    assert first.rate_date.isoformat() == "2026-08-17"
    assert first.stale is False
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(
        "https://api.frankfurter.dev/v2/rate/CNY/ZAR"
    )


def test_latest_rate_discloses_stale_cache_after_refresh_failure() -> None:
    current_time = [datetime(2026, 8, 18, 2, 30, tzinfo=UTC)]
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "date": "2026-08-17",
                    "base": "CNY",
                    "quote": "ZAR",
                    "rate": 2.3971,
                },
            )
        return httpx.Response(503, json={"error": "unavailable"})

    service = CnyZarRateService(
        transport=httpx.MockTransport(handler),
        now=lambda: current_time[0],
    )
    try:
        service.latest()
        current_time[0] += timedelta(hours=2)
        stale = service.latest()
        cached_stale = service.latest()
    finally:
        service.close()

    assert stale.stale is True
    assert cached_stale.stale is True
    assert stale.rate == Decimal("2.3971")
    assert attempts == 2


def test_latest_rate_rejects_a_mismatched_currency_pair() -> None:
    service = CnyZarRateService(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "date": "2026-08-17",
                    "base": "USD",
                    "quote": "ZAR",
                    "rate": 17.5,
                },
            )
        ),
        now=lambda: datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
    )
    try:
        with pytest.raises(ExchangeRateUnavailable, match="基础币种不一致"):
            service.latest()
    finally:
        service.close()


class _FixedRateService:
    def latest(self) -> ExchangeRateQuote:
        return ExchangeRateQuote(
            rate=Decimal("2.3971"),
            rate_date=datetime(2026, 8, 17, tzinfo=UTC).date(),
            fetched_at=datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
        )


def test_product_cost_conversion_rounds_to_zar_cents_and_keeps_evidence() -> None:
    payload = product_cost_conversion_payload(Decimal("268.7917"), _FixedRateService())

    assert payload == {
        "base_currency": "CNY",
        "quote_currency": "ZAR",
        "cost_rmb": 268.7917,
        "cost_zar": 644.32,
        "rate": 2.3971,
        "rate_date": "2026-08-17",
        "fetched_at": "2026-08-18T02:30:00+00:00",
        "source": "Frankfurter 机构参考汇率",
        "status": "converted",
        "message": "按最新发布的机构参考汇率换算，仅供成本估算，非交易结算价。",
    }


def test_product_cost_conversion_does_not_fetch_when_rmb_cost_is_missing() -> None:
    class UnexpectedRateService:
        def latest(self) -> ExchangeRateQuote:
            raise AssertionError("missing cost must not trigger an external request")

    payload = product_cost_conversion_payload(None, UnexpectedRateService())

    assert payload["status"] == "missing_cost"
    assert payload["cost_zar"] is None
    assert payload["rate"] is None
