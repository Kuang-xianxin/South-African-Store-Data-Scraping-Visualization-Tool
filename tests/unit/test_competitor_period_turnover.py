from datetime import date
from decimal import Decimal

from takealot_ops.competitors.service import (
    _InventoryTurnoverObservation,
    _period_inventory_turnover,
    _recent_observed_sales_units,
)


def _observation(
    stock: int | None,
    price: str | None,
    *,
    exact: bool = True,
    scope: str = "same-offer",
    display_date: date | None = None,
) -> _InventoryTurnoverObservation:
    return _InventoryTurnoverObservation(
        scope=(scope,),
        stock_quantity=stock,
        stock_exact=exact,
        price=Decimal(price) if price is not None else None,
        display_date=display_date,
    )


def test_period_turnover_accumulates_decreases_and_replenishment_at_later_price() -> None:
    result = _period_inventory_turnover(
        [
            _observation(10, "100.00"),
            _observation(6, "90.00"),
            _observation(12, "80.00"),
            _observation(9, "70.00"),
        ]
    )

    assert result.sales_units == 7
    assert result.sales_amount == 570.0
    assert result.replenishment_units == 6
    assert result.replenishment_value == 480.0
    assert result.turnover_value == 1050.0


def test_period_turnover_stays_unavailable_without_a_comparable_exact_pair() -> None:
    missing_exact = _period_inventory_turnover(
        [_observation(10, "100.00"), _observation(None, "90.00", exact=False)]
    )
    changed_scope = _period_inventory_turnover(
        [_observation(10, "100.00"), _observation(8, "90.00", scope="other-offer")]
    )

    assert missing_exact.sales_units is None
    assert missing_exact.sales_amount is None
    assert missing_exact.turnover_value is None
    assert changed_scope.sales_units is None
    assert changed_scope.sales_amount is None
    assert changed_scope.turnover_value is None


def test_period_turnover_can_skip_a_bounded_foreign_scope_gap() -> None:
    result = _period_inventory_turnover(
        [
            _observation(11, "782.00"),
            _observation(9, "783.00", scope="temporary-other-offer"),
            _observation(9, "782.00"),
            _observation(13, "782.00"),
            _observation(11, "782.00"),
        ],
    )
    changed_endpoint = _period_inventory_turnover(
        [
            _observation(11, "782.00"),
            _observation(9, "783.00", scope="other-offer"),
        ],
    )

    assert result.sales_units == 4
    assert result.sales_amount == 3128.0
    assert result.replenishment_units == 4
    assert result.replenishment_value == 3128.0
    assert result.turnover_value == 6256.0
    assert changed_endpoint.sales_units is None
    assert changed_endpoint.replenishment_units is None


def test_period_turnover_skips_inexact_points_and_singleton_scopes() -> None:
    result = _period_inventory_turnover(
        [
            _observation(10, "100.00", scope="offer-a"),
            _observation(500, "90.00", exact=False, scope="offer-a"),
            _observation(20, "50.00", scope="offer-b"),
            _observation(99, "10.00", scope="singleton-offer"),
            _observation(15, "40.00", scope="offer-b"),
            _observation(7, "80.00", scope="offer-a"),
        ]
    )

    assert result.sales_units == 8
    assert result.sales_amount == 440.0
    assert result.replenishment_units == 0
    assert result.replenishment_value == 0.0
    assert result.turnover_value == 440.0


def test_period_turnover_keeps_units_but_withholds_amount_when_movement_price_is_missing() -> None:
    result = _period_inventory_turnover(
        [_observation(10, "100.00"), _observation(7, None)]
    )

    assert result.sales_units == 3
    assert result.sales_amount is None
    assert result.replenishment_units == 0
    assert result.replenishment_value == 0.0
    assert result.turnover_value is None


def test_recent_observed_sales_windows_include_both_dates_and_share_latest_anchor() -> None:
    observations = [
        _observation(300, "100", display_date=date(2026, 5, 1)),
        _observation(280, "100", display_date=date(2026, 6, 1)),
        _observation(320, "100", display_date=date(2026, 7, 1)),
        _observation(270, "100", display_date=date(2026, 8, 1)),
        _observation(300, "100", display_date=date(2026, 8, 10)),
        _observation(290, "100", display_date=date(2026, 8, 15)),
        _observation(280, "100", display_date=date(2026, 8, 22)),
        _observation(270, "100", display_date=date(2026, 8, 23)),
        _observation(260, "100", display_date=date(2026, 8, 29)),
    ]

    windows, through_date = _recent_observed_sales_units(observations)

    assert through_date == date(2026, 8, 29)
    assert windows == {"7": 10, "15": 30, "30": 40, "60": 90, "90": 90}
