"""Equivalence checks for the radar's cheaper read projections."""

from datetime import date, timedelta
from decimal import Decimal
from random import Random

from takealot_ops.competitors.service import (
    OBSERVED_SALES_WINDOW_DAYS,
    _InventoryTurnoverObservation,
    _period_inventory_turnover,
    _recent_observed_sales_units,
)


def test_units_only_windows_match_full_turnover_with_missing_and_changing_scopes() -> None:
    random = Random(905)
    for _ in range(100):
        observations = [
            _InventoryTurnoverObservation(
                scope=(random.randrange(5),),
                stock_quantity=random.choice([None, 0, 0, 1, 8, 15]),
                stock_exact=random.choice([True, True, False]),
                price=random.choice([None, Decimal("12.99"), Decimal("0")]),
                display_date=(
                    date(2026, 9, 5) - timedelta(days=random.randrange(120))
                    if random.randrange(10) else None
                ),
            )
            for _ in range(random.randrange(100))
        ]
        # Preserve input order, including same-day observations, as the full
        # turnover calculator does; dates select membership, not ordering.
        values, through = _recent_observed_sales_units(observations)
        dated = [point for point in observations if point.display_date is not None]
        if not dated:
            assert through is None
            assert all(value is None for value in values.values())
            continue
        assert through == max(point.display_date for point in dated)
        for days in OBSERVED_SALES_WINDOW_DAYS:
            start = through - timedelta(days=days - 1)
            expected = _period_inventory_turnover([
                point for point in dated if start <= point.display_date <= through
            ]).sales_units
            assert values[str(days)] == expected
