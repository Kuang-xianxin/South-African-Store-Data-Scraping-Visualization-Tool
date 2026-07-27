"""Isolated anonymous-cart stock probing for explicitly requested products."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright

from takealot_ops.competitors.domain import (
    CompetitorProduct,
    CompetitorVariant,
    StockProbeResult,
    VariantStockObservation,
)


BROWSER_PATHS = (
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
)
HIGH_QUANTITY_PROBE = 100
MAX_CUSTOM_QUANTITY = 999
WAREHOUSE_WARNING_SETTLE_SECONDS = 3.0
WAREHOUSE_STOCK_PATTERN = re.compile(
    r"current\s+stock\s*=\s*(?P<quantity>[\d,]+)",
    re.IGNORECASE,
)
CUSTOM_QUANTITY_INPUT = (
    'input[name="quantity"]:not([aria-hidden="true"]):visible'
)


def skipped_stock_probe() -> StockProbeResult:
    """Return the explicit no-probe state used by fast API-only collection."""
    return StockProbeResult(
        quantity=None,
        exact=False,
        method="skipped",
        note="本次未执行购物车库存探测；可在下次采集时勾选库存探测。",
    )


def non_platform_stock_probe() -> StockProbeResult:
    """Return the excluded state for supplier/lead-time stock."""
    return StockProbeResult(
        quantity=0,
        exact=True,
        method="not-platform-stock",
        note="商品为供应商调货/长时效到货，不计入平台仓有效库存，已标记没货。",
    )


def unavailable_stock_probe() -> StockProbeResult:
    """Return an exact effective zero when the variant cannot be added to cart."""
    return StockProbeResult(
        quantity=0,
        exact=True,
        method="out-of-stock",
        note="该变体当前不可加入购物车，已标记平台仓没货。",
    )


async def probe_stock(
    product: CompetitorProduct,
    *,
    profile_dir: Path,
    visible: bool = False,
) -> StockProbeResult:
    """Probe the current seller/SKU cart limit in an isolated browser profile."""
    executable = _find_browser_executable()
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=str(executable),
            headless=False,
            locale="en-ZA",
            viewport={"width": 1365, "height": 900},
            args=[
                "--window-position=100,100" if visible else "--window-position=-32000,-32000",
                "--disable-background-timer-throttling",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            return await _probe_page_stock(
                page,
                plid=product.plid,
                url=product.url,
                title=product.title,
            )
        finally:
            try:
                await _clear_isolated_cart(page)
            except Exception:
                # The isolated profile is cleared again before the next probe.
                pass
            await context.close()


async def probe_variant_stocks(
    product: CompetitorProduct,
    *,
    profile_dir: Path,
    visible: bool = False,
) -> list[VariantStockObservation]:
    """Probe every purchasable variant in one isolated browser session."""
    results: dict[str, StockProbeResult] = {}
    purchasable: list[CompetitorVariant] = []
    for variant in product.variants:
        if variant.is_leadtime:
            results[variant.key] = non_platform_stock_probe()
        elif not variant.is_add_to_cart_available:
            results[variant.key] = unavailable_stock_probe()
        else:
            purchasable.append(variant)

    if purchasable:
        executable = _find_browser_executable()
        profile_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=str(executable),
                headless=False,
                locale="en-ZA",
                viewport={"width": 1365, "height": 900},
                args=[
                    "--window-position=100,100"
                    if visible
                    else "--window-position=-32000,-32000",
                    "--disable-background-timer-throttling",
                ],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                for variant in purchasable:
                    try:
                        results[variant.key] = await _probe_page_stock(
                            page,
                            plid=product.plid,
                            url=variant.url,
                            title=variant.title,
                        )
                    except (OSError, RuntimeError, PlaywrightError) as exc:
                        results[variant.key] = StockProbeResult(
                            quantity=None,
                            exact=False,
                            method="failed",
                            note=str(exc),
                        )
                    finally:
                        try:
                            await _clear_isolated_cart(page)
                        except Exception:
                            pass
            finally:
                await context.close()

    return [
        VariantStockObservation(variant=variant, stock=results[variant.key])
        for variant in product.variants
    ]


async def _probe_page_stock(
    page: Page,
    *,
    plid: str,
    url: str,
    title: str,
) -> StockProbeResult:
    await _clear_isolated_cart(page)
    await _goto(page, url)
    await _wait_for_product(page, plid, title)
    await _add_main_product_to_cart(page)

    await _goto(page, "https://www.takealot.com/cart")
    quantity, exact, note = await _find_exact_quantity(page, plid)
    return StockProbeResult(
        quantity=quantity,
        exact=exact,
        method="anonymous-cart-limit",
        note=note,
    )


def _find_browser_executable() -> Path:
    for candidate in BROWSER_PATHS:
        if candidate.exists():
            return candidate
    raise RuntimeError("未找到 Chrome 或 Edge，库存探测需要本机浏览器")


async def _dismiss_cookie(page: Page) -> None:
    button = page.get_by_role("button", name="Got it", exact=True)
    if await button.count() == 1 and await button.is_visible():
        await button.click()


async def _wait_for_product(page: Page, plid: str, title: str) -> None:
    for attempt in range(3):
        await page.wait_for_timeout(3500 if attempt == 0 else 6500)
        await _dismiss_cookie(page)
        body = await page.locator("body").inner_text()
        if _url_matches_plid(page.url, plid) and title in body:
            try:
                await _find_main_add_to_cart_button(page)
            except RuntimeError:
                pass
            else:
                return
        if attempt < 2:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
    raise RuntimeError(
        f"目标竞品 PLID{plid} 的主商品购买按钮未完整加载；"
        "已拒绝点击推荐商品，请稍后重试"
    )


def _url_matches_plid(url: str, plid: str) -> bool:
    return re.search(
        rf"/PLID{re.escape(plid)}(?:[/?#]|$)",
        url,
        re.IGNORECASE,
    ) is not None


async def _find_main_add_to_cart_button(page: Page) -> Locator:
    """Return only the target PDP buy-box button, never carousel recommendations."""
    buy_box = page.locator("main aside")
    button = buy_box.get_by_role("button", name="Add to Cart", exact=True)
    if await button.count() != 1 or not await button.is_visible():
        raise RuntimeError("无法唯一定位目标竞品主购买区的 Add to Cart 按钮")
    return button


async def _add_main_product_to_cart(page: Page) -> None:
    """Click the verified target button and let Takealot persist the async cart add."""
    await _dismiss_marketing_overlay(page)
    button = await _find_main_add_to_cart_button(page)
    await button.click()
    await page.wait_for_timeout(1500)


async def _dismiss_marketing_overlay(page: Page) -> None:
    """Remove Braze marketing modals that randomly block isolated cart controls."""
    await page.evaluate(
        """() => {
            document
                .querySelectorAll(".ab-iam-root, .ab-page-blocker")
                .forEach((element) => element.remove());
            document.documentElement.classList.remove("ab-pause-scrolling");
            document.body.classList.remove("ab-pause-scrolling");
        }"""
    )


async def _goto(page: Page, url: str) -> None:
    last_error: PlaywrightError | None = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return
        except PlaywrightError as exc:
            last_error = exc
            if attempt < 2:
                await page.wait_for_timeout(random.randint(1200, 2500) * (attempt + 1))
    assert last_error is not None
    raise last_error


async def _clear_isolated_cart(page: Page) -> None:
    await _goto(page, "https://www.takealot.com/cart")
    await _dismiss_marketing_overlay(page)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        body = await page.locator("body").inner_text()
        buttons = page.locator('button[title="Remove product from cart"]:visible')
        if await buttons.count() > 0 or "Your shopping cart is empty" in body:
            break
        await page.wait_for_timeout(random.randint(1200, 2500))
    buttons = page.locator('button[title="Remove product from cart"]:visible')
    while await buttons.count() > 0:
        await _dismiss_marketing_overlay(page)
        await buttons.first.click(force=True)
        await page.wait_for_timeout(random.randint(1200, 2500))


async def _choose_quantity(
    page: Page,
    combo: Locator,
    quantity: int,
) -> tuple[bool, int | None]:
    await _select_quantity_option(page, combo, quantity)

    deadline = time.monotonic() + 9
    accepted_since: float | None = None
    while time.monotonic() < deadline:
        body = await page.locator("body").inner_text()
        explicit_quantity = _parse_warehouse_stock_message(body)
        if explicit_quantity is not None:
            return False, explicit_quantity
        if f"We currently do not have {quantity} in stock." in body:
            return False, None
        if "An error occurred while trying to update your cart" in body:
            raise RuntimeError("Takealot 拒绝了购物车数量更新，请稍后重试")
        if (await combo.inner_text()).strip() == f"Qty: {quantity}":
            accepted_since = accepted_since or time.monotonic()
            if (
                time.monotonic() - accepted_since
                >= WAREHOUSE_WARNING_SETTLE_SECONDS
            ):
                return True, None
        else:
            accepted_since = None
        await page.wait_for_timeout(300)
    raise RuntimeError(f"等待数量 {quantity} 的库存校验结果超时")


async def _select_quantity_option(
    page: Page,
    combo: Locator,
    quantity: int,
) -> None:
    """Open the animated quantity menu and select one exact numeric option."""
    selected = await _select_quantity_menu_option(
        page,
        combo,
        re.compile(rf"^{quantity}$"),
    )
    if not selected:
        raise RuntimeError(f"购物车没有提供数量 {quantity} 的测试选项")


async def _select_quantity_menu_option(
    page: Page,
    combo: Locator,
    option_pattern: re.Pattern[str],
) -> bool:
    """Retry an animated quantity-menu option whose first click may be swallowed."""
    for _ in range(3):
        await _dismiss_marketing_overlay(page)
        await page.wait_for_timeout(random.randint(1200, 2500))
        await combo.click()
        for _ in range(8):
            option = page.locator('[role="option"]:visible').filter(
                has_text=option_pattern
            )
            if await option.count() == 1 and await option.is_visible():
                await _dismiss_marketing_overlay(page)
                await option.click()
                return True
            await page.wait_for_timeout(random.randint(1200, 2500))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(random.randint(1200, 2500))
    return False


async def _read_visible_numeric_quantity_options(page: Page) -> list[int]:
    """Read the currently rendered numeric options from the animated menu."""
    numeric_options: list[int] = []
    options = page.locator('[role="option"]:visible')
    for index in range(await options.count()):
        text = (await options.nth(index).inner_text()).strip()
        if text.isdigit():
            numeric_options.append(int(text))
    return numeric_options


async def _open_quantity_menu_with_retry(
    page: Page,
    plid: str,
) -> tuple[Locator, list[int]]:
    """Wait for the initial menu, then reload once and re-identify the PLID."""
    for page_attempt in range(2):
        combo = await _find_product_quantity_combo(page, plid)
        for _ in range(3):
            await _dismiss_marketing_overlay(page)
            await page.wait_for_timeout(random.randint(800, 1500))
            await combo.click()
            for _ in range(8):
                numeric_options = await _read_visible_numeric_quantity_options(page)
                if numeric_options:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(random.randint(800, 1500))
                    return combo, numeric_options
                await page.wait_for_timeout(500)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(random.randint(800, 1500))
        if page_attempt == 0:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(random.randint(1500, 3000))
    raise RuntimeError(
        "购物车数量菜单在3次重开和1次页面刷新后仍没有可识别的数字选项"
    )


async def _find_product_quantity_combo(page: Page, plid: str) -> Locator:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        product_link = page.locator(
            f'a[href*="/PLID{plid}"]:visible'
        )
        if await product_link.count() >= 1:
            product_row = product_link.first.locator(
                'xpath=ancestor::*[.//button[@role="combobox"]][1]'
            )
            scoped_combo = product_row.locator('button[role="combobox"]:visible')
            if await scoped_combo.count() == 1:
                return scoped_combo
        await page.wait_for_timeout(random.randint(1200, 2500))
    raise RuntimeError(
        f"购物车中未找到目标竞品 PLID{plid}；"
        "已拒绝把其他商品当作目标库存"
    )


def _parse_warehouse_stock_message(text: str) -> int | None:
    """Parse Takealot's explicit current warehouse stock warning."""
    match = WAREHOUSE_STOCK_PATTERN.search(text)
    if match is None:
        return None
    return int(match.group("quantity").replace(",", ""))


async def _probe_above_quick_menu(
    page: Page,
    combo: Locator,
) -> tuple[int, bool] | None:
    """Switch to custom quantity input and parse the explicit warehouse limit."""
    low = 9
    accepted, explicit_quantity = await _probe_custom_quantity_with_retry(
        page,
        combo,
        10,
    )
    if explicit_quantity is not None:
        return explicit_quantity, True
    if accepted is None:
        return None
    if not accepted:
        return 9, True

    low = 10
    await page.wait_for_timeout(random.randint(1500, 3000))
    probe = HIGH_QUANTITY_PROBE
    while True:
        accepted, explicit_quantity = await _probe_custom_quantity_with_retry(
            page,
            combo,
            probe,
        )
        if explicit_quantity is not None:
            return explicit_quantity, True
        if accepted is None:
            return None
        if not accepted:
            high = probe - 1
            break
        low = probe
        if probe >= MAX_CUSTOM_QUANTITY:
            return MAX_CUSTOM_QUANTITY, False
        probe = min(MAX_CUSTOM_QUANTITY, probe * 2)
        await page.wait_for_timeout(random.randint(1500, 3000))

    while low < high:
        middle = (low + high + 1) // 2
        accepted, explicit_quantity = await _probe_custom_quantity_with_retry(
            page,
            combo,
            middle,
        )
        if explicit_quantity is not None:
            return explicit_quantity, True
        if accepted is None:
            return None
        if accepted:
            low = middle
        else:
            high = middle - 1
        await page.wait_for_timeout(random.randint(1500, 3000))
    return low, True


async def _probe_custom_quantity_with_retry(
    page: Page,
    combo: Locator,
    quantity: int,
) -> tuple[bool | None, int | None]:
    """Retry transient cart-update errors without changing the target product."""
    for attempt in range(3):
        if not await _ensure_custom_quantity_input(page, combo):
            cart_text = await page.locator("body").inner_text()
            explicit_quantity = _parse_warehouse_stock_message(cart_text)
            if explicit_quantity is not None:
                return False, explicit_quantity
            if f"We currently do not have {quantity} in stock." in cart_text:
                return False, None
            return None, None
        result = await _submit_custom_quantity(page, combo, quantity)
        if result[1] is not None:
            return result
        if result[0] is not None:
            await page.wait_for_timeout(random.randint(1200, 3000))
            return result
        if attempt < 2:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(1500, 3500))
    return None, None


async def _ensure_custom_quantity_input(page: Page, combo: Locator) -> bool:
    quantity_input = page.locator(CUSTOM_QUANTITY_INPUT)
    if await quantity_input.count() == 1:
        return True
    if not await _select_quantity_menu_option(
        page,
        combo,
        re.compile(r"^10\+$"),
    ):
        return False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and await quantity_input.count() != 1:
        await page.wait_for_timeout(random.randint(1200, 2000))
    return await quantity_input.count() == 1


async def _submit_custom_quantity(
    page: Page,
    combo: Locator,
    quantity: int,
) -> tuple[bool | None, int | None]:
    quantity_input = page.locator(CUSTOM_QUANTITY_INPUT)
    if await quantity_input.count() != 1:
        return None, None
    # Takealot renders a full-card product link over the custom quantity
    # editor.  The editor is still the unique target-cart input, but normal
    # pointer clicks can be intercepted by that link.
    await quantity_input.fill(str(quantity), force=True)
    await quantity_input.press("Tab")

    update_button = page.locator("button:visible").filter(has_text="Update")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and await update_button.count() != 1:
        await page.wait_for_timeout(random.randint(1200, 2000))
    if await update_button.count() != 1:
        return None, None
    await _dismiss_marketing_overlay(page)
    await update_button.click()

    await page.wait_for_timeout(random.randint(1200, 3500))
    deadline = time.monotonic() + 10
    accepted_since: float | None = None
    while time.monotonic() < deadline:
        cart_text = await page.locator("body").inner_text()
        explicit_quantity = _parse_warehouse_stock_message(cart_text)
        if explicit_quantity is not None:
            return False, explicit_quantity
        if f"We currently do not have {quantity} in stock." in cart_text:
            return False, None
        if "An error occurred while trying to update your cart" in cart_text:
            return None, None
        accepted_signal = False
        if await combo.is_visible() and (await combo.inner_text()).strip() == f"Qty: {quantity}":
            accepted_signal = True
        if await quantity_input.count() == 1:
            current_value = (await quantity_input.input_value()).strip()
            if current_value == str(quantity):
                accepted_signal = True
            if current_value and current_value != str(quantity):
                return False, None
        accepted_since = (
            accepted_since or time.monotonic()
            if accepted_signal
            else None
        )
        if (
            accepted_since is not None
            and time.monotonic() - accepted_since
            >= WAREHOUSE_WARNING_SETTLE_SECONDS
        ):
            return True, None
        await page.wait_for_timeout(300)
    return None, None


async def _find_exact_quantity(
    page: Page, plid: str
) -> tuple[int, bool, str]:
    combo, numeric_options = await _open_quantity_menu_with_retry(page, plid)
    if 9 not in numeric_options:
        maximum = max(numeric_options)
        accepted, explicit_quantity = await _choose_quantity(page, combo, maximum)
        if explicit_quantity is not None:
            return (
                explicit_quantity,
                True,
                "购物车明确提示的当前平台仓库存；供应商追加库存未计入。",
            )
        if not accepted:
            raise RuntimeError(f"购物车拒绝了菜单显示的最大数量 {maximum}")
        return maximum, True, "购物车数量菜单显示的当前最大可选数量。"
    accepted, explicit_quantity = await _choose_quantity(page, combo, 9)
    if explicit_quantity is not None:
        return (
            explicit_quantity,
            True,
            "购物车明确提示的当前平台仓库存；供应商追加库存未计入。",
        )
    if accepted:
        warehouse_result = await _probe_above_quick_menu(page, combo)
        if warehouse_result is not None:
            warehouse_quantity, warehouse_exact = warehouse_result
            return (
                warehouse_quantity,
                warehouse_exact,
                (
                    "通过购物车超量提示和数量二分校验取得当前平台仓精确库存；"
                    "供应商追加库存未计入。"
                    if warehouse_exact
                    else "购物车已接受999件；当前只能保守记录平台仓库存至少999。"
                ),
            )
        return 9, False, "购物车已接受9件；快捷数量菜单无法继续给出可靠上限。"
    low = 1
    high = 8
    while low < high:
        middle = (low + high + 1) // 2
        accepted, explicit_quantity = await _choose_quantity(page, combo, middle)
        if explicit_quantity is not None:
            return (
                explicit_quantity,
                True,
                "购物车明确提示的当前平台仓库存；供应商追加库存未计入。",
            )
        if accepted:
            low = middle
        else:
            high = middle - 1
    return low, True, "通过隔离匿名购物车数量校验得到的当前可售上限。"
