"""Read-only client for Takealot's public product and review endpoints — Playwright-backed."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
)


PUBLIC_API_BASE = "https://api.takealot.com/rest/v-1-10-0"
PLID_PATTERN = re.compile(r"PLID(\d+)", re.IGNORECASE)

BROWSER_PATHS = (
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
)


def extract_plid(value: str) -> str:
    """Extract the numeric PLID from a Takealot product URL."""
    match = PLID_PATTERN.search(value)
    if match is None:
        raise ValueError(f"链接中未找到 PLID：{value}")
    return match.group(1)


def _find_browser_executable() -> Path:
    for candidate in BROWSER_PATHS:
        if candidate.exists():
            return candidate
    raise RuntimeError("未找到 Chrome 或 Edge，需要本机浏览器")


class CompetitorPublicClient:
    """Browser-backed public-data client.

    Uses Playwright with a real Chrome/Edge browser so Cloudflare JS challenges
    pass through naturally.  Human-like random delays are inserted between
    requests to avoid secondary rate-limiting.

    Must be used as an async context manager::

        async with CompetitorPublicClient() as client:
            product = await client.fetch_product(url)
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        headless: bool = True,
    ) -> None:
        self._timeout_ms = int(timeout_seconds * 1000)
        self._headless = headless
        self._started = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        """Launch browser and warm up Cloudflare.  Called automatically by __aenter__."""
        if self._started:
            return
        executable = _find_browser_executable()
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            executable_path=str(executable),
            headless=self._headless,
            args=[
                "--disable-background-timer-throttling",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(
            locale="en-ZA",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        # Strip navigator.webdriver so headless mode isn't fingerprintable
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # One-time warm-up: pass Cloudflare's JS challenge
        await page.goto(
            "https://www.takealot.com/",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        await self._human_delay(5.0, 12.0)
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._started = True

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> CompetitorPublicClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                if self._playwright is not None:
                    await self._playwright.stop()
        self._started = False

    # ── public API (same signatures as before) ────────────────────────────

    async def fetch_product(self, url: str) -> CompetitorProduct:
        plid = extract_plid(url)
        detail = await self._get_json(f"{PUBLIC_API_BASE}/product-details/PLID{plid}")
        variant_details = await self._fetch_variant_details(detail)
        variants = tuple(_variant_record(item, plid) for item in variant_details)
        requested_key = _variant_key(url)
        selected_index = next(
            (
                index
                for index, item in enumerate(variant_details)
                if requested_key
                and _variant_key(str(item.get("desktop_href") or "")) == requested_key
            ),
            -1,
        )
        if selected_index < 0:
            selected_index = next(
                (
                    index
                    for index, item in enumerate(variant_details)
                    if bool(_selected_offer(item).get("is_add_to_cart_available"))
                ),
                0,
            )
        selected_detail = variant_details[selected_index]
        selected_variant = variants[selected_index]
        offer = _selected_offer(selected_detail)
        seller = _mapping(selected_detail.get("seller_detail"))
        compact_offers: list[CompetitorOffer] = []
        if offer:
            compact_offers.append(_offer_record(offer, seller, selected=True))
        other_offers = _mapping(selected_detail.get("other_offers"))
        for condition in _mapping_list(other_offers.get("conditions")):
            for other_offer in _mapping_list(condition.get("items")):
                compact_offers.append(
                    _offer_record(
                        other_offer,
                        _mapping(other_offer.get("seller")),
                        selected=False,
                    )
                )
        core = _mapping(detail.get("core"))
        reviews = _mapping(detail.get("reviews"))
        gallery = _mapping(detail.get("gallery"))
        images = gallery.get("images")
        image_url = str(images[0]).replace("{size}", "zoom") if isinstance(images, list) and images else None
        return CompetitorProduct(
            plid=plid,
            url=str(detail.get("desktop_href") or url),
            title=str(core.get("title") or detail.get("title") or f"PLID{plid}"),
            image_url=image_url,
            sku=selected_variant.sku,
            seller_id=selected_variant.seller_id,
            seller_name=selected_variant.seller_name,
            price=selected_variant.price,
            stock_status=selected_variant.stock_status,
            is_leadtime=selected_variant.is_leadtime,
            review_count=int(_number(reviews.get("count") or core.get("reviews"))),
            rating=_number(reviews.get("star_rating") or core.get("star_rating")),
            offers=tuple(compact_offers),
            variants=variants,
        )

    async def _fetch_variant_details(
        self,
        root: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        queue: list[Mapping[str, Any]] = [root]
        terminal: dict[str, Mapping[str, Any]] = {}
        visited: set[str] = set()
        while queue:
            detail = queue.pop(0)
            detail_url = str(detail.get("desktop_href") or "")
            state_key = _variant_key(detail_url) or "__default__"
            selectors = _mapping_list(_mapping(detail.get("variants")).get("selectors"))
            pending = next(
                (
                    selector
                    for selector in selectors
                    if not any(
                        bool(option.get("is_selected"))
                        for option in _mapping_list(selector.get("options"))
                    )
                ),
                None,
            )
            if pending is None:
                terminal[state_key] = detail
                continue
            for option in _mapping_list(pending.get("options")):
                href = str(option.get("href") or "")
                if not href or href in visited:
                    continue
                visited.add(href)
                await self._human_delay(5.0, 10.0)
                queue.append(await self._get_json(href))
        return list(terminal.values()) or [root]

    async def fetch_all_reviews(
        self, plid: str, *, page_delay_seconds: float = 0.1
    ) -> list[CompetitorReviewRecord]:
        first = await self._get_json(f"{PUBLIC_API_BASE}/product-reviews/plid/{plid}?page=0")
        page_info = _mapping(first.get("page_info"))
        total_pages = max(1, int(_number(page_info.get("total_pages"))))
        raw_reviews = list(_mapping_list(first.get("reviews")))
        for page in range(1, total_pages):
            delay = max(page_delay_seconds, random.uniform(3.0, 8.0))
            await asyncio.sleep(delay)
            result = await self._get_json(
                f"{PUBLIC_API_BASE}/product-reviews/plid/{plid}?page={page}"
            )
            raw_reviews.extend(_mapping_list(result.get("reviews")))

        unique: dict[str, CompetitorReviewRecord] = {}
        for review in raw_reviews:
            text = _mapping(review.get("text"))
            natural_key = "|".join(
                (
                    str(review.get("customer_name") or ""),
                    str(review.get("date") or ""),
                    str(text.get("title") or ""),
                    str(text.get("body") or ""),
                )
            )
            review_id = str(review.get("uuid") or hashlib.sha256(natural_key.encode()).hexdigest())
            unique[review_id] = CompetitorReviewRecord(
                review_id=review_id,
                rating=max(1, min(5, int(_number(review.get("rating")) or 1))),
                title=str(text.get("title") or ""),
                body=str(text.get("body") or ""),
                customer_name=str(review.get("customer_name") or ""),
                review_date=str(review.get("date") or ""),
            )
        return sorted(unique.values(), key=lambda item: item.review_date, reverse=True)

    # ── internal ──────────────────────────────────────────────────────────

    async def _human_delay(self, min_s: float, max_s: float) -> None:
        """Sleep for a random duration to avoid triggering bot detection."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _get_json(self, url: str, *, retries: int = 3) -> dict[str, Any]:
        """Fetch JSON by navigating the browser to the API URL.

        Uses ``page.goto()`` — a real browser navigation — so Cloudflare sees
        ``Sec-Fetch-Dest: document`` instead of an XHR ``fetch`` and passes
        the request through like a typed address-bar URL.
        """
        page = self._page
        if page is None:
            raise RuntimeError("竞品浏览器尚未启动")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                if response is None:
                    raise RuntimeError("浏览器导航未返回响应")
                status = response.status
                if status != 200:
                    retryable = status == 429 or status >= 500
                    if not retryable or attempt == retries:
                        raise RuntimeError(f"Takealot API returned {status}")
                    await self._human_delay(2.0 * (2**attempt), 4.0 * (2**attempt))
                    continue
                try:
                    payload = await response.json()
                except Exception:
                    body = await page.content()
                    if any(kw in body.lower() for kw in ("cloudflare", "cf-challenge", "checking your browser")):
                        if attempt == retries:
                            raise RuntimeError("Cloudflare 验证失败，请稍后重试")
                        await self._human_delay(3.0, 6.0)
                        continue
                    raise ValueError("Takealot 公开接口返回了非 JSON 数据")
                if not isinstance(payload, dict):
                    raise ValueError("Takealot 公开接口返回了非对象数据")
                return payload
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt == retries:
                    break
                await self._human_delay(2.0 * (2**attempt), 4.0 * (2**attempt))
        raise RuntimeError("Takealot 公开接口暂时不可用，请稍后重试") from last_error


# ── helpers (unchanged logic) ──────────────────────────────────────────────


def _offer_record(
    offer: Mapping[str, Any],
    seller: Mapping[str, Any],
    *,
    selected: bool,
) -> CompetitorOffer:
    stock = _mapping(offer.get("stock_availability"))
    return CompetitorOffer(
        selected=selected,
        sku=str(offer.get("sku") or offer.get("product_id") or ""),
        seller_id=str(seller.get("seller_id") or ""),
        seller_name=str(seller.get("display_name") or "未知卖家"),
        price=_number(offer.get("price")),
        stock_status=str(stock.get("status") or "未知"),
    )


def _selected_offer(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    buybox = _mapping(detail.get("buybox"))
    items = _mapping_list(buybox.get("items"))
    return next(
        (item for item in items if item.get("is_selected")),
        items[0] if items else {},
    )


def _variant_record(detail: Mapping[str, Any], plid: str) -> CompetitorVariant:
    buybox = _mapping(detail.get("buybox"))
    offer = _selected_offer(detail)
    seller = _mapping(detail.get("seller_detail"))
    stock = _mapping(offer.get("stock_availability"))
    is_leadtime = bool(stock.get("is_leadtime"))
    selected_values: list[str] = []
    selectors = _mapping_list(_mapping(detail.get("variants")).get("selectors"))
    for selector in selectors:
        selected = next(
            (
                option
                for option in _mapping_list(selector.get("options"))
                if option.get("is_selected")
            ),
            None,
        )
        if selected is not None:
            title = str(selector.get("title") or selector.get("selector_type") or "选项")
            selected_values.append(f"{title}：{selected.get('value') or ''}")
    url = str(detail.get("desktop_href") or f"https://www.takealot.com/PLID{plid}")
    return CompetitorVariant(
        key=_variant_key(url) or "default",
        label=" / ".join(selected_values) if selected_values else "默认款",
        url=url,
        title=str(detail.get("title") or f"PLID{plid}"),
        sku=str(offer.get("sku") or buybox.get("tsin") or ""),
        seller_id=str(seller.get("seller_id") or ""),
        seller_name=str(seller.get("display_name") or "未知卖家"),
        price=_number(offer.get("price")),
        stock_status=(
            "没货（非平台仓/供应商调货）"
            if is_leadtime
            else str(stock.get("status") or "未知")
        ),
        is_leadtime=is_leadtime,
        is_add_to_cart_available=bool(offer.get("is_add_to_cart_available")),
    )


def _variant_key(url: str) -> str:
    parts = urlsplit(url)
    values = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "platform"
    ]
    return urlencode(sorted(values))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
