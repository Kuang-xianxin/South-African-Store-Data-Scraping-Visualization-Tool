from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from takealot_ops.exchange_rates import (
    ExchangeRateProvider,
    ExchangeRateQuote,
    ExchangeRateUnavailable,
)
from takealot_ops.profitability import (
    ProfitabilityOffer,
    ProfitabilitySalesLine,
    build_own_store_profitability_payload,
)


class _FixedRateService:
    def latest(self) -> ExchangeRateQuote:
        return ExchangeRateQuote(
            rate=Decimal("2"),
            rate_date=date(2026, 8, 17),
            fetched_at=datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
        )


def _offer(
    *,
    cost_rmb: Decimal | None = Decimal("100"),
    selling_price_zar: Decimal | None = Decimal("300"),
    rrp_zar: Decimal | None = Decimal("400"),
    fee_lines: tuple[ProfitabilitySalesLine, ...] | None = None,
    covered_days: int = 30,
) -> ProfitabilityOffer:
    return ProfitabilityOffer(
        store_code="current",
        store_name="Alpha Store",
        offer_id="OFFER-1",
        plid="12345678",
        sku="SKU-1",
        company_sku="COMP-1",
        company_product_name="Product One",
        cost_rmb=cost_rmb,
        cost_effective_date=date(2026, 8, 15),
        selling_price_zar=selling_price_zar,
        rrp_zar=rrp_zar,
        fee_covered_days=covered_days,
        fee_lines=fee_lines
        if fee_lines is not None
        else (
            ProfitabilitySalesLine(
                sales_day=date(2026, 8, 16),
                selling_price=Decimal("200"),
                total_fees=Decimal("40"),
                quantity=1,
            ),
            ProfitabilitySalesLine(
                sales_day=date(2026, 8, 17),
                selling_price=Decimal("300"),
                total_fees=Decimal("60"),
                quantity=2,
            ),
        ),
    )


def _payload(
    offer: ProfitabilityOffer,
    rate_service: ExchangeRateProvider | None = None,
):
    return build_own_store_profitability_payload(
        [offer],
        rate_service=rate_service or _FixedRateService(),
        store_codes={"current"},
        fee_window_start=date(2026, 7, 19),
        fee_window_end=date(2026, 8, 17),
        fee_window_days=30,
    )


def test_builds_multiple_rmb_profit_scenarios_with_both_margin_definitions() -> None:
    payload = _payload(_offer())
    item = payload["items"][0]

    assert item["offer_key"] == "seller-api:current:OFFER-1"
    assert item["cost_rmb"] == 100.0
    assert item["cost_zar"] == 200.0
    assert item["fee_basis"] == {
        "window_days": 30,
        "covered_days": 30,
        "sales_days": 2,
        "order_line_count": 2,
        "ordered_units": 3,
        "sales_revenue_zar": 500.0,
        "total_fees_zar": 100.0,
        "fee_rate_percentage": 20.0,
        "source": "Takealot Seller Sales /sales",
        "status": "available",
        "invalid_line_count": 0,
        "message": (
            "综合费率 = 已验证 Seller Sales 行 total_fees 合计 / "
            "selling_price 合计；total_fees 已包含库存调拨费。"
        ),
    }
    assert item["scenarios"]["current_gross"] == {
        "key": "current_gross",
        "label": "当前售价毛利润",
        "price_zar": 300.0,
        "price_rmb": 150.0,
        "estimated_fees_zar": 0.0,
        "estimated_fees_rmb": 0.0,
        "profit_rmb": 50.0,
        "profit_margin_percentage": 33.33,
        "cost_markup_percentage": 50.0,
        "note": "当前单件售价换算成人民币后减当前单件成本；未扣平台及履约费用。",
    }
    assert item["scenarios"]["current_fee_adjusted"]["profit_rmb"] == 20.0
    assert (
        item["scenarios"]["current_fee_adjusted"]["profit_margin_percentage"]
        == 13.33
    )
    assert item["scenarios"]["current_fee_adjusted"]["cost_markup_percentage"] == 20.0
    assert item["scenarios"]["current_fee_adjusted"]["estimated_fees_rmb"] == 30.0
    assert (
        item["scenarios"]["current_fee_adjusted"]["label"]
        == "当前售价平台扣费后利润（估算）"
    )
    assert item["scenarios"]["rrp_gross"]["profit_rmb"] == 100.0
    assert item["scenarios"]["rrp_gross"]["profit_margin_percentage"] == 50.0


def test_negative_profit_keeps_negative_margin_and_markup() -> None:
    item = _payload(_offer(cost_rmb=Decimal("200")))["items"][0]

    assert item["scenarios"]["current_gross"]["profit_rmb"] == -50.0
    assert item["scenarios"]["current_gross"]["profit_margin_percentage"] == -33.33
    assert item["scenarios"]["current_gross"]["cost_markup_percentage"] == -25.0
    assert item["scenarios"]["current_fee_adjusted"]["profit_rmb"] == -80.0


def test_missing_cost_does_not_request_exchange_rate_or_invent_profit() -> None:
    class _UnexpectedRateService:
        def latest(self) -> ExchangeRateQuote:
            raise AssertionError("missing cost must not request an exchange rate")

    item = _payload(_offer(cost_rmb=None), _UnexpectedRateService())["items"][0]

    assert item["cost_rmb"] is None
    assert item["cost_zar"] is None
    assert item["scenarios"] == {
        "current_gross": None,
        "current_fee_adjusted": None,
        "rrp_gross": None,
    }


def test_unavailable_rate_keeps_cost_but_omits_all_converted_profit() -> None:
    class _UnavailableRateService:
        def latest(self) -> ExchangeRateQuote:
            raise ExchangeRateUnavailable("offline")

    payload = _payload(_offer(), _UnavailableRateService())
    item = payload["items"][0]

    assert payload["exchange_rate"]["status"] == "unavailable"
    assert item["cost_rmb"] == 100.0
    assert item["cost_zar"] is None
    assert item["scenarios"] == {
        "current_gross": None,
        "current_fee_adjusted": None,
        "rrp_gross": None,
    }


def test_incomplete_fee_sample_never_uses_partial_rows_for_fee_profit() -> None:
    item = _payload(
        _offer(
            fee_lines=(
                ProfitabilitySalesLine(
                    sales_day=date(2026, 8, 17),
                    selling_price=Decimal("300"),
                    total_fees=None,
                    quantity=1,
                ),
            )
        )
    )["items"][0]

    assert item["fee_basis"]["status"] == "incomplete"
    assert item["fee_basis"]["invalid_line_count"] == 1
    assert item["scenarios"]["current_gross"]["profit_rmb"] == 50.0
    assert item["scenarios"]["current_fee_adjusted"] is None


def test_verified_window_without_offer_sales_discloses_no_fee_estimate() -> None:
    item = _payload(_offer(fee_lines=()))["items"][0]

    assert item["fee_basis"]["status"] == "no_sales"
    assert item["fee_basis"]["covered_days"] == 30
    assert item["scenarios"]["current_fee_adjusted"] is None
