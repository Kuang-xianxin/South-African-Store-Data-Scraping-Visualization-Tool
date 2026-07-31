from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from takealot_ops.competitors.stock import (
    _add_main_product_to_cart,
    _choose_quantity,
    _dismiss_marketing_overlay,
    _find_main_add_to_cart_button,
    _find_product_quantity_combo,
    _open_quantity_menu_with_retry,
    _parse_customer_purchase_limit,
    _probe_above_quick_menu,
    _probe_custom_quantity_with_retry,
    _read_visible_numeric_quantity_options,
    _select_quantity_option,
    _stock_probe_failure_note,
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


def test_stock_probe_failure_note_records_stage_and_network_code() -> None:
    error = RuntimeError(
        "Page.goto: net::ERR_CONNECTION_CLOSED at https://www.takealot.com/cart\n"
        "Call log:\n  - navigating to cart"
    )

    assert _stock_probe_failure_note("打开购物车", error) == (
        "打开购物车失败：连接在页面加载时被关闭（ERR_CONNECTION_CLOSED）"
    )


@pytest.mark.asyncio
async def test_initial_quantity_menu_waits_for_animated_numeric_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    page.keyboard.press = AsyncMock()
    combo = Mock()
    combo.click = AsyncMock()
    find_combo = AsyncMock(return_value=combo)
    read_options = AsyncMock(side_effect=[[], [1, 2, 9]])
    dismiss_overlay = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_product_quantity_combo",
        find_combo,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._read_visible_numeric_quantity_options",
        read_options,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        dismiss_overlay,
    )

    selected_combo, options = await _open_quantity_menu_with_retry(page, "95565512")

    assert selected_combo is combo
    assert options == [1, 2, 9]
    find_combo.assert_awaited_once_with(page, "95565512")
    combo.click.assert_awaited_once_with()
    assert read_options.await_count == 2
    page.reload.assert_not_called()


@pytest.mark.asyncio
async def test_initial_quantity_menu_reloads_and_reidentifies_same_plid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.reload = AsyncMock()
    first_combo = Mock()
    first_combo.click = AsyncMock()
    refreshed_combo = Mock()
    refreshed_combo.click = AsyncMock()
    find_combo = AsyncMock(side_effect=[first_combo, refreshed_combo])
    read_options = AsyncMock(side_effect=[*([[]] * 24), [1, 2, 9]])
    dismiss_overlay = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_product_quantity_combo",
        find_combo,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._read_visible_numeric_quantity_options",
        read_options,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        dismiss_overlay,
    )

    selected_combo, options = await _open_quantity_menu_with_retry(page, "95565512")

    assert selected_combo is refreshed_combo
    assert options == [1, 2, 9]
    assert find_combo.await_args_list == [
        call(page, "95565512"),
        call(page, "95565512"),
    ]
    assert first_combo.click.await_count == 3
    refreshed_combo.click.assert_awaited_once_with()
    page.reload.assert_awaited_once_with(
        wait_until="domcontentloaded",
        timeout=45_000,
    )


@pytest.mark.asyncio
async def test_visible_numeric_quantity_options_ignore_ten_plus() -> None:
    page = Mock()
    options = page.locator.return_value
    options.count = AsyncMock(return_value=3)
    options.nth.side_effect = [
        Mock(inner_text=AsyncMock(return_value="1")),
        Mock(inner_text=AsyncMock(return_value="9")),
        Mock(inner_text=AsyncMock(return_value="10+")),
    ]

    assert await _read_visible_numeric_quantity_options(page) == [1, 9]
    page.locator.assert_called_once_with('[role="option"]:visible')


@pytest.mark.asyncio
async def test_quantity_choice_stops_on_late_explicit_warehouse_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    page = Mock()
    body = Mock()
    combo = Mock()
    warning = (
        "You've attempted to order more stock than currently available at our "
        "warehouse (current stock = 4). The products will need to be ordered "
        "from our supplier."
    )
    body.inner_text = AsyncMock(side_effect=["Shopping Cart", "Shopping Cart", warning])
    page.locator.return_value = body

    async def advance(_: int) -> None:
        clock[0] += 0.3

    page.wait_for_timeout = AsyncMock(side_effect=advance)
    combo.inner_text = AsyncMock(return_value="Qty: 9")
    select_quantity = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._select_quantity_option",
        select_quantity,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    assert await _choose_quantity(page, combo, 9) == (False, 4)
    select_quantity.assert_awaited_once_with(page, combo, 9)
    assert page.wait_for_timeout.await_count == 2


@pytest.mark.asyncio
async def test_explicit_warehouse_warning_skips_all_followup_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    submit_probe = AsyncMock(return_value=(False, 4, None))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_custom_quantity_with_retry",
        submit_probe,
    )

    assert await _probe_above_quick_menu(page, combo) == (4, True, None)
    submit_probe.assert_awaited_once_with(page, combo, 10)


@pytest.mark.asyncio
async def test_rejected_quantity_ten_confirms_exact_stock_of_nine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    submit_probe = AsyncMock(return_value=(False, None, None))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_custom_quantity_with_retry",
        submit_probe,
    )

    assert await _probe_above_quick_menu(page, combo) == (9, True, None)
    submit_probe.assert_awaited_once_with(page, combo, 10)
    page.wait_for_timeout.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_ten_plus_editor_without_warning_remains_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    submit_probe = AsyncMock(return_value=(None, None, None))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_custom_quantity_with_retry",
        submit_probe,
    )

    assert await _probe_above_quick_menu(page, combo) is None
    submit_probe.assert_awaited_once_with(page, combo, 10)
    page.wait_for_timeout.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepted_quantity_ten_continues_to_high_quantity_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    submit_probe = AsyncMock(
        side_effect=[
            (True, None, None),
            (False, 42, None),
        ]
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_custom_quantity_with_retry",
        submit_probe,
    )

    assert await _probe_above_quick_menu(page, combo) == (42, True, None)
    assert submit_probe.await_args_list == [
        call(page, combo, 10),
        call(page, combo, 100),
    ]


@pytest.mark.asyncio
async def test_customer_limit_is_verified_and_retained_as_at_least_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    submit_probe = AsyncMock(
        side_effect=[
            (True, None, None),
            (False, None, 20),
            (True, None, 20),
        ]
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_custom_quantity_with_retry",
        submit_probe,
    )

    assert await _probe_above_quick_menu(page, combo) == (20, False, 20)
    assert submit_probe.await_args_list == [
        call(page, combo, 10),
        call(page, combo, 100),
        call(page, combo, 20),
    ]


def test_customer_purchase_limit_message_parser() -> None:
    assert _parse_customer_purchase_limit("Limited to 20 per customer.") == 20
    assert _parse_customer_purchase_limit("Limited to 1,250 per customer") == 1250
    assert _parse_customer_purchase_limit("In stock") is None


@pytest.mark.asyncio
async def test_explicit_custom_quantity_warning_returns_without_extra_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    ensure_input = AsyncMock(return_value=True)
    submit_quantity = AsyncMock(return_value=(False, 4, None))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure_input,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit_quantity,
    )

    assert await _probe_custom_quantity_with_retry(page, combo, 100) == (
        False,
        4,
        None,
    )
    ensure_input.assert_awaited_once_with(page, combo)
    submit_quantity.assert_awaited_once_with(page, combo, 100)
    page.wait_for_timeout.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_ten_plus_editor_reads_explicit_no_ten_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    body = Mock()
    body.inner_text = AsyncMock(return_value="We currently do not have 10 in stock.")
    page.locator.return_value = body
    combo = Mock()
    ensure_input = AsyncMock(return_value=False)
    submit_quantity = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure_input,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit_quantity,
    )

    assert await _probe_custom_quantity_with_retry(page, combo, 10) == (
        False,
        None,
        None,
    )
    ensure_input.assert_awaited_once_with(page, combo)
    page.locator.assert_called_once_with("body")
    submit_quantity.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_ten_plus_editor_without_warning_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    body = Mock()
    body.inner_text = AsyncMock(return_value="Shopping Cart")
    page.locator.return_value = body
    page.reload = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    combo = Mock()
    ensure_input = AsyncMock(return_value=False)
    submit_quantity = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure_input,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit_quantity,
    )

    assert await _probe_custom_quantity_with_retry(page, combo, 10) == (
        None,
        None,
        None,
    )
    assert ensure_input.await_count == 3
    assert page.reload.await_count == 2
    submit_quantity.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_cart_button_is_scoped_to_the_product_buy_box() -> None:
    page = Mock()
    buy_box = Mock()
    button = Mock()
    page.locator.return_value = buy_box
    buy_box.get_by_role.return_value = button
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)

    selected = await _find_main_add_to_cart_button(page)

    assert selected is button
    page.locator.assert_called_once_with("main aside")
    buy_box.get_by_role.assert_called_once_with(
        "button",
        name="Add to Cart",
        exact=True,
    )


@pytest.mark.asyncio
async def test_main_cart_button_rejects_missing_or_ambiguous_buy_box() -> None:
    page = Mock()
    button = page.locator.return_value.get_by_role.return_value
    button.count = AsyncMock(return_value=2)

    with pytest.raises(RuntimeError, match="主购买区"):
        await _find_main_add_to_cart_button(page)


@pytest.mark.asyncio
async def test_add_to_cart_waits_for_takealot_to_persist_the_request() -> None:
    page = Mock()
    button = page.locator.return_value.get_by_role.return_value
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)
    button.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()

    await _add_main_product_to_cart(page)

    button.click.assert_awaited_once_with()
    page.wait_for_timeout.assert_awaited_once_with(1500)


@pytest.mark.asyncio
async def test_marketing_overlay_cleanup_is_limited_to_braze_elements() -> None:
    page = Mock()
    page.evaluate = AsyncMock()

    await _dismiss_marketing_overlay(page)

    script = page.evaluate.await_args.args[0]
    assert ".ab-iam-root, .ab-page-blocker" in script
    assert "ab-pause-scrolling" in script


@pytest.mark.asyncio
async def test_cart_never_falls_back_to_an_unrelated_single_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    product_link = page.locator.return_value
    product_link.count = AsyncMock(return_value=0)
    page.wait_for_timeout = AsyncMock()
    times = iter((0.0, 0.0, 16.0))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.time",
        SimpleNamespace(monotonic=lambda: next(times)),
    )

    with pytest.raises(RuntimeError, match="未找到目标竞品 PLID72189176"):
        await _find_product_quantity_combo(page, "72189176")

    page.locator.assert_called_once_with('a[href*="/PLID72189176"]:visible')


@pytest.mark.asyncio
async def test_quantity_menu_waits_for_reopen_animation() -> None:
    page = Mock()
    combo = Mock()
    option = page.locator.return_value.filter.return_value
    option.count = AsyncMock(side_effect=[0, 0, 1])
    option.is_visible = AsyncMock(return_value=True)
    option.click = AsyncMock()
    combo.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()

    await _select_quantity_option(page, combo, 9)

    combo.click.assert_awaited_once_with()
    assert page.wait_for_timeout.await_count == 3
    option.click.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_quantity_menu_retries_when_first_open_is_swallowed() -> None:
    page = Mock()
    combo = Mock()
    option = page.locator.return_value.filter.return_value
    option.count = AsyncMock(side_effect=[0] * 8 + [1])
    option.is_visible = AsyncMock(return_value=True)
    option.click = AsyncMock()
    combo.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard.press = AsyncMock()

    await _select_quantity_option(page, combo, 9)

    assert combo.click.await_count == 2
    page.keyboard.press.assert_awaited_once_with("Escape")
    option.click.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_custom_quantity_accepts_closed_editor_with_matching_combo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    quantity_input = Mock()
    update_button = Mock()
    body = Mock()

    def locator(selector: str) -> Mock:
        if selector == ('input[name="quantity"]:not([aria-hidden="true"]):visible'):
            return quantity_input
        if selector == "button:visible":
            return update_button
        if selector == "body":
            return body
        raise AssertionError(selector)

    page.locator.side_effect = locator
    quantity_input.count = AsyncMock(return_value=1)
    quantity_input.input_value = AsyncMock(return_value="")
    quantity_input.fill = AsyncMock()
    quantity_input.press = AsyncMock()
    update_button.filter.return_value = update_button
    update_button.count = AsyncMock(return_value=1)
    update_button.click = AsyncMock()
    body.inner_text = AsyncMock(return_value="Shopping Cart")
    combo.is_visible = AsyncMock(return_value=True)
    combo.inner_text = AsyncMock(return_value="Qty: 14")
    clock = [100.0]

    async def advance(_: int) -> None:
        clock[0] += 1.0

    page.wait_for_timeout = AsyncMock(side_effect=advance)
    page.evaluate = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    assert await _submit_custom_quantity(page, combo, 14) == (True, None, None)
    quantity_input.fill.assert_awaited_once_with("14", force=True)


@pytest.mark.asyncio
async def test_custom_quantity_reloads_and_retries_transient_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    ensure = AsyncMock(return_value=True)
    submit = AsyncMock(side_effect=[(None, None, None), (True, None, None)])
    page.reload = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit,
    )

    assert await _probe_custom_quantity_with_retry(page, combo, 20) == (
        True,
        None,
        None,
    )
    assert ensure.await_count == 2
    assert submit.await_count == 2
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded")
    assert page.wait_for_timeout.await_count == 2


@pytest.mark.asyncio
async def test_custom_quantity_retries_transient_error_even_with_limit_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    combo = Mock()
    ensure = AsyncMock(return_value=True)
    submit = AsyncMock(
        side_effect=[
            (None, None, 20),
            (True, None, 20),
        ]
    )
    page.reload = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._ensure_custom_quantity_input",
        ensure,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._submit_custom_quantity",
        submit,
    )

    assert await _probe_custom_quantity_with_retry(page, combo, 20) == (
        True,
        None,
        20,
    )
    assert ensure.await_count == 2
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded")
