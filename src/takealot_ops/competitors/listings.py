"""Discover competitor product links from safe Takealot seller/category listings."""

from __future__ import annotations

import asyncio
import copy
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit


ListingSourceType = Literal["seller", "category"]

MAX_LISTING_PRODUCTS = 1000
BALANCED_LISTING_SELECTION_RULE = "balanced_rank_fusion_then_plid_deduplicate"
LISTING_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Relevance", "相关度"),
    ("Price Descending", "价格：从高到低"),
    ("Price Ascending", "价格：从低到高"),
    ("Rating Descending", "评分最高"),
    ("ReleaseDate Descending", "最新上架"),
)
_LISTING_SORT_VALUES = {value for value, _ in LISTING_SORT_OPTIONS}
_PRICE_SORT_VALUES = frozenset({"Price Descending", "Price Ascending"})
_ORGANIC_RESULT_TYPE = "product_views"
_SEO_CATEGORY_PATH_RE = re.compile(
    r"^/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)*"
    r"(?P<label>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<category_id>\d{1,12})$",
    re.IGNORECASE,
)


class CompetitorListingInputError(ValueError):
    """The submitted source/filter combination is not safe or supported."""


class CompetitorListingProviderError(RuntimeError):
    """Takealot returned a listing payload without its required product contract."""


class CompetitorListingPreviewExpiredError(LookupError):
    """A human-confirmation preview is missing, expired, or belongs to another user."""


@dataclass(frozen=True)
class CompetitorListingSource:
    source_type: ListingSourceType
    source_url: str
    source_label: str
    default_sort: str


@dataclass(frozen=True)
class CompetitorListingProduct:
    plid: str
    title: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"plid": self.plid, "title": self.title, "url": self.url}


@dataclass(frozen=True)
class CompetitorListingSelection:
    product: CompetitorListingProduct
    sort_ranks: Mapping[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            **self.product.as_dict(),
            "sort_ranks": dict(self.sort_ranks),
        }


@dataclass(frozen=True)
class CompetitorListingPage:
    products: tuple[CompetitorListingProduct, ...]
    total: int | None
    next_after: str | None
    sort_options: tuple[tuple[str, str], ...]


class CompetitorListingClient(Protocol):
    async def fetch_listing_first_page(
        self,
        source_url: str,
    ) -> tuple[str, dict[str, Any]]: ...

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _StoredListingPreview:
    user_id: int
    expires_at: float
    payload: dict[str, object]


class CompetitorListingPreviewRegistry:
    """Keep bounded user-specific previews between manual review and commit."""

    def __init__(self, *, ttl_seconds: float = 20 * 60, max_entries: int = 64) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("preview registry bounds must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._items: dict[str, _StoredListingPreview] = {}
        self._lock = Lock()

    def issue(self, *, user_id: int, payload: Mapping[str, object]) -> str:
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            while len(self._items) >= self._max_entries:
                oldest = min(self._items, key=lambda token: self._items[token].expires_at)
                self._items.pop(oldest, None)
            token = secrets.token_urlsafe(24)
            self._items[token] = _StoredListingPreview(
                user_id=user_id,
                expires_at=now + self._ttl_seconds,
                payload=copy.deepcopy(dict(payload)),
            )
        return token

    def resolve(self, *, token: str, user_id: int) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            item = self._items.get(token)
            if item is None or item.user_id != user_id:
                raise CompetitorListingPreviewExpiredError(
                    "筛选预览已失效，请重新预览后再确认加入"
                )
            return copy.deepcopy(item.payload)

    def discard(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def _remove_expired(self, now: float) -> None:
        self._items = {
            token: item for token, item in self._items.items() if item.expires_at > now
        }


def parse_competitor_listing_source(
    value: str,
    *,
    expected_type: ListingSourceType | None = None,
) -> CompetitorListingSource:
    """Validate and canonicalize one frontend seller/category URL."""

    raw_url = value.strip()
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise CompetitorListingInputError("链接格式无效") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or (hostname != "takealot.com" and not hostname.endswith(".takealot.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise CompetitorListingInputError("请输入 Takealot 店铺或类目链接")

    query = parse_qs(parsed.query, keep_blank_values=True)
    path = parsed.path.rstrip("/") or "/"
    default_sort = _single_query_value(query, "sort") or "Relevance"
    if default_sort not in _LISTING_SORT_VALUES:
        default_sort = "Relevance"

    if path.casefold().startswith("/seller/"):
        seller_id = _single_query_value(query, "sellers")
        if not seller_id or not seller_id.isdigit() or len(seller_id) > 30:
            raise CompetitorListingInputError("店铺链接缺少有效的 sellers 店铺编号")
        source_type: ListingSourceType = "seller"
        slug = path.split("/")[-1].strip()
        source_label = slug.replace("-", " ") or f"Seller {seller_id}"
        identity_query = {"sellers": seller_id}
    elif path.casefold() == "/all":
        category_key = _single_query_value(query, "custom")
        if not category_key or len(category_key) > 200:
            raise CompetitorListingInputError("类目链接缺少有效的 custom 类目标识")
        source_type = "category"
        source_label = category_key.replace("-", " ")
        identity_query = {"custom": category_key}
    elif category_match := _SEO_CATEGORY_PATH_RE.fullmatch(path):
        source_type = "category"
        source_label = category_match.group("label").replace("-", " ")
        identity_query = {}
    else:
        raise CompetitorListingInputError(
            "无法识别该链接；店铺链接应包含 /seller/ 和 sellers，类目链接应为"
            "末段带数字类目 ID 的 Takealot 类目路径或 /all?custom=..."
        )

    if expected_type is not None and source_type != expected_type:
        expected_label = "店铺" if expected_type == "seller" else "类目"
        raise CompetitorListingInputError(f"当前入口只接受 Takealot {expected_label}链接")
    identity_query_string = urlencode(identity_query)
    canonical_url = f"https://www.takealot.com{path}"
    if identity_query_string:
        canonical_url = f"{canonical_url}?{identity_query_string}"
    return CompetitorListingSource(
        source_type=source_type,
        source_url=canonical_url,
        source_label=source_label,
        default_sort=default_sort,
    )


def build_competitor_listing_url(
    source: CompetitorListingSource,
    *,
    sort: str,
    price_min: int | None,
    price_max: int | None,
) -> str:
    """Build the exact frontend URL that causes Takealot to apply the chosen filters."""

    normalized_sort = _validated_sort(sort)
    normalized_min, normalized_max = _validated_price_range(price_min, price_max)
    parsed = urlsplit(source.source_url)
    params = [
        (key, value)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        for value in values
    ]
    params.append(("sort", normalized_sort))
    if normalized_min is not None or normalized_max is not None:
        lower = normalized_min if normalized_min is not None else 0
        upper = normalized_max if normalized_max is not None else "*"
        params.append(("filter", f"Price:{lower}-{upper}"))
    return parsed._replace(query=urlencode(params), fragment="").geturl()


async def preview_competitor_listing(
    client: CompetitorListingClient,
    *,
    source_url: str,
    source_type: ListingSourceType,
    price_min: int | None,
    price_max: int | None,
    sorts: Sequence[str],
    product_limit: int | None,
    page_delay_seconds: float = 1.5,
) -> dict[str, object]:
    """Collect and freeze a reusable ranked candidate queue across selected sorts."""

    source = parse_competitor_listing_source(source_url, expected_type=source_type)
    normalized_min, normalized_max = _validated_price_range(price_min, price_max)
    selected_sorts = _unique_sorts(sorts or (source.default_sort,))
    if product_limit is not None and not 1 <= product_limit <= MAX_LISTING_PRODUCTS:
        raise CompetitorListingInputError(
            f"加入数量必须在 1 到 {MAX_LISTING_PRODUCTS} 之间"
        )

    products_by_sort: list[list[CompetitorListingProduct]] = []
    total_candidates = 0
    source_total: int | None = None
    available_sorts: tuple[tuple[str, str], ...] = LISTING_SORT_OPTIONS
    requires_limit = False

    for sort_index, sort in enumerate(selected_sorts):
        if sort_index and page_delay_seconds:
            await asyncio.sleep(page_delay_seconds)
        filtered_url = build_competitor_listing_url(
            source,
            sort=sort,
            price_min=normalized_min,
            price_max=normalized_max,
        )
        request_url, payload = await client.fetch_listing_first_page(filtered_url)
        first_page = parse_competitor_listing_page(payload)
        if sort_index == 0:
            if first_page.sort_options:
                available_sorts = first_page.sort_options
            allowed_by_page = {value for value, _ in available_sorts}
            unsupported = [item for item in selected_sorts if item not in allowed_by_page]
            if unsupported:
                raise CompetitorListingInputError(
                    f"Takealot 当前不支持所选排序：{unsupported[0]}"
                )
        if first_page.total is not None:
            source_total = max(source_total or 0, first_page.total)
        page_requires_limit = (
            first_page.total > 20
            if first_page.total is not None
            else bool(first_page.next_after or len(first_page.products) > 20)
        )
        requires_limit = requires_limit or page_requires_limit
        scan_limit = (
            min(first_page.total, MAX_LISTING_PRODUCTS)
            if first_page.total is not None
            else MAX_LISTING_PRODUCTS
            if first_page.next_after
            else len(first_page.products)
        )
        products = list(first_page.products[:scan_limit])
        next_after = first_page.next_after
        while len(products) < scan_limit and next_after:
            if page_delay_seconds:
                await asyncio.sleep(page_delay_seconds)
            next_payload = await client.fetch_search_next_page(request_url, next_after)
            next_page = parse_competitor_listing_page(next_payload)
            products.extend(next_page.products[: scan_limit - len(products)])
            next_after = next_page.next_after
        products_by_sort.append(products)
        total_candidates += len(products)

    candidate_products = _balanced_rank_fusion(
        products_by_sort,
        sorts=selected_sorts,
        limit=MAX_LISTING_PRODUCTS,
    )
    initial_limit = (
        product_limit
        if requires_limit and product_limit is not None
        else 20
        if requires_limit
        else len(candidate_products)
    )
    selected_products = candidate_products[:initial_limit]
    can_commit = bool(candidate_products) and (
        not requires_limit or product_limit is not None
    )
    all_unique_plids = {
        product.plid for products in products_by_sort for product in products
    }
    return {
        "source_type": source.source_type,
        "source_url": source.source_url,
        "source_label": source.source_label,
        "price_min": normalized_min,
        "price_max": normalized_max,
        "sorts": list(selected_sorts),
        "sort_options": [
            {"value": value, "label": label} for value, label in available_sorts
        ],
        "source_total": source_total,
        "requires_limit": requires_limit,
        "product_limit": product_limit,
        "can_commit": can_commit,
        "scanned_candidate_count": total_candidates,
        "deduplicated_candidate_count": len(all_unique_plids),
        "candidate_capacity": len(candidate_products),
        "candidate_queue_frozen": bool(candidate_products),
        "duplicate_count": max(0, total_candidates - len(all_unique_plids)),
        "selected_count": len(selected_products),
        "selection_rule": BALANCED_LISTING_SELECTION_RULE,
        "products": [selection.as_dict() for selection in selected_products],
        "candidate_products": [
            selection.as_dict() for selection in candidate_products
        ],
    }


def finalize_competitor_listing_preview(
    payload: Mapping[str, object],
    *,
    product_limit: int | None,
) -> dict[str, object]:
    """Resize one frozen candidate queue without making another provider request."""

    result = copy.deepcopy(dict(payload))
    raw_candidates = result.get("candidate_products")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise CompetitorListingInputError(
            "筛选候选队列已失效，请重新扫描后再确认"
        )
    requires_limit = bool(result.get("requires_limit"))
    if requires_limit:
        if product_limit is None:
            raise CompetitorListingInputError(
                "筛选结果超过 20 个，请填写最终加入数量"
            )
        if not 1 <= product_limit <= MAX_LISTING_PRODUCTS:
            raise CompetitorListingInputError(
                f"加入数量必须在 1 到 {MAX_LISTING_PRODUCTS} 之间"
            )
        if product_limit > len(raw_candidates):
            raise CompetitorListingInputError(
                f"加入数量不能超过本次冻结的 {len(raw_candidates)} 个候选"
            )
        selected_limit = product_limit
    else:
        selected_limit = len(raw_candidates)
    selected_products = raw_candidates[:selected_limit]
    if not selected_products:
        raise CompetitorListingInputError("筛选预览没有可加入的商品")
    result["product_limit"] = product_limit
    result["products"] = selected_products
    result["selected_count"] = len(selected_products)
    result["can_commit"] = True
    return result


def parse_competitor_listing_page(payload: Mapping[str, Any]) -> CompetitorListingPage:
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, Mapping):
        raise CompetitorListingProviderError("Takealot 列表响应缺少商品区")
    sections: Mapping[str, Any] = raw_sections
    products_section = sections.get("products")
    if not isinstance(products_section, Mapping):
        raise CompetitorListingProviderError("Takealot 列表响应缺少商品区")
    raw_results = products_section.get("results")
    if not isinstance(raw_results, list):
        raise CompetitorListingProviderError("Takealot 列表响应缺少商品列表")

    products: list[CompetitorListingProduct] = []
    seen: set[str] = set()
    for raw in raw_results:
        if (
            not isinstance(raw, Mapping)
            or raw.get("type") != _ORGANIC_RESULT_TYPE
            or _is_sponsored_result(raw)
        ):
            continue
        view = raw.get("product_views")
        core = view.get("core") if isinstance(view, Mapping) else None
        if not isinstance(core, Mapping):
            continue
        plid = str(core.get("id") or "").strip()
        if not plid.isdigit() or plid in seen:
            continue
        title = " ".join(str(core.get("title") or "").split()) or f"PLID{plid}"
        slug = str(core.get("slug") or "").strip("/")
        url = (
            f"https://www.takealot.com/{slug}/PLID{plid}"
            if slug
            else f"https://www.takealot.com/p/PLID{plid}"
        )
        seen.add(plid)
        products.append(CompetitorListingProduct(plid=plid, title=title, url=url))

    paging = products_section.get("paging")
    paging = paging if isinstance(paging, Mapping) else {}
    total = _optional_nonnegative_int(paging.get("total_num_found"))
    next_after = str(paging.get("next_is_after") or "").strip() or None
    return CompetitorListingPage(
        products=tuple(products),
        total=total,
        next_after=next_after,
        sort_options=_parse_sort_options(sections),
    )


def _parse_sort_options(sections: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    section = sections.get("sort_options")
    results = section.get("results") if isinstance(section, Mapping) else None
    if not isinstance(results, list):
        return ()
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    fallback_labels = dict(LISTING_SORT_OPTIONS)
    for raw in results:
        option = raw.get("sort_option") if isinstance(raw, Mapping) else None
        if not isinstance(option, Mapping):
            continue
        value = str(option.get("param_value") or "").strip()
        if value not in _LISTING_SORT_VALUES or value in seen:
            continue
        display = str(option.get("display_value") or "").strip()
        options.append((value, fallback_labels.get(value, display or value)))
        seen.add(value)
    return tuple(options)


def _balanced_rank_fusion(
    products_by_sort: Sequence[Sequence[CompetitorListingProduct]],
    *,
    sorts: Sequence[str],
    limit: int | None,
) -> list[CompetitorListingSelection]:
    """Fuse selected Takealot ranks while preferring balanced recency and rating."""

    if len(products_by_sort) != len(sorts):
        raise ValueError("products and sorts must have the same length")
    representatives: dict[str, CompetitorListingProduct] = {}
    ranks_by_plid: dict[str, dict[int, int]] = {}
    first_seen: dict[str, int] = {}
    seen_index = 0
    for sort_index, products in enumerate(products_by_sort):
        for rank, product in enumerate(products, start=1):
            representatives.setdefault(product.plid, product)
            ranks_by_plid.setdefault(product.plid, {}).setdefault(sort_index, rank)
            if product.plid not in first_seen:
                first_seen[product.plid] = seen_index
                seen_index += 1

    rating_index = next(
        (index for index, value in enumerate(sorts) if value == "Rating Descending"),
        None,
    )
    release_index = next(
        (
            index
            for index, value in enumerate(sorts)
            if value == "ReleaseDate Descending"
        ),
        None,
    )
    core_indexes: tuple[int, ...]
    if rating_index is not None and release_index is not None:
        core_indexes = (rating_index, release_index)
    else:
        core_indexes = tuple(range(len(sorts)))
    all_indexes = tuple(range(len(sorts)))
    missing_rank = max((len(products) for products in products_by_sort), default=0) + 1

    def rank_metrics(
        plid: str,
        indexes: Sequence[int],
    ) -> tuple[int, int, int]:
        present = [
            ranks_by_plid[plid][index]
            for index in indexes
            if index in ranks_by_plid[plid]
        ]
        return (
            -len(present),
            max(present, default=missing_rank),
            sum(present) if present else missing_rank,
        )

    def ranking_key(plid: str) -> tuple[int, int, int, int, int, int, float, int, str]:
        core_coverage, core_worst, core_sum = rank_metrics(plid, core_indexes)
        all_coverage, all_worst, all_sum = rank_metrics(plid, all_indexes)
        reciprocal_rank_score = sum(
            1 / (60 + rank)
            for rank in ranks_by_plid[plid].values()
        )
        return (
            core_coverage,
            core_worst,
            core_sum,
            all_coverage,
            all_worst,
            all_sum,
            -reciprocal_rank_score,
            first_seen[plid],
            plid,
        )

    ordered_plids = sorted(representatives, key=ranking_key)
    selected_plids = ordered_plids if limit is None else ordered_plids[:limit]
    return [
        CompetitorListingSelection(
            product=representatives[plid],
            sort_ranks={
                sorts[index]: rank
                for index, rank in sorted(ranks_by_plid[plid].items())
            },
        )
        for plid in selected_plids
    ]


def _unique_sorts(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        normalized = _validated_sort(value)
        if normalized not in output:
            output.append(normalized)
    if not output:
        raise CompetitorListingInputError("请至少选择一种排序")
    if _PRICE_SORT_VALUES.issubset(output):
        raise CompetitorListingInputError(
            "价格从高到低和价格从低到高不能同时选择"
        )
    return tuple(output)


def _validated_sort(value: str) -> str:
    normalized = " ".join(str(value).split())
    if normalized not in _LISTING_SORT_VALUES:
        raise CompetitorListingInputError("排序方式无效")
    return normalized


def _validated_price_range(
    price_min: int | None,
    price_max: int | None,
) -> tuple[int | None, int | None]:
    for value in (price_min, price_max):
        if value is not None and not 0 <= value <= 10_000_000:
            raise CompetitorListingInputError("价格必须在 R0 到 R10,000,000 之间")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise CompetitorListingInputError("最低价格不能高于最高价格")
    return price_min, price_max


def _single_query_value(query: Mapping[str, list[str]], key: str) -> str | None:
    values = [value.strip() for value in query.get(key, []) if value.strip()]
    if len(values) != 1:
        return None
    return values[0]


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _is_sponsored_result(raw: Mapping[str, Any]) -> bool:
    containers: list[Mapping[str, Any]] = [raw]
    view = raw.get("product_views")
    if isinstance(view, Mapping):
        containers.append(view)
        core = view.get("core")
        if isinstance(core, Mapping):
            containers.append(core)
    for container in containers:
        for key in ("is_sponsored", "sponsored", "is_ad", "is_promoted"):
            value = container.get(key)
            if value is True or str(value).strip().casefold() in {"1", "true", "yes"}:
                return True
        for key in ("listing_type", "placement_type", "result_type"):
            kind = str(container.get(key) or "").strip().casefold()
            if kind in {"ad", "advertisement", "promoted", "sponsored"}:
                return True
    return False
