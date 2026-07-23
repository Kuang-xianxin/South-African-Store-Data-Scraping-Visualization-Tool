from __future__ import annotations

from unittest.mock import Mock

import pytest

from takealot_ops.competitors.stock import (
    _find_main_add_to_cart_button,
    _find_product_quantity_combo,
    _url_matches_plid,
)


def test_target_url_requires_the_requested_plid() -> None:
    assert _url_matches_plid(
        "https://www.takealot.com/product/PLID72189176?size=Right",
        "72189176",
    )
    assert not _url_matches_plid(
        "https://www.takealot.com/recommendation/PLID91577928",
        "72189176",
    )


def test_main_cart_button_is_scoped_to_the_product_buy_box() -> None:
    page = Mock()
    buy_box = Mock()
    button = Mock()
    page.locator.return_value = buy_box
    buy_box.get_by_role.return_value = button
    button.count.return_value = 1
    button.is_visible.return_value = True

    selected = _find_main_add_to_cart_button(page)

    assert selected is button
    page.locator.assert_called_once_with("main aside")
    buy_box.get_by_role.assert_called_once_with(
        "button",
        name="Add to Cart",
        exact=True,
    )


def test_main_cart_button_rejects_missing_or_ambiguous_buy_box() -> None:
    page = Mock()
    button = page.locator.return_value.get_by_role.return_value
    button.count.return_value = 2

    with pytest.raises(RuntimeError, match="主购买区"):
        _find_main_add_to_cart_button(page)


def test_cart_never_falls_back_to_an_unrelated_single_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    product_link = page.locator.return_value
    product_link.count.return_value = 0
    times = iter((0.0, 0.0, 16.0))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.time.monotonic",
        lambda: next(times),
    )

    with pytest.raises(RuntimeError, match="未找到目标竞品 PLID72189176"):
        _find_product_quantity_combo(page, "72189176")

    page.locator.assert_called_once_with(
        'a[href*="/PLID72189176"]:visible'
    )
