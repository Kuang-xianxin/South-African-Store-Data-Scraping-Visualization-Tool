from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorVariant,
)
from takealot_ops.competitors.stock import (
    _BuyboxOfferCandidate,
    _add_main_product_to_cart,
    _add_other_offer_to_cart,
    _buybox_candidate_is_selected,
    _choose_buybox_offer_candidate,
    _choose_quantity,
    _dismiss_cookie,
    _dismiss_marketing_overlay,
    _dismiss_terms_modal,
    _expand_other_offers,
    _find_main_add_to_cart_button,
    _find_other_offer_add_to_cart_button,
    _find_other_offer_button_by_seller_row,
    _find_product_quantity_combo,
    _open_quantity_menu_with_retry,
    _parse_customer_purchase_limit,
    _probe_page_offer_stock,
    _probe_above_quick_menu,
    _probe_custom_quantity_with_retry,
    _read_visible_numeric_quantity_options,
    _select_quantity_option,
    _select_buybox_offer,
    _stock_probe_failure_note,
    _submit_custom_quantity,
    _url_matches_plid,
    probe_product_stocks,
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
async def test_multi_buybox_probe_selects_the_exact_green_price_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="240468115",
        seller_id="29900272",
        seller_name="卖家ID 29900272（平台未返回名称）",
        price=513,
        stock_status="Ships in 7 - 9 work days",
        is_buybox=True,
        offer_id="240468115",
        buybox_rank=1,
        is_follower_offer=True,
    )
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    radio = Mock()
    radio.is_checked = AsyncMock(side_effect=[False, True])
    radio.get_attribute = AsyncMock(return_value=None)
    label = Mock()
    label.click = AsyncMock()
    label.get_attribute = AsyncMock(return_value=None)
    candidate = _BuyboxOfferCandidate(
        index=1,
        click_targets=(label,),
        state_targets=(radio, label),
        text="Best Price R 513 Delivery 15 Aug",
        radio=radio,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._buybox_offer_candidates",
        AsyncMock(return_value=[candidate]),
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        AsyncMock(return_value=False),
    )

    await _select_buybox_offer(page, offer)

    label.click.assert_awaited_once_with(timeout=5_000)
    page.wait_for_timeout.assert_awaited_once_with(1200)


def test_multi_buybox_candidate_uses_unique_price_after_dom_rerender() -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="238358711",
        seller_id="29899430",
        seller_name="卖家ID 29899430（平台未返回名称）",
        price=999,
        stock_status="In stock",
        is_buybox=True,
        offer_id="238358711",
        buybox_rank=1,
        is_follower_offer=True,
    )
    candidates = [
        _BuyboxOfferCandidate(0, (Mock(),), (Mock(),), "Fastest Delivery R 1,099"),
        _BuyboxOfferCandidate(1, (Mock(),), (Mock(),), "TakealotMore credit option"),
        _BuyboxOfferCandidate(
            2,
            (Mock(),),
            (Mock(),),
            "Best Price R 999R 1,299 Get it Tomorrow, 7am - 7pm T&Cs Apply",
        ),
    ]

    selected = _choose_buybox_offer_candidate(candidates, offer)

    assert selected is candidates[2]


def test_multi_buybox_candidate_rejects_rank_with_the_wrong_price() -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="238358711",
        seller_id="29899430",
        seller_name="卖家ID 29899430（平台未返回名称）",
        price=999,
        stock_status="In stock",
        is_buybox=True,
        offer_id="238358711",
        buybox_rank=1,
        is_follower_offer=True,
    )
    candidates = [
        _BuyboxOfferCandidate(0, (Mock(),), (Mock(),), "Fastest Delivery R 1,099"),
        _BuyboxOfferCandidate(1, (Mock(),), (Mock(),), "Best Price R 998"),
    ]

    assert _choose_buybox_offer_candidate(candidates, offer) is None


@pytest.mark.asyncio
async def test_multi_buybox_candidate_accepts_card_aria_selected_state() -> None:
    radio = Mock()
    radio.is_checked = AsyncMock(return_value=False)
    radio.get_attribute = AsyncMock(return_value=None)
    card = Mock()
    card.get_attribute = AsyncMock(
        side_effect=lambda attribute: "true" if attribute == "aria-checked" else None
    )
    candidate = _BuyboxOfferCandidate(
        1,
        (card,),
        (radio, card),
        "Best Price R 999",
        radio,
    )

    assert await _buybox_candidate_is_selected(candidate) is True


@pytest.mark.asyncio
async def test_multi_buybox_candidate_accepts_current_active_card_class() -> None:
    offer_link = Mock()
    offer_link.get_attribute = AsyncMock(return_value=None)
    card = Mock()
    card.get_attribute = AsyncMock(
        side_effect=lambda attribute: (
            "buybox-offer-module_buybox-offer_1JNpe "
            "buybox-offer-module_active_3I1Yj"
            if attribute == "class"
            else None
        )
    )
    candidate = _BuyboxOfferCandidate(
        index=1,
        click_targets=(offer_link,),
        state_targets=(offer_link, card),
        text="Best Price R 999",
    )

    assert await _buybox_candidate_is_selected(candidate) is True


@pytest.mark.asyncio
async def test_follower_probe_can_keep_competing_buybox_without_variant_probe(
    tmp_path,
) -> None:
    buybox = CompetitorOffer(
        selected=True,
        sku="COMPETING-SKU",
        seller_id="competing-seller",
        seller_name="Competing Seller",
        price=99,
        stock_status="Out of stock",
        is_buybox=True,
        is_add_to_cart_available=False,
        plid="12345678",
        url="https://www.takealot.com/p/PLID12345678",
    )
    product = CompetitorProduct(
        plid="12345678",
        url="https://www.takealot.com/p/PLID12345678",
        title="Example",
        image_url=None,
        sku="COMPETING-SKU",
        seller_id="competing-seller",
        seller_name="Competing Seller",
        price=99,
        stock_status="Out of stock",
        is_leadtime=False,
        review_count=0,
        rating=0,
        offers=(buybox,),
        variants=(),
    )

    variants, offers = await probe_product_stocks(
        product,
        profile_dir=tmp_path / "stock-profile",
        probe_buyboxes=False,
        probe_offer_buyboxes=True,
    )

    assert variants == []
    assert len(offers) == 1
    assert offers[0].offer.is_buybox is True
    assert offers[0].stock.quantity == 0
    assert offers[0].stock.exact is True


@pytest.mark.asyncio
async def test_stock_probe_cancellation_closes_browser_without_cart_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant = CompetitorVariant(
        key="default",
        label="默认款",
        url="https://www.takealot.com/p/PLID12345678",
        title="Example",
        sku="SKU-1",
        seller_id="seller-1",
        seller_name="Seller One",
        price=99,
        stock_status="In stock",
        is_leadtime=False,
        is_add_to_cart_available=True,
    )
    product = CompetitorProduct(
        plid="12345678",
        url=variant.url,
        title=variant.title,
        image_url=None,
        sku=variant.sku,
        seller_id=variant.seller_id,
        seller_name=variant.seller_name,
        price=variant.price,
        stock_status=variant.stock_status,
        is_leadtime=False,
        review_count=0,
        rating=0,
        offers=(),
        variants=(variant,),
    )
    page = Mock()
    context = Mock()
    context.pages = [page]
    context.close = AsyncMock()
    playwright = Mock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    manager = Mock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=None)
    clear_cart = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock.async_playwright",
        lambda: manager,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_browser_executable",
        lambda: tmp_path / "chrome.exe",
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._probe_page_stock",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._clear_isolated_cart",
        clear_cart,
    )

    with pytest.raises(asyncio.CancelledError):
        await probe_product_stocks(
            product,
            profile_dir=tmp_path / "stock-profile",
        )

    assert context.close.await_count >= 1
    clear_cart.assert_not_awaited()

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
async def test_other_offer_button_is_scoped_by_sku_and_verified_seller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="237580845",
        seller_id="29866597",
        seller_name="GOnline",
        price=2499.0,
        stock_status="In stock",
        offer_id="other-buying-option-237580845",
    )
    page = Mock()
    card = page.locator.return_value
    card.count = AsyncMock(return_value=1)
    card.inner_text = AsyncMock(return_value="R 2,499 Sold by GOnline")
    button = card.get_by_role.return_value
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        AsyncMock(return_value=False),
    )

    selected = await _find_other_offer_add_to_cart_button(page, offer)

    assert selected is button
    page.locator.assert_called_once_with(
        '[id="other-buying-option-237580845"]:visible'
    )
    card.get_by_role.assert_called_once_with(
        "button",
        name="Add to Cart",
        exact=True,
    )


@pytest.mark.asyncio
async def test_current_other_offer_button_uses_exact_seller_row() -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="69484211",
        seller_id="29823500",
        seller_name="Station Vibration",
        price=4595.0,
        stock_status="In stock",
    )
    page = Mock()
    seller_links = Mock()
    seller_link = Mock()
    row = Mock()
    button = Mock()
    page.locator.return_value = seller_links
    seller_links.count = AsyncMock(return_value=1)
    seller_links.nth.return_value = seller_link
    seller_link.inner_text = AsyncMock(return_value=" Station Vibration ")
    seller_link.get_attribute = AsyncMock(
        return_value="/seller/station-vibration?sellers=29823500"
    )
    seller_link.locator.return_value = row
    row.count = AsyncMock(return_value=1)
    row.inner_text = AsyncMock(
        return_value="R 4,595 Estimated Delivery Station Vibration"
    )
    row.locator.return_value = button
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)

    selected = await _find_other_offer_button_by_seller_row(page, offer)

    assert selected is button
    page.locator.assert_called_once_with('a[href*="sellers="]:visible')
    row.locator.assert_called_once_with(
        'button[data-ref="buying-choice-atc"]:visible'
    )


@pytest.mark.asyncio
async def test_current_other_offer_button_rejects_wrong_seller_id() -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="69484211",
        seller_id="29823500",
        seller_name="Station Vibration",
        price=4595.0,
        stock_status="In stock",
    )
    page = Mock()
    seller_links = page.locator.return_value
    seller_link = Mock()
    seller_links.count = AsyncMock(return_value=1)
    seller_links.nth.return_value = seller_link
    seller_link.inner_text = AsyncMock(return_value="Station Vibration")
    seller_link.get_attribute = AsyncMock(
        return_value="/seller/station-vibration?sellers=99999999"
    )

    assert await _find_other_offer_button_by_seller_row(page, offer) is None
    seller_link.locator.assert_not_called()


@pytest.mark.asyncio
async def test_other_offer_list_expands_only_when_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Mock()
    trigger = page.locator.return_value.filter.return_value
    trigger.count = AsyncMock(return_value=1)
    trigger.is_visible = AsyncMock(return_value=True)
    trigger.get_attribute = AsyncMock(side_effect=["trigger", "trigger is-open"])
    trigger.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        AsyncMock(return_value=False),
    )

    assert await _expand_other_offers(page) is True
    assert await _expand_other_offers(page) is False

    trigger.click.assert_awaited_once_with()
    page.wait_for_timeout.assert_awaited_once_with(700)


@pytest.mark.asyncio
async def test_other_offer_button_rejects_a_different_seller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="237580845",
        seller_id="29866597",
        seller_name="GOnline",
        price=2499.0,
        stock_status="In stock",
    )
    page = Mock()
    card = page.locator.return_value
    card.count = AsyncMock(return_value=1)
    card.inner_text = AsyncMock(return_value="Sold by Another Seller")
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        AsyncMock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="未显示预期卖家 GOnline"):
        await _find_other_offer_add_to_cart_button(page, offer)


@pytest.mark.asyncio
async def test_add_other_offer_waits_for_cart_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="237580845",
        seller_id="29866597",
        seller_name="GOnline",
        price=2499.0,
        stock_status="In stock",
    )
    page = Mock()
    page.wait_for_timeout = AsyncMock()
    button = Mock(click=AsyncMock())
    find_button = AsyncMock(return_value=button)
    dismiss_overlay = AsyncMock()
    dismiss_terms = AsyncMock(side_effect=[False, False, False])
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_other_offer_add_to_cart_button",
        find_button,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        dismiss_overlay,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        dismiss_terms,
    )

    await _add_other_offer_to_cart(page, offer)

    assert dismiss_overlay.await_count == 2
    assert dismiss_terms.await_count == 3
    find_button.assert_awaited_once_with(page, offer)
    button.click.assert_awaited_once_with(timeout=10_000)
    assert page.wait_for_timeout.await_args_list == [call(600), call(1500)]


@pytest.mark.asyncio
async def test_add_other_offer_retries_after_terms_modal_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = CompetitorOffer(
        selected=False,
        sku="69484211",
        seller_id="29823500",
        seller_name="Station Vibration",
        price=4595.0,
        stock_status="In stock",
    )
    page = Mock(wait_for_timeout=AsyncMock())
    button = Mock(click=AsyncMock())
    find_button = AsyncMock(return_value=button)
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_other_offer_add_to_cart_button",
        find_button,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_terms_modal",
        AsyncMock(side_effect=[False, False, True]),
    )

    await _add_other_offer_to_cart(page, offer)

    assert find_button.await_count == 2
    assert button.click.await_count == 2
    assert button.click.await_args_list == [
        call(timeout=10_000),
        call(timeout=10_000),
    ]


@pytest.mark.asyncio
async def test_terms_modal_close_is_scoped_to_a_visible_terms_dialog() -> None:
    page = Mock()
    dialogs = page.locator.return_value
    dialog = Mock()
    close_buttons = Mock()
    close_button = Mock()
    dialogs.count = AsyncMock(return_value=1)
    dialogs.nth.return_value = dialog
    dialog.inner_text = AsyncMock(
        return_value="Please read these terms and conditions carefully"
    )
    dialog.locator.return_value = close_buttons
    close_buttons.count = AsyncMock(return_value=1)
    close_buttons.nth.return_value = close_button
    close_button.is_visible = AsyncMock(return_value=True)
    close_button.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    assert await _dismiss_terms_modal(page) is True

    close_button.click.assert_awaited_once_with()
    page.wait_for_timeout.assert_awaited_once_with(300)


@pytest.mark.asyncio
async def test_follower_probe_reads_quantity_from_verified_seller_cart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.takealot.com/example/PLID98055738"
    offer = CompetitorOffer(
        selected=False,
        sku="237580845",
        seller_id="29866597",
        seller_name="GOnline",
        price=2499.0,
        stock_status="In stock",
        offer_id="other-buying-option-237580845",
        url=url,
    )
    variant = CompetitorVariant(
        key="default",
        label="默认款",
        url=url,
        title="Camping Trolley",
        sku="222378247",
        seller_id="29864263",
        seller_name="Main Seller",
        price=2489.0,
        stock_status="In stock",
        is_leadtime=False,
        is_add_to_cart_available=True,
    )
    product = CompetitorProduct(
        plid="98055738",
        url=url,
        title="Camping Trolley",
        image_url=None,
        sku=variant.sku,
        seller_id=variant.seller_id,
        seller_name=variant.seller_name,
        price=variant.price,
        stock_status=variant.stock_status,
        is_leadtime=False,
        review_count=0,
        rating=0.0,
        offers=(offer,),
        variants=(variant,),
    )
    page = Mock()
    clear_cart = AsyncMock()
    goto = AsyncMock()
    wait_for_product = AsyncMock()
    add_offer = AsyncMock()
    find_quantity = AsyncMock(return_value=(4, True, "精确库存", None))
    monkeypatch.setattr("takealot_ops.competitors.stock._clear_isolated_cart", clear_cart)
    monkeypatch.setattr("takealot_ops.competitors.stock._goto", goto)
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._wait_for_product",
        wait_for_product,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._add_other_offer_to_cart",
        add_offer,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._find_exact_quantity",
        find_quantity,
    )

    result = await _probe_page_offer_stock(page, product=product, offer=offer)

    assert result.quantity == 4
    assert result.exact is True
    assert result.method == "anonymous-cart-limit"
    assert result.note == (
        "跟卖卖家 GOnline：已核验卖家报价行、购物车PLID和价格；"
        "精确库存"
    )
    add_offer.assert_awaited_once_with(page, offer)
    find_quantity.assert_awaited_once_with(
        page,
        "98055738",
        seller_name="GOnline",
        expected_price=2499.0,
    )


@pytest.mark.asyncio
async def test_marketing_overlay_cleanup_is_limited_to_braze_elements() -> None:
    page = Mock()
    page.evaluate = AsyncMock()

    await _dismiss_marketing_overlay(page)

    script = page.evaluate.await_args.args[0]
    assert ".ab-iam-root, .ab-page-blocker" in script
    assert "ab-pause-scrolling" in script


@pytest.mark.asyncio
async def test_cookie_dismissal_clears_late_braze_overlay_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def clear_overlay(_page: Mock) -> None:
        calls.append("overlay")

    page = Mock()
    button = page.get_by_role.return_value
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)
    button.click = AsyncMock(side_effect=lambda **_kwargs: calls.append("cookie"))
    monkeypatch.setattr(
        "takealot_ops.competitors.stock._dismiss_marketing_overlay",
        clear_overlay,
    )

    await _dismiss_cookie(page)

    assert calls == ["overlay", "cookie"]
    button.click.assert_awaited_once_with(timeout=5_000)


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
async def test_cart_accepts_hidden_seller_only_with_the_exact_offer_price() -> None:
    page = Mock()
    product_link = page.locator.return_value
    product_row = Mock()
    combo = Mock()
    price = Mock()
    product_link.count = AsyncMock(return_value=1)
    product_link.first.locator.return_value = product_row

    def row_locator(selector: str) -> Mock:
        if selector == 'button[role="combobox"]:visible':
            return combo
        if selector == '[data-ref="product-card-price"]:visible':
            return price
        raise AssertionError(selector)

    product_row.locator.side_effect = row_locator
    product_row.inner_text = AsyncMock(
        return_value="Hybrid M1202UBTX Band Mixer In stock Qty: 1"
    )
    combo.count = AsyncMock(return_value=1)
    price.count = AsyncMock(return_value=1)
    price.inner_text = AsyncMock(return_value="R 4,595")

    selected = await _find_product_quantity_combo(
        page,
        "50067762",
        seller_name="Station Vibration",
        expected_price=4595.0,
    )

    assert selected is combo


@pytest.mark.asyncio
async def test_cart_rejects_a_different_offer_price_when_seller_is_hidden() -> None:
    page = Mock()
    product_link = page.locator.return_value
    product_row = Mock()
    combo = Mock()
    price = Mock()
    product_link.count = AsyncMock(return_value=1)
    product_link.first.locator.return_value = product_row
    product_row.locator.side_effect = [combo, price]
    combo.count = AsyncMock(return_value=1)
    price.count = AsyncMock(return_value=1)
    price.inner_text = AsyncMock(return_value="R 4,568")

    with pytest.raises(RuntimeError, match="未显示预期跟卖价格"):
        await _find_product_quantity_combo(
            page,
            "50067762",
            seller_name="Station Vibration",
            expected_price=4595.0,
        )


@pytest.mark.asyncio
async def test_cart_rejects_an_explicitly_different_seller_even_at_same_price() -> None:
    page = Mock()
    product_link = page.locator.return_value
    product_row = Mock()
    combo = Mock()
    price = Mock()
    product_link.count = AsyncMock(return_value=1)
    product_link.first.locator.return_value = product_row
    product_row.locator.side_effect = [combo, price]
    product_row.inner_text = AsyncMock(
        return_value="Sold by Main Seller R 4,595 Qty: 1"
    )
    combo.count = AsyncMock(return_value=1)
    price.count = AsyncMock(return_value=1)
    price.inner_text = AsyncMock(return_value="R 4,595")

    with pytest.raises(RuntimeError, match="未显示预期跟卖卖家"):
        await _find_product_quantity_combo(
            page,
            "50067762",
            seller_name="Station Vibration",
            expected_price=4595.0,
        )


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
