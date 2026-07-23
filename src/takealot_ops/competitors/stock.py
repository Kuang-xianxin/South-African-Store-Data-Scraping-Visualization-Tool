"""Isolated anonymous-cart stock probing for explicitly requested products."""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

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
WAREHOUSE_STOCK_PATTERN = re.compile(
    r"current\s+stock\s*=\s*(?P<quantity>[\d,]+)",
    re.IGNORECASE,
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


def probe_stock(
    product: CompetitorProduct,
    *,
    profile_dir: Path,
    visible: bool = False,
) -> StockProbeResult:
    """Probe the current seller/SKU cart limit in an isolated browser profile."""
    executable = _find_browser_executable()
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
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
        page = context.pages[0] if context.pages else context.new_page()
        try:
            return _probe_page_stock(
                page,
                plid=product.plid,
                url=product.url,
                title=product.title,
            )
        finally:
            try:
                _clear_isolated_cart(page)
            except Exception:
                # The isolated profile is cleared again before the next probe.
                pass
            context.close()


def probe_variant_stocks(
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
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
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
            page = context.pages[0] if context.pages else context.new_page()
            try:
                for variant in purchasable:
                    try:
                        results[variant.key] = _probe_page_stock(
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
                            _clear_isolated_cart(page)
                        except Exception:
                            pass
            finally:
                context.close()

    return [
        VariantStockObservation(variant=variant, stock=results[variant.key])
        for variant in product.variants
    ]


def _probe_page_stock(
    page: Page,
    *,
    plid: str,
    url: str,
    title: str,
) -> StockProbeResult:
    _clear_isolated_cart(page)
    _goto(page, url)
    _wait_for_product(page, plid, title)
    _find_main_add_to_cart_button(page).click()

    _goto(page, "https://www.takealot.com/cart")
    quantity, exact, note = _find_exact_quantity(page, plid)
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


def _dismiss_cookie(page: Page) -> None:
    button = page.get_by_role("button", name="Got it", exact=True)
    if button.count() == 1 and button.is_visible():
        button.click()


def _wait_for_product(page: Page, plid: str, title: str) -> None:
    for attempt in range(3):
        page.wait_for_timeout(3500 if attempt == 0 else 6500)
        _dismiss_cookie(page)
        body = page.locator("body").inner_text()
        if _url_matches_plid(page.url, plid) and title in body:
            try:
                _find_main_add_to_cart_button(page)
            except RuntimeError:
                pass
            else:
                return
        if attempt < 2:
            page.reload(wait_until="domcontentloaded", timeout=45_000)
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


def _find_main_add_to_cart_button(page: Page) -> Locator:
    """Return only the target PDP buy-box button, never carousel recommendations."""
    buy_box = page.locator("main aside")
    button = buy_box.get_by_role("button", name="Add to Cart", exact=True)
    if button.count() != 1 or not button.is_visible():
        raise RuntimeError("无法唯一定位目标竞品主购买区的 Add to Cart 按钮")
    return button


def _goto(page: Page, url: str) -> None:
    last_error: PlaywrightError | None = None
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return
        except PlaywrightError as exc:
            last_error = exc
            if attempt < 2:
                page.wait_for_timeout(1200 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _clear_isolated_cart(page: Page) -> None:
    _goto(page, "https://www.takealot.com/cart")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        body = page.locator("body").inner_text()
        buttons = page.locator('button[title="Remove product from cart"]:visible')
        if buttons.count() > 0 or "Your shopping cart is empty" in body:
            break
        page.wait_for_timeout(500)
    buttons = page.locator('button[title="Remove product from cart"]:visible')
    while buttons.count() > 0:
        buttons.first.click(force=True)
        page.wait_for_timeout(500)


def _choose_quantity(page: Page, combo: Locator, quantity: int) -> bool:
    combo.click()
    option = page.locator('[role="option"]:visible').filter(
        has_text=re.compile(rf"^{quantity}$")
    )
    if option.count() != 1 or not option.is_visible():
        raise RuntimeError(f"购物车没有提供数量 {quantity} 的测试选项")
    option.click()

    deadline = time.monotonic() + 9
    while time.monotonic() < deadline:
        if combo.inner_text().strip() == f"Qty: {quantity}":
            return True
        body = page.locator("body").inner_text()
        if f"We currently do not have {quantity} in stock." in body:
            return False
        if "An error occurred while trying to update your cart" in body:
            raise RuntimeError("Takealot 拒绝了购物车数量更新，请稍后重试")
        page.wait_for_timeout(300)
    raise RuntimeError(f"等待数量 {quantity} 的库存校验结果超时")


def _find_product_quantity_combo(page: Page, plid: str) -> Locator:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        product_link = page.locator(
            f'a[href*="/PLID{plid}"]:visible'
        )
        if product_link.count() >= 1:
            product_row = product_link.first.locator(
                'xpath=ancestor::*[.//button[@role="combobox"]][1]'
            )
            scoped_combo = product_row.locator('button[role="combobox"]:visible')
            if scoped_combo.count() == 1:
                return scoped_combo
        page.wait_for_timeout(500)
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


def _probe_above_quick_menu(
    page: Page,
    combo: Locator,
) -> tuple[int, bool] | None:
    """Switch to custom quantity input and parse the explicit warehouse limit."""
    combo.click()
    custom_options = page.locator('[role="option"]:visible').filter(has_text="10+")
    if custom_options.count() != 1 or not custom_options.first.is_visible():
        page.keyboard.press("Escape")
        return None
    custom_options.first.click()

    deadline = time.monotonic() + 8
    quantity_input = page.locator('input[name="quantity"]:visible')
    while time.monotonic() < deadline and quantity_input.count() != 1:
        page.wait_for_timeout(250)
    if quantity_input.count() != 1:
        return None

    low = 9
    probe = HIGH_QUANTITY_PROBE
    while True:
        accepted, explicit_quantity = _submit_custom_quantity(page, probe)
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

    while low < high:
        middle = (low + high + 1) // 2
        accepted, explicit_quantity = _submit_custom_quantity(page, middle)
        if explicit_quantity is not None:
            return explicit_quantity, True
        if accepted is None:
            return None
        if accepted:
            low = middle
        else:
            high = middle - 1
    return low, True


def _submit_custom_quantity(
    page: Page,
    quantity: int,
) -> tuple[bool | None, int | None]:
    quantity_input = page.locator('input[name="quantity"]:visible')
    if quantity_input.count() != 1:
        return None, None
    quantity_input.click()
    quantity_input.press("Control+A")
    quantity_input.type(str(quantity), delay=30)
    quantity_input.press("Tab")

    update_button = page.locator("button:visible").filter(has_text="Update")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and update_button.count() != 1:
        page.wait_for_timeout(250)
    if update_button.count() != 1:
        return None, None
    update_button.click()

    page.wait_for_timeout(900)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cart_text = page.locator("body").inner_text()
        explicit_quantity = _parse_warehouse_stock_message(cart_text)
        if explicit_quantity is not None:
            return False, explicit_quantity
        if f"We currently do not have {quantity} in stock." in cart_text:
            return False, None
        if "An error occurred while trying to update your cart" in cart_text:
            return None, None
        if quantity_input.count() == 1:
            current_value = quantity_input.input_value().strip()
            if current_value == str(quantity):
                return True, None
            if current_value and current_value != str(quantity):
                return False, None
        page.wait_for_timeout(300)
    return None, None


def _find_exact_quantity(
    page: Page, plid: str
) -> tuple[int, bool, str]:
    combo = _find_product_quantity_combo(page, plid)
    combo.click()
    numeric_options: list[int] = []
    options = page.locator('[role="option"]:visible')
    for index in range(options.count()):
        text = options.nth(index).inner_text().strip()
        if text.isdigit():
            numeric_options.append(int(text))
    page.keyboard.press("Escape")
    if not numeric_options:
        raise RuntimeError("购物车数量菜单没有可识别的数字选项")
    if 9 not in numeric_options:
        maximum = max(numeric_options)
        if not _choose_quantity(page, combo, maximum):
            raise RuntimeError(f"购物车拒绝了菜单显示的最大数量 {maximum}")
        return maximum, True, "购物车数量菜单显示的当前最大可选数量。"
    if _choose_quantity(page, combo, 9):
        warehouse_result = _probe_above_quick_menu(page, combo)
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
        if _choose_quantity(page, combo, middle):
            low = middle
        else:
            high = middle - 1
    return low, True, "通过隔离匿名购物车数量校验得到的当前可售上限。"
