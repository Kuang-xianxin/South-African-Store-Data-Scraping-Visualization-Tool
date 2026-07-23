"""Read-only client for Takealot's public product and review endpoints."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
)


PUBLIC_API_BASE = "https://api.takealot.com/rest/v-1-10-0"
PLID_PATTERN = re.compile(r"PLID(\d+)", re.IGNORECASE)
PUBLIC_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-ZA,en;q=0.9",
    "origin": "https://www.takealot.com",
    "referer": "https://www.takealot.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def extract_plid(value: str) -> str:
    """Extract the numeric PLID from a Takealot product URL."""
    match = PLID_PATTERN.search(value)
    if match is None:
        raise ValueError(f"链接中未找到 PLID：{value}")
    return match.group(1)


class CompetitorPublicClient:
    """Retrying public-data client; it never calls seller-only write endpoints."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            headers=PUBLIC_HEADERS,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CompetitorPublicClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_product(self, url: str) -> CompetitorProduct:
        plid = extract_plid(url)
        detail = self._get_json(f"{PUBLIC_API_BASE}/product-details/PLID{plid}")
        variant_details = self._fetch_variant_details(detail)
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

    def _fetch_variant_details(
        self,
        root: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Resolve every selector combination while keeping one product-level PLID."""
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
                queue.append(self._get_json(href))
        return list(terminal.values()) or [root]

    def fetch_all_reviews(self, plid: str, *, page_delay_seconds: float = 0.1) -> list[CompetitorReviewRecord]:
        first = self._get_json(f"{PUBLIC_API_BASE}/product-reviews/plid/{plid}?page=0")
        page_info = _mapping(first.get("page_info"))
        total_pages = max(1, int(_number(page_info.get("total_pages"))))
        raw_reviews = list(_mapping_list(first.get("reviews")))
        for page in range(1, total_pages):
            if page_delay_seconds:
                time.sleep(page_delay_seconds)
            result = self._get_json(
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

    def _get_json(self, url: str, *, retries: int = 3) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Takealot 公开接口返回了非对象数据")
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt == retries:
                    break
                time.sleep(0.8 * (2**attempt))
        raise RuntimeError("Takealot 公开接口暂时不可用，请稍后重试") from last_error


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
