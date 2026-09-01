"""Read-only client for Takealot's public product and review endpoints — Playwright-backed."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from takealot_ops.competitors.domain import (
    CompetitorCategoryBreadcrumb,
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
    TAKEALOT_RETAIL_SELLER_ID,
    TAKEALOT_RETAIL_SELLER_NAME,
    competitor_offer_identity,
)


PUBLIC_API_BASE = "https://api.takealot.com/rest/v-1-10-0"
PLID_PATTERN = re.compile(r"PLID(\d+)", re.IGNORECASE)
API_QUERY_SEPARATOR = re.compile(r"&(?=[A-Za-z0-9_.~-]+=)")

BROWSER_PATHS = (
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
)


class CompetitorNetworkError(RuntimeError):
    """Temporary Takealot connectivity failure that may succeed after recovery."""


class CompetitorNotFoundError(RuntimeError):
    """A product-details endpoint returned HTTP 404 and needs cross-checking."""


class CompetitorPageValidationError(RuntimeError):
    """A product-page cross-check was inconclusive but connectivity still worked."""


ProductPageState = Literal["product", "not-found", "uncertain"]
PageValidationDiagnostic = Callable[[str], None]
PRODUCT_PAGE_VALIDATION_ATTEMPTS = 7
PRODUCT_PAGE_VALIDATION_POLL_MS = 2_000


def _is_retryable_takealot_status(status: int) -> bool:
    """Classify temporary edge/network responses that may recover with the VPN."""
    return status in {403, 429} or status >= 500


def _is_matching_listing_response_url(
    response_url: str,
    *,
    expected_qsearch: str | None = None,
) -> bool:
    """Accept only the public product payload belonging to the requested search."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.takealot.com"
        or "/searches/products," not in parsed.path
    ):
        return False
    if expected_qsearch is None:
        return True
    response_query = dict(parse_qsl(parsed.query, keep_blank_values=True)).get(
        "qsearch",
        "",
    )
    return " ".join(response_query.casefold().split()) == " ".join(
        expected_qsearch.casefold().split()
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
        search_endpoint_retries: int = 3,
    ) -> None:
        self._timeout_ms = int(timeout_seconds * 1000)
        self._headless = headless
        self._search_endpoint_retries = max(0, min(3, search_endpoint_retries))
        self._started = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def ready(self) -> bool:
        """Return whether the current browser session can be reused."""
        return bool(
            self._started
            and self._browser is not None
            and self._browser.is_connected()
            and self._page is not None
            and not self._page.is_closed()
        )

    async def start(self) -> None:
        """Launch browser and warm up Cloudflare.  Called automatically by __aenter__."""
        if self.ready:
            return
        if self._started or any(
            item is not None
            for item in (self._page, self._context, self._browser, self._playwright)
        ):
            await self.close()
        executable = _find_browser_executable()
        playwright = await async_playwright().start()
        self._playwright = playwright
        try:
            browser = await playwright.chromium.launch(
                executable_path=str(executable),
                headless=self._headless,
                args=[
                    "--disable-background-timer-throttling",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            self._browser = browser
            context = await browser.new_context(
                locale="en-ZA",
                viewport={"width": 1365, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            self._context = context
            page = await context.new_page()
            self._page = page
            # Strip navigator.webdriver so headless mode isn't fingerprintable.
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            # One-time warm-up: tolerate short proxy/VPN interruptions.
            for attempt in range(3):
                try:
                    await page.goto(
                        "https://www.takealot.com/",
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == 2:
                        raise
                    await self._human_delay(2.0 * (2**attempt), 4.0 * (2**attempt))
            await self._human_delay(3.0, 5.0)
            self._started = True
        except asyncio.CancelledError:
            await self._close_after_failed_start()
            raise
        except Exception as exc:
            await self._close_after_failed_start()
            raise CompetitorNetworkError(
                "Takealot 当前无法访问，请检查梯子或代理连接后重试"
            ) from exc

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> CompetitorPublicClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        context = self._context
        browser = self._browser
        playwright = self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._started = False
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if playwright is not None:
                    await playwright.stop()

    async def _close_after_failed_start(self) -> None:
        try:
            await self.close()
        except BaseException:
            # Preserve the original cancellation/network error after best-effort cleanup.
            pass

    # ── public API (same signatures as before) ────────────────────────────

    async def confirm_product_page_absent(
        self,
        target_url: str,
        control_products: str | Sequence[tuple[str, str]],
        *,
        diagnostic_callback: PageValidationDiagnostic | None = None,
    ) -> str:
        """Cross-check one 404 against recently successful control products.

        Returning normally means the target page explicitly renders Takealot's
        not-found state while one control page renders product-specific content;
        the returned value is the PLID of that verified control product.
        Ambiguous rendered content is kept separate from real connectivity
        failures so it cannot trip the batch network circuit breaker.
        """
        target_plid = _optional_plid(target_url)
        target_state = await self._product_page_state(
            target_url,
            role="target",
            plid=target_plid,
            diagnostic_callback=diagnostic_callback,
        )
        if target_state == "product":
            raise CompetitorPageValidationError(
                "目标商品页仍可打开，但公开数据接口暂时返回 404；已保留重试"
            )
        if target_state != "not-found":
            raise CompetitorPageValidationError(
                "目标商品页未能稳定识别；网络可以访问，但本次复核结果不确定，已保留重试"
            )

        controls = (
            ((_optional_plid(control_products), control_products),)
            if isinstance(control_products, str)
            else tuple(control_products)
        )
        first_network_error: CompetitorNetworkError | None = None
        saw_non_network_response = False
        for control_plid, control_url in controls:
            try:
                control_state = await self._product_page_state(
                    control_url,
                    role="control",
                    plid=control_plid,
                    diagnostic_callback=diagnostic_callback,
                )
            except CompetitorNetworkError as exc:
                if first_network_error is None:
                    first_network_error = exc
                continue
            saw_non_network_response = True
            if control_state == "product":
                verified_plid = control_plid or _optional_plid(control_url)
                _emit_page_validation_diagnostic(
                    diagnostic_callback,
                    (
                        "page_validation outcome=confirmed "
                        f"target_plid={target_plid or '-'} "
                        f"control_plid={verified_plid or '-'}"
                    ),
                )
                return verified_plid

        if first_network_error is not None and not saw_non_network_response:
            raise first_network_error
        raise CompetitorPageValidationError(
            "最近的正常对照商品页均未能稳定识别；网络可以访问，但本次复核结果不确定，已保留重试"
        )

    async def fetch_product(self, url: str) -> CompetitorProduct:
        plid = extract_plid(url)
        detail = await self._get_json(f"{PUBLIC_API_BASE}/product-details/PLID{plid}")
        variant_details = await self._fetch_variant_details(detail)
        variants = tuple(_variant_record(item, plid) for item in variant_details)
        requested_keys = {
            _variant_key(url),
            _variant_key(_normalise_api_desktop_href(url)),
        }
        requested_keys.discard("")
        selected_index = next(
            (
                index
                for index, item in enumerate(variant_details)
                if requested_keys
                and _variant_key(
                    _normalise_api_desktop_href(
                        str(item.get("desktop_href") or "")
                    )
                )
                in requested_keys
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
        compact_offers: list[CompetitorOffer] = []
        for detail_index, offer_detail in enumerate(variant_details):
            variant = variants[detail_index]
            variant_seller = _mapping(offer_detail.get("seller_detail"))
            buybox_items = _mapping_list(
                _mapping(offer_detail.get("buybox")).get("items")
            )
            known_sellers = _known_offer_sellers(offer_detail)
            for buybox_rank, variant_offer in enumerate(buybox_items):
                selected = bool(variant_offer.get("is_selected"))
                compact_offers.append(
                    _offer_record(
                        variant_offer,
                        _buybox_offer_seller(
                            variant_offer,
                            selected_seller=variant_seller,
                            known_sellers=known_sellers,
                        ),
                        selected=selected,
                        is_buybox=True,
                        fallback_url=str(
                            offer_detail.get("desktop_href")
                            or detail.get("desktop_href")
                            or url
                        ),
                        condition=_condition_label(variant_offer.get("condition")),
                        variant_key=variant.key,
                        variant_label=variant.label,
                        buybox_rank=buybox_rank,
                        is_follower_offer=not selected,
                    )
                )
            other_offers = _mapping(offer_detail.get("other_offers"))
            for condition in _mapping_list(other_offers.get("conditions")):
                condition_label = _condition_label(condition)
                for other_offer in _mapping_list(condition.get("items")):
                    compact_offers.append(
                        _offer_record(
                            other_offer,
                            _public_offer_seller(other_offer),
                            selected=False,
                            is_buybox=False,
                            fallback_url=str(
                                offer_detail.get("desktop_href")
                                or detail.get("desktop_href")
                                or url
                            ),
                            condition=condition_label,
                            variant_key=variant.key,
                            variant_label=variant.label,
                            is_follower_offer=True,
                        )
                    )
        core = _mapping(detail.get("core"))
        reviews = _mapping(detail.get("reviews"))
        image_url = _product_image_url(selected_detail)
        return CompetitorProduct(
            plid=plid,
            url=_normalise_api_desktop_href(
                str(detail.get("desktop_href") or url)
            ),
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
            category_path=_category_breadcrumbs(detail),
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
            detail_url = _normalise_api_desktop_href(
                str(detail.get("desktop_href") or "")
            )
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
                await self._human_delay(2.5, 4.0)
                queue.append(await self._get_json(href))
        return list(terminal.values()) or [root]

    async def fetch_all_reviews(
        self, plid: str, *, page_delay_seconds: float | None = None
    ) -> list[CompetitorReviewRecord]:
        first = await self._get_json(f"{PUBLIC_API_BASE}/product-reviews/plid/{plid}?page=0")
        page_info = _mapping(first.get("page_info"))
        total_pages = max(1, int(_number(page_info.get("total_pages"))))
        raw_reviews = list(_mapping_list(first.get("reviews")))
        for page in range(1, total_pages):
            delay = (
                max(0.0, page_delay_seconds)
                if page_delay_seconds is not None
                else random.uniform(1.0, 3.0)
            )
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

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        """Capture the current public search payload behind Takealot's rendered page."""
        query = " ".join(keyword.split())
        if not query or len(query) > 200:
            raise ValueError("搜索词必须为1到200个字符")
        return await self.fetch_listing_first_page(
            f"https://www.takealot.com/all?{urlencode({'qsearch': query})}",
            expected_qsearch=query,
        )

    async def fetch_listing_first_page(
        self,
        source_url: str,
        *,
        expected_qsearch: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Capture one safe Takealot seller/category/search listing response."""
        parsed = urlsplit(source_url)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or (hostname != "takealot.com" and not hostname.endswith(".takealot.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Takealot 列表地址无效")
        page = self._page
        if page is None:
            raise RuntimeError("竞品浏览器尚未启动")
        loop = asyncio.get_running_loop()
        captured: asyncio.Future[tuple[str, dict[str, Any]]] = loop.create_future()
        tasks: set[asyncio.Task[None]] = set()

        async def capture(response: Any) -> None:
            if captured.done():
                return
            try:
                payload = await response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Takealot 搜索接口没有返回对象")
                captured.set_result((response.url, payload))
            except Exception as exc:
                if not captured.done():
                    captured.set_exception(exc)

        def on_response(response: Any) -> None:
            if captured.done() or not _is_matching_listing_response_url(
                response.url,
                expected_qsearch=expected_qsearch,
            ):
                return
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            response = await page.goto(
                source_url,
                wait_until="domcontentloaded",
                timeout=max(self._timeout_ms, 45_000),
            )
            if response is None or _is_retryable_takealot_status(response.status):
                raise CompetitorNetworkError("Takealot 搜索页暂时无法访问")
            return await asyncio.wait_for(captured, timeout=max(15, self._timeout_ms / 1000))
        except asyncio.TimeoutError as exc:
            raise CompetitorNetworkError("Takealot 搜索结果接口响应超时") from exc
        finally:
            page.remove_listener("response", on_response)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        """Follow one server-issued organic-search cursor on the same safe endpoint."""
        parsed = urlsplit(request_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.takealot.com"
            or "/searches/products," not in parsed.path
            or not after
        ):
            raise ValueError("Takealot 搜索游标地址无效")
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.pop("before", None)
        params.pop("start", None)
        params["after"] = after
        next_url = parsed._replace(query=urlencode(params)).geturl()
        return await self._get_json(
            next_url,
            retries=self._search_endpoint_retries,
        )

    async def fetch_search_suggestions(self, keyword: str) -> list[str]:
        """Return Takealot's ranked search-box completions for a shopper prefix."""
        query = " ".join(keyword.split())
        if not query or len(query) > 100:
            raise ValueError("补全词根必须为1到100个字符")
        payload = await self._get_json(
            f"{PUBLIC_API_BASE}/searches/search_suggestions?"
            f"{urlencode({'qsearch': query})}",
            retries=self._search_endpoint_retries,
        )
        sections = payload.get("sections")
        section = (
            sections.get("search_suggestions")
            if isinstance(sections, Mapping)
            else None
        )
        results = section.get("results") if isinstance(section, Mapping) else None
        if not isinstance(results, list):
            raise CompetitorNetworkError("Takealot 搜索框补全响应缺少结果列表")
        output: list[str] = []
        seen: set[str] = set()
        for result in results:
            suggestion = (
                result.get("search_suggestion")
                if isinstance(result, Mapping)
                else None
            )
            phrase = " ".join(
                str(suggestion.get("qsearch") or "").split()
            ) if isinstance(suggestion, Mapping) else ""
            normalized = phrase.casefold()
            if not phrase or normalized in seen:
                continue
            seen.add(normalized)
            output.append(phrase)
        return output

    # ── internal ──────────────────────────────────────────────────────────

    async def _human_delay(self, min_s: float, max_s: float) -> None:
        """Sleep for a random duration to avoid triggering bot detection."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _product_page_state(
        self,
        url: str,
        *,
        role: str = "page",
        plid: str | None = None,
        diagnostic_callback: PageValidationDiagnostic | None = None,
    ) -> ProductPageState:
        page = self._page
        if page is None:
            raise RuntimeError("竞品浏览器尚未启动")
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit_product_page_observation(
                diagnostic_callback,
                role=role,
                plid=plid,
                state="uncertain",
                status=None,
                final_url=_page_url(page, url),
                error=type(exc).__name__,
            )
            raise CompetitorNetworkError(
                "Takealot 商品页暂时无法访问；本次按网络问题保留重试"
            ) from exc
        if response is None:
            _emit_product_page_observation(
                diagnostic_callback,
                role=role,
                plid=plid,
                state="uncertain",
                status=None,
                final_url=_page_url(page, url),
                error="no-response",
            )
            raise CompetitorNetworkError("Takealot 商品页暂时无法访问；本次按网络问题保留重试")
        status = response.status
        if _is_retryable_takealot_status(status):
            _emit_product_page_observation(
                diagnostic_callback,
                role=role,
                plid=plid,
                state="uncertain",
                status=status,
                final_url=_page_url(page, url),
                error="retryable-status",
            )
            raise CompetitorNetworkError("Takealot 商品页暂时无法访问；本次按网络问题保留重试")
        if status == 404:
            _emit_product_page_observation(
                diagnostic_callback,
                role=role,
                plid=plid,
                state="not-found",
                status=status,
                final_url=_page_url(page, url),
            )
            return "not-found"

        heading = page.locator("main h1")
        title = ""
        headings: list[str] = []
        generic_shell = False
        challenge = False
        for attempt in range(PRODUCT_PAGE_VALIDATION_ATTEMPTS):
            title = _normalized_page_text(await page.title())
            headings = [
                _normalized_page_text(value)
                for value in await heading.all_text_contents()
                if _normalized_page_text(value)
            ]
            challenge = _is_takealot_challenge(title) or any(
                _is_takealot_challenge(value) for value in headings
            )
            generic_shell = _is_takealot_generic_shell(title, headings)
            if challenge:
                _emit_product_page_observation(
                    diagnostic_callback,
                    role=role,
                    plid=plid,
                    state="uncertain",
                    status=status,
                    final_url=_page_url(page, url),
                    title=title,
                    headings=headings,
                    generic_shell=generic_shell,
                    challenge=True,
                    error="challenge",
                )
                raise CompetitorNetworkError(
                    "Takealot 商品页触发访问验证；本次按网络问题保留重试"
                )
            if _is_takealot_not_found(title) or any(
                _is_takealot_not_found(value) for value in headings
            ):
                state: ProductPageState = "not-found"
                break
            if not generic_shell and (headings or _is_takealot_product_title(title)):
                state = "product"
                break
            if attempt < PRODUCT_PAGE_VALIDATION_ATTEMPTS - 1:
                await page.wait_for_timeout(PRODUCT_PAGE_VALIDATION_POLL_MS)
        else:
            state = "uncertain"

        _emit_product_page_observation(
            diagnostic_callback,
            role=role,
            plid=plid,
            state=state,
            status=status,
            final_url=_page_url(page, url),
            title=title,
            headings=headings,
            generic_shell=generic_shell,
            challenge=challenge,
        )
        return state

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
                    raise CompetitorNetworkError("浏览器导航未返回响应")
                status = response.status
                if status != 200:
                    if status == 404:
                        raise CompetitorNotFoundError("Takealot 商品数据返回 404")
                    retryable = _is_retryable_takealot_status(status)
                    if not retryable or attempt == retries:
                        error_type = CompetitorNetworkError if retryable else RuntimeError
                        raise error_type(f"Takealot API returned {status}")
                    await self._human_delay(2.0 * (2**attempt), 4.0 * (2**attempt))
                    continue
                try:
                    payload = await response.json()
                except Exception:
                    body = await page.content()
                    if any(
                        kw in body.lower()
                        for kw in ("cloudflare", "cf-challenge", "checking your browser")
                    ):
                        if attempt == retries:
                            raise CompetitorNetworkError("Cloudflare 验证失败，请稍后重试")
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
        raise CompetitorNetworkError(
            "Takealot 公开接口暂时不可用，请检查梯子或代理连接后重试"
        ) from last_error


# ── helpers (unchanged logic) ──────────────────────────────────────────────


def _normalized_page_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _optional_plid(value: str) -> str:
    match = PLID_PATTERN.search(value)
    return match.group(1) if match is not None else ""


def _page_url(page: Page, fallback: str) -> str:
    current = getattr(page, "url", "")
    return current if isinstance(current, str) and current else fallback


def _is_takealot_challenge(value: str) -> bool:
    return any(
        marker in value
        for marker in (
            "just a moment",
            "checking your browser",
            "cloudflare",
            "verify you are human",
        )
    )


def _is_takealot_generic_shell(title: str, headings: Sequence[str]) -> bool:
    return not headings and (
        "takealot.com: online shopping" in title
        or "sa's leading online store" in title
        or title in {"takealot.com", "takealot"}
    )


def _emit_page_validation_diagnostic(
    callback: PageValidationDiagnostic | None,
    message: str,
) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        # Diagnostics must never alter the conservative validation result.
        pass


def _emit_product_page_observation(
    callback: PageValidationDiagnostic | None,
    *,
    role: str,
    plid: str | None,
    state: ProductPageState,
    status: int | None,
    final_url: str,
    title: str = "",
    headings: Sequence[str] = (),
    generic_shell: bool = False,
    challenge: bool = False,
    error: str = "",
) -> None:
    clean_title = " ".join(title.split())[:240]
    clean_heading = " | ".join(" ".join(value.split()) for value in headings)[:240]
    clean_url = " ".join(final_url.split())[:500]
    clean_error = " ".join(error.split())[:120]
    _emit_page_validation_diagnostic(
        callback,
        (
            f"page_validation role={role} plid={plid or '-'} state={state} "
            f"status={status if status is not None else '-'} "
            f"final_url={clean_url or '-'} title={clean_title!r} h1={clean_heading!r} "
            f"generic_shell={str(generic_shell).lower()} "
            f"challenge={str(challenge).lower()} error={clean_error or '-'}"
        ),
    )


def _is_takealot_not_found(value: str) -> bool:
    return "404" in value and ("page not found" in value or "not found" in value)


def _is_takealot_product_title(value: str) -> bool:
    return "| takealot.com" in value and not _is_takealot_generic_shell(value, ())


def _offer_record(
    offer: Mapping[str, Any],
    seller: Mapping[str, Any],
    *,
    selected: bool,
    is_buybox: bool,
    fallback_url: str | None = None,
    condition: str | None = None,
    variant_key: str = "default",
    variant_label: str = "默认款",
    buybox_rank: int | None = None,
    is_follower_offer: bool = False,
) -> CompetitorOffer:
    stock = _mapping(offer.get("stock_availability"))
    offer_url, offer_plid = _offer_target(offer, fallback_url=fallback_url)
    offer_id = str(
        offer.get("offer_id")
        or offer.get("id")
        or (offer.get("sku") if is_buybox else "")
        or ""
    ).strip() or None
    seller_id = str(seller.get("seller_id") or "").strip()
    seller_name = str(seller.get("display_name") or "未知卖家")
    sku = str(offer.get("sku") or offer.get("product_id") or "").strip()
    add_to_cart_value = offer.get("is_add_to_cart_available")
    if not isinstance(add_to_cart_value, bool):
        add_to_cart_value = stock.get("is_in_stock")
    is_add_to_cart_available = (
        add_to_cart_value if isinstance(add_to_cart_value, bool) else None
    )
    return CompetitorOffer(
        selected=selected,
        sku=sku,
        seller_id=seller_id,
        seller_name=seller_name,
        price=_number(offer.get("price")),
        stock_status=str(stock.get("status") or "未知"),
        is_buybox=is_buybox,
        is_leadtime=bool(stock.get("is_leadtime")),
        is_add_to_cart_available=is_add_to_cart_available,
        plid=offer_plid,
        url=offer_url,
        offer_id=offer_id,
        condition=condition,
        variant_key=variant_key,
        variant_label=variant_label,
        identity_key=competitor_offer_identity(
            offer_id=offer_id,
            seller_id=seller_id,
            seller_name=seller_name,
            sku=sku,
            variant_key=variant_key,
            condition=condition,
        ),
        buybox_rank=buybox_rank,
        is_follower_offer=is_follower_offer,
    )


def _normalised_public_seller_id(value: object) -> str:
    seller_id = str(value or "").strip()
    return seller_id[1:] if seller_id[:1].casefold() == "m" else seller_id


def _public_offer_seller(offer: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the authoritative seller identity embedded in one public offer."""
    if offer.get("is_takealot") is True:
        return {
            "seller_id": TAKEALOT_RETAIL_SELLER_ID,
            "display_name": TAKEALOT_RETAIL_SELLER_NAME,
        }
    return _mapping(offer.get("seller"))


def _known_offer_sellers(detail: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sellers: dict[str, Mapping[str, Any]] = {}
    selected_seller = _mapping(detail.get("seller_detail"))
    selected_id = _normalised_public_seller_id(selected_seller.get("seller_id"))
    if selected_id:
        sellers[selected_id] = selected_seller
    other_offers = _mapping(detail.get("other_offers"))
    for condition in _mapping_list(other_offers.get("conditions")):
        for offer in _mapping_list(condition.get("items")):
            seller = _public_offer_seller(offer)
            seller_id = _normalised_public_seller_id(seller.get("seller_id"))
            if seller_id:
                sellers[seller_id] = seller
    return sellers


def _buybox_offer_seller(
    offer: Mapping[str, Any],
    *,
    selected_seller: Mapping[str, Any],
    known_sellers: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    if offer.get("is_takealot") is True:
        return _public_offer_seller(offer)
    embedded = _mapping(offer.get("seller"))
    seller_id = _normalised_public_seller_id(
        embedded.get("seller_id") or offer.get("sponsored_ads_seller_id")
    )
    if not seller_id and bool(offer.get("is_selected")):
        seller_id = _normalised_public_seller_id(selected_seller.get("seller_id"))
    if seller_id in known_sellers:
        return known_sellers[seller_id]
    display_name = str(embedded.get("display_name") or "").strip()
    return {
        "seller_id": seller_id,
        "display_name": display_name or (
            f"卖家ID {seller_id}（平台未返回名称）" if seller_id else "未知卖家"
        ),
    }


def _condition_label(value: object) -> str | None:
    if isinstance(value, Mapping):
        mapped = value
        label = (
            mapped.get("display_name")
            or mapped.get("title")
            or mapped.get("name")
            or mapped.get("condition")
        )
    else:
        label = value
    normalized = " ".join(str(label or "").split())
    return normalized or None


def _offer_target(
    offer: Mapping[str, Any],
    *,
    fallback_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Return only an explicitly identifiable Takealot target for an offer."""
    product = _mapping(offer.get("product"))
    candidates = [
        offer.get("desktop_href"),
        offer.get("href"),
        offer.get("url"),
        offer.get("product_url"),
        product.get("desktop_href"),
        product.get("href"),
        product.get("url"),
        product.get("product_url"),
        offer.get("plid"),
        offer.get("productline_id"),
        offer.get("product_id"),
        product.get("plid"),
        product.get("productline_id"),
        product.get("product_id"),
        fallback_url,
    ]
    for candidate in candidates:
        # A Takealot variant value can legitimately end with whitespace.  Remove
        # only accidental leading padding around an API field; the URL normalizer
        # below must still see any trailing query-value whitespace so it can encode
        # the exact SKU selector instead of silently opening another variant.
        value = str(candidate or "").lstrip()
        if not value.strip():
            continue
        match = PLID_PATTERN.search(value)
        if match is None:
            continue
        plid = match.group(1)
        if value.casefold().startswith(("http://", "https://")):
            hostname = (urlsplit(value).hostname or "").casefold()
            if hostname != "takealot.com" and not hostname.endswith(".takealot.com"):
                continue
            return _normalise_api_desktop_href(value), plid
        return f"https://www.takealot.com/product/PLID{plid}", plid
    return None, None


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
    selected_seller = _mapping(detail.get("seller_detail"))
    seller = _buybox_offer_seller(
        offer,
        selected_seller=selected_seller,
        known_sellers=_known_offer_sellers(detail),
    )
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
            value = _selector_option_display_value(selected)
            if value:
                selected_values.append(f"{title}：{value}")
    url = _normalise_api_desktop_href(
        str(detail.get("desktop_href") or f"https://www.takealot.com/PLID{plid}")
    )
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
            "没货（非平台仓/供应商调货）" if is_leadtime else str(stock.get("status") or "未知")
        ),
        is_leadtime=is_leadtime,
        is_add_to_cart_available=bool(offer.get("is_add_to_cart_available")),
        image_url=_product_image_url(detail),
    )


def _selector_option_display_value(option: Mapping[str, Any]) -> str:
    """Return a human-readable selector value without serializing API objects."""
    value: object = option.get("value")
    if isinstance(value, Mapping):
        for key in ("name", "label", "value", "title"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                return str(candidate).strip()
        value = None
    if isinstance(value, (str, int, float)) and str(value).strip():
        return str(value).strip()
    for key in ("name", "label", "title"):
        candidate = option.get(key)
        if isinstance(candidate, (str, int, float)) and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _product_image_url(detail: Mapping[str, Any]) -> str | None:
    gallery = _mapping(detail.get("gallery"))
    images = gallery.get("images")
    if not isinstance(images, list) or not images:
        return None
    return str(images[0]).replace("{size}", "zoom")


def _category_breadcrumbs(
    detail: Mapping[str, Any],
) -> tuple[CompetitorCategoryBreadcrumb, ...]:
    """Preserve the bounded public department/category path without guessing."""

    breadcrumbs = _mapping_list(_mapping(detail.get("breadcrumbs")).get("items"))
    result: list[CompetitorCategoryBreadcrumb] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in breadcrumbs[:12]:
        name = " ".join(str(item.get("name") or "").split())[:200]
        if not name:
            continue
        raw_id = item.get("id")
        category_id = (
            str(raw_id).strip()[:100]
            if isinstance(raw_id, (str, int, float)) and str(raw_id).strip()
            else None
        )
        raw_type = item.get("type")
        category_type = (
            str(raw_type).strip()[:50]
            if isinstance(raw_type, (str, int, float)) and str(raw_type).strip()
            else None
        )
        raw_slug = item.get("slug")
        slug = (
            str(raw_slug).strip()[:255]
            if isinstance(raw_slug, (str, int, float)) and str(raw_slug).strip()
            else None
        )
        identity = (name.casefold(), category_id, category_type)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            CompetitorCategoryBreadcrumb(
                name=name,
                category_id=category_id,
                category_type=category_type,
                slug=slug,
            )
        )
    return tuple(result)


def _variant_key(url: str) -> str:
    parts = urlsplit(url)
    values = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "platform"
    ]
    return urlencode(sorted(values))


def _normalise_api_desktop_href(url: str) -> str:
    """Encode literal variant values from Takealot's display-oriented PDP URLs.

    The public API can return ``desktop_href`` values with unescaped ampersands,
    literal leading plus signs, or significant trailing spaces.  Passing those
    strings directly to a browser leaves the variant selectors incomplete.  Split
    only on ampersands that introduce another ``key=`` pair, then encode the exact
    API value so Playwright opens the intended SKU.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url

    query: list[tuple[str, str]] = []
    for component in API_QUERY_SEPARATOR.split(parts.query):
        key, separator, value = component.partition("=")
        if not key:
            continue
        query.append((unquote(key), unquote(value if separator else "")))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


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
