from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from takealot_ops.competitors.stock import (
    _add_main_product_to_cart,
    _dismiss_marketing_overlay,
    _find_main_add_to_cart_button,
    _find_product_quantity_combo,
    _probe_custom_quantity_with_retry,
    _select_quantity_option,
    _submit_custom_quantity,
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


def test_add_to_cart_waits_for_takealot_to_persist_the_request() -> None:
    page = Mock()
    button = page.locator.return_value.get_by_role.return_value
    button.count.return_value = 1
    button.is_visible.return_value = True

    _add_main_product_to_cart(page)

    button.click.assert_called_once_with()
    page.wait_for_timeout.assert_called_once_with(1500)


def test_marketing_overlay_cleanup_is_limited_to_braze_elements() -> None:
    page = Mock()

    _dismiss_marketing_overlay(page)

    script = page.evaluate.call_args.args[0]
    assert ".ab-iam-root, .ab-page-blocker" in script
    assert "ab-pause-scrolling" in script


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


def test_quantity_menu_waits_for_reopen_animation() -> None:
    page = Mock()
    combo = Mock()
    option = page.locator.return_value.filter.return_value
    option.count.side_effect = [0, 0, 1]
    option.is_visible.return_value = True

    _select_quantity_option(page, combo, 9)

    combo.click.assert_called_once_with()
    assert page.wait_for_timeout.call_count == 2
    option.click.assert_called_once_with()


def test_quantity_menu_retries_when_first_open_is_swallowed() -> None:
    page = Mock()
    combo = Mock()
    option = page.locator.return_value.filter.return_value
    option.count.side_effect = [0] * 8 + [1]
    option.is_visible.return_value = True

    _select_quantity_option(page, combo, 9)

    assert combo.click.call_count == 2
    page.keyboard.press.assert_called_once_with("Escape")
    option.click.assert_called_once_with()


def test_custom_quantity_accepts_closed_editor_with_matching_combo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    quantity_input = Mock()
    update_button = Mock()
    body = Mock()

    def locator(selector: str) -> Mock:
        if selector == (
            'input[name="quantity"]:not([aria-hidden="true"]):visible'
        ):
            return quantity_input
        if selector == "button:visible":
            return update_button
        if selector == "body":
            return body
        raise AssertionError(selector)

    page.locator.side_effect = locator
    quantity_input.count.return_value = 1
    quantity_input.input_value.return_value = ""
    update_button.filter.return_value = update_button
    update_button.count.return_value = 1
    body.inner_text.return_value = "Shopping Cart"
    combo.is_visible.return_value = True
    combo.inner_text.return_value = "Qty: 14"
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.time.monotonic",
        lambda: 0.0,
    )

    assert _submit_custom_quantity(page, combo, 14) == (True, None)
    quantity_input.fill.assert_called_once_with("14", force=True)


def test_custom_quantity_reloads_and_retries_transient_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    ensure = Mock(return_value=True)
    submit = Mock(side_effect=[(None, None), (True, None)])
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit,
    )

    assert _probe_custom_quantity_with_retry(page, combo, 20) == (
        True,
        None,
    )
    assert ensure.call_count == 2
    assert submit.call_count == 2
    page.reload.assert_called_once_with(wait_until="domcontentloaded")
    assert page.wait_for_timeout.call_args_list == [
        call(2000),
        call(1200),
    ]
