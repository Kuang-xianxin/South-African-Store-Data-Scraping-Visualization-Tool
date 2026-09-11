from takealot_ops.competitors.own_store_sales import summarize_own_store_sales_windows


def test_official_total_includes_orders_older_than_ninety_days_and_retains_coverage() -> None:
    values = summarize_own_store_sales_windows({
        "listing_date": "2026-01-01",
        "through_date": "2026-09-07",
        "partial_days": 1,
        "missing_days": 240,
        "points": [
            {"date": "2026-01-01", "ordered_units": 100},
            {"date": "2026-09-06", "ordered_units": 3},
            {"date": "2026-09-07", "ordered_units": None},
            {"date": "2026-09-08", "ordered_units": 500},
        ],
    })
    assert values["total"] == 103
    assert values["90"] == 3
    assert values["total_partial_days"] == 1
    assert values["total_missing_days"] == 240


def test_official_total_does_not_turn_missing_orders_into_zero() -> None:
    series = {
        "listing_date": "2026-09-01",
        "through_date": "2026-09-07",
        "points": [{"date": "2026-09-01", "ordered_units": None}],
    }
    assert summarize_own_store_sales_windows(series)["total"] is None
    series["points"] = [{"date": "2026-09-01", "ordered_units": 0}]
    assert summarize_own_store_sales_windows(series)["total"] == 0
