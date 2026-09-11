"""Own-store quote prices must not treat unpriced Offers as free products."""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from takealot_ops.competitors.own_store import ConnectedStoreOfferPoint
from takealot_ops.competitors.service import (
    _store_baseline_history_row,
    _store_snapshot_rows,
)


def point(store: str, price: str | None, stock: int, day: int = 10):
    return ConnectedStoreOfferPoint(
        id=day, store_code=store, display_date=date(2026, 9, day),
        offer_id=f"offer-{store}", productline_id="12345678", sku=f"sku-{store}",
        title="Hand massager", image_url=None,
        selling_price=Decimal(price) if price is not None else None,
        status="buyable" if stock else "disabled_by_seller", total_stock=stock,
        takealot_available_stock=stock, seller_available_stock=0,
        captured_at=datetime(2026, 9, day, 2), source_kind="observation",
    )


def card(points):
    return _store_snapshot_rows(
        points, [], all_follower_snapshots=[], current_store_offers=[],
        selected_start_date=None, selected_end_date=None,
        store_names_by_code={}, own_offer_ids_by_plid={}, own_skus_by_plid={},
        variants_by_snapshot={}, follower_timelines={}, store_tsin_by_offer={},
        variant_labels_by_scope={},
    )[0]


@pytest.mark.parametrize("unpriced", ["0", "-1", None])
def test_unpriced_second_store_cannot_override_valid_card_or_detail_price(unpriced):
    active = point("current", "520", 1)
    inactive = point("other", unpriced, 0)
    row = card([active, inactive])
    assert row["价格"] == 520
    assert row["库存数量"] == 1
    quotes = {item["卖家ID"]: item for item in row["对比报价"]}
    assert quotes["current"]["价格"] == row["价格"]
    assert quotes["other"]["价格"] is None
    assert quotes["other"]["库存数量"] == 0
    assert len(row["自有报价"]) == 2
    assert next(item for item in row["自有报价"] if item["offer_id"] == "offer-other")["价格"] is None
    # Read projection must retain the original source evidence.
    assert inactive.selling_price == (Decimal(unpriced) if unpriced else None)


@pytest.mark.parametrize("price", ["0", "-1", None])
def test_only_unpriced_quotes_remain_unknown_in_card_and_history(price):
    source = point("current", price, 0)
    row = card([source])
    assert row["价格"] is None
    assert row["库存数量"] == 0
    history = _store_baseline_history_row(
        "12345678", [source], store_names_by_code={}, store_tsin_by_offer={},
        variant_labels_by_scope={},
    )
    assert history["价格"] is None
    assert history["对比报价"][0]["价格"] is None


def test_unpriced_baseline_does_not_invent_price_increase():
    before = point("current", "0", 2, 9)
    after = point("current", "520", 1)
    row = card([before, after])
    assert row["价格"] == 520
    assert row["区间起始价格"] is None
    assert row["价格变化"] is None
    assert row["对比报价"][0]["价格变化"] is None


def test_valid_prices_keep_minimum_and_ordinary_price_changes():
    before = point("current", "540", 2, 9)
    after = point("current", "520", 1)
    other = replace(point("other", "600", 0), id=11)
    row = card([before, after, point("other", "600", 0, 9), other])
    assert row["价格"] == 520
    assert row["区间起始价格"] == 540
    assert row["价格变化"] == -20
    assert row["周期销售件数"] == 1
    assert row["库存数量"] == 1
