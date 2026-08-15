from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from takealot_ops.competitors.listings import (
    CompetitorListingInputError,
    CompetitorListingPreviewExpiredError,
    CompetitorListingPreviewRegistry,
    build_competitor_listing_url,
    finalize_competitor_listing_preview,
    parse_competitor_listing_page,
    parse_competitor_listing_source,
    preview_competitor_listing,
)


SORTS = (
    "Relevance",
    "Price Descending",
    "Price Ascending",
    "Rating Descending",
    "ReleaseDate Descending",
)


def _raw_product(plid: int, *, sponsored: bool = False) -> dict[str, object]:
    return {
        "type": "product_views",
        "is_sponsored": sponsored,
        "product_views": {
            "core": {
                "id": str(plid),
                "title": f"Product {plid}",
                "slug": f"product-{plid}",
            }
        },
    }


def _payload(
    plids: list[int],
    *,
    total: int,
    next_after: str | None = None,
) -> dict[str, object]:
    return {
        "sections": {
            "products": {
                "results": [_raw_product(plid) for plid in plids],
                "paging": {
                    "total_num_found": total,
                    "next_is_after": next_after,
                },
            },
            "sort_options": {
                "results": [
                    {
                        "type": "sort_option",
                        "sort_option": {
                            "param_value": value,
                            "display_value": value,
                        },
                    }
                    for value in SORTS
                ]
            },
        }
    }


class _FakeListingClient:
    def __init__(
        self,
        first_pages: Mapping[str, dict[str, object]],
        *,
        next_pages: Mapping[str, dict[str, object]] | None = None,
    ) -> None:
        self.first_pages = dict(first_pages)
        self.next_pages = dict(next_pages or {})
        self.first_urls: list[str] = []
        self.next_calls: list[tuple[str, str]] = []

    async def fetch_listing_first_page(
        self,
        source_url: str,
    ) -> tuple[str, dict[str, Any]]:
        self.first_urls.append(source_url)
        sort = parse_qs(urlsplit(source_url).query)["sort"][0]
        return f"https://api.takealot.com/searches/products,filters?sort={sort}", dict(
            self.first_pages[sort]
        )

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        self.next_calls.append((request_url, after))
        return dict(self.next_pages[after])


def test_listing_source_classification_and_canonicalization() -> None:
    seller = parse_competitor_listing_source(
        "https://www.takealot.com/seller/techitstore?sellers=29853614"
    )
    category = parse_competitor_listing_source(
        "https://www.takealot.com/all?custom=new-to-tal-appliances"
        "&sort=ReleaseDate%20Descending"
    )

    assert seller.source_type == "seller"
    assert seller.source_url == (
        "https://www.takealot.com/seller/techitstore?sellers=29853614"
    )
    assert category.source_type == "category"
    assert category.default_sort == "ReleaseDate Descending"
    assert category.source_url == (
        "https://www.takealot.com/all?custom=new-to-tal-appliances"
    )

    with pytest.raises(CompetitorListingInputError, match="当前入口只接受"):
        parse_competitor_listing_source(
            seller.source_url,
            expected_type="category",
        )
    with pytest.raises(CompetitorListingInputError, match="类目链接缺少"):
        parse_competitor_listing_source("https://www.takealot.com/all?qsearch=mouse")
    with pytest.raises(CompetitorListingInputError, match="Takealot"):
        parse_competitor_listing_source(
            "https://example.com/seller/shop?sellers=29853614"
        )


@pytest.mark.parametrize(
    ("value", "canonical_url", "source_label", "default_sort"),
    [
        (
            "https://www.takealot.com/camping-outdoor/family-tents-27895/"
            "?sort=ReleaseDate%20Descending#products",
            "https://www.takealot.com/camping-outdoor/family-tents-27895",
            "family tents",
            "ReleaseDate Descending",
        ),
        (
            "https://www.takealot.com/camping-outdoor/tents-25681",
            "https://www.takealot.com/camping-outdoor/tents-25681",
            "tents",
            "Relevance",
        ),
        (
            "https://www.takealot.com/camping-outdoor/tents-and-shelter-25675",
            "https://www.takealot.com/camping-outdoor/tents-and-shelter-25675",
            "tents and shelter",
            "Relevance",
        ),
    ],
)
def test_listing_source_accepts_takealot_seo_category_paths(
    value: str,
    canonical_url: str,
    source_label: str,
    default_sort: str,
) -> None:
    source = parse_competitor_listing_source(value, expected_type="category")

    assert source.source_type == "category"
    assert source.source_url == canonical_url
    assert source.source_label == source_label
    assert source.default_sort == default_sort


def test_listing_source_rejects_arbitrary_takealot_paths_as_categories() -> None:
    with pytest.raises(CompetitorListingInputError, match="数字类目 ID"):
        parse_competitor_listing_source(
            "https://www.takealot.com/camping-outdoor/family-tents",
            expected_type="category",
        )


def test_listing_url_builds_takealot_price_filter_and_one_sort() -> None:
    source = parse_competitor_listing_source(
        "https://www.takealot.com/seller/techitstore?sellers=29853614"
    )
    result = build_competitor_listing_url(
        source,
        sort="Price Ascending",
        price_min=100,
        price_max=1000,
    )

    assert parse_qs(urlsplit(result).query) == {
        "sellers": ["29853614"],
        "sort": ["Price Ascending"],
        "filter": ["Price:100-1000"],
    }


def test_listing_url_preserves_seo_category_path_when_adding_filters() -> None:
    source = parse_competitor_listing_source(
        "https://www.takealot.com/camping-outdoor/family-tents-27895"
    )
    result = build_competitor_listing_url(
        source,
        sort="Rating Descending",
        price_min=200,
        price_max=None,
    )

    parsed = urlsplit(result)
    assert parsed.path == "/camping-outdoor/family-tents-27895"
    assert parse_qs(parsed.query) == {
        "sort": ["Rating Descending"],
        "filter": ["Price:200-*"],
    }


def test_listing_page_keeps_only_unique_unsponsored_products() -> None:
    payload = _payload([101, 102], total=3)
    products = payload["sections"]["products"]  # type: ignore[index]
    products["results"].extend(  # type: ignore[index, union-attr]
        [_raw_product(102), _raw_product(103, sponsored=True)]
    )

    page = parse_competitor_listing_page(payload)

    assert [item.plid for item in page.products] == ["101", "102"]
    assert page.total == 3
    assert page.products[0].url.endswith("/product-101/PLID101")
    assert [value for value, _ in page.sort_options] == list(SORTS)


def test_preview_collects_every_product_when_filtered_total_is_at_most_twenty() -> None:
    client = _FakeListingClient({"Relevance": _payload([1, 2, 3, 4], total=4)})

    result = asyncio.run(
        preview_competitor_listing(
            client,
            source_url="https://www.takealot.com/seller/shop?sellers=123",
            source_type="seller",
            price_min=None,
            price_max=None,
            sorts=["Relevance"],
            product_limit=2,
            page_delay_seconds=0,
        )
    )

    assert result["requires_limit"] is False
    assert result["can_commit"] is True
    assert result["selected_count"] == 4
    assert [item["plid"] for item in result["products"]] == ["1", "2", "3", "4"]


def test_preview_requires_quantity_above_twenty() -> None:
    client = _FakeListingClient(
        {"ReleaseDate Descending": _payload(list(range(1, 37)), total=233)}
    )

    result = asyncio.run(
        preview_competitor_listing(
            client,
            source_url=(
                "https://www.takealot.com/all?custom=new-to-tal-appliances"
                "&sort=ReleaseDate%20Descending"
            ),
            source_type="category",
            price_min=100,
            price_max=1000,
            sorts=[],
            product_limit=None,
            page_delay_seconds=0,
        )
    )

    assert result["source_total"] == 233
    assert result["requires_limit"] is True
    assert result["can_commit"] is False
    assert result["selected_count"] == 20
    assert "filter=Price%3A100-1000" in client.first_urls[0]


def test_preview_rejects_both_price_directions_before_request() -> None:
    client = _FakeListingClient({})

    with pytest.raises(CompetitorListingInputError, match="不能同时选择"):
        asyncio.run(
            preview_competitor_listing(
                client,
                source_url="https://www.takealot.com/seller/shop?sellers=123",
                source_type="seller",
                price_min=None,
                price_max=None,
                sorts=["Price Descending", "Price Ascending"],
                product_limit=10,
                page_delay_seconds=0,
            )
        )

    assert client.first_urls == []


def test_preview_balances_rating_and_release_ranks_before_final_limit() -> None:
    client = _FakeListingClient(
        {
            "Rating Descending": _payload([1, 2, 3, 4, 5], total=100),
            "ReleaseDate Descending": _payload([5, 4, 3, 6, 7], total=100),
        }
    )

    result = asyncio.run(
        preview_competitor_listing(
            client,
            source_url="https://www.takealot.com/seller/shop?sellers=123",
            source_type="seller",
            price_min=None,
            price_max=None,
            sorts=[
                "Rating Descending",
                "ReleaseDate Descending",
                "Rating Descending",
            ],
            product_limit=5,
            page_delay_seconds=0,
        )
    )

    assert result["sorts"] == ["Rating Descending", "ReleaseDate Descending"]
    assert result["selected_count"] == 5
    assert [item["plid"] for item in result["products"]] == ["3", "4", "5", "1", "2"]
    assert result["products"][0]["sort_ranks"] == {
        "Rating Descending": 3,
        "ReleaseDate Descending": 3,
    }
    assert result["selection_rule"] == (
        "balanced_rank_fusion_then_plid_deduplicate"
    )
    assert result["scanned_candidate_count"] == 10
    assert result["deduplicated_candidate_count"] == 7
    assert result["duplicate_count"] == 3


def test_preview_follows_takealot_after_cursor_until_limit() -> None:
    client = _FakeListingClient(
        {"Relevance": _payload(list(range(1, 37)), total=50, next_after="cursor-1")},
        next_pages={"cursor-1": _payload(list(range(37, 51)), total=50)},
    )

    result = asyncio.run(
        preview_competitor_listing(
            client,
            source_url="https://www.takealot.com/seller/shop?sellers=123",
            source_type="seller",
            price_min=None,
            price_max=None,
            sorts=["Relevance"],
            product_limit=40,
            page_delay_seconds=0,
        )
    )

    assert result["selected_count"] == 40
    assert client.next_calls == [
        ("https://api.takealot.com/searches/products,filters?sort=Relevance", "cursor-1")
    ]


def test_frozen_preview_quantity_changes_reuse_ranked_candidates() -> None:
    client = _FakeListingClient(
        {"Relevance": _payload(list(range(1, 31)), total=30)}
    )
    preview = asyncio.run(
        preview_competitor_listing(
            client,
            source_url="https://www.takealot.com/seller/shop?sellers=123",
            source_type="seller",
            price_min=None,
            price_max=None,
            sorts=["Relevance"],
            product_limit=5,
            page_delay_seconds=0,
        )
    )

    resized = finalize_competitor_listing_preview(preview, product_limit=12)

    assert preview["selected_count"] == 5
    assert preview["candidate_capacity"] == 30
    assert resized["selected_count"] == 12
    assert [item["plid"] for item in resized["products"]] == [
        str(value) for value in range(1, 13)
    ]
    assert len(client.first_urls) == 1
    assert client.next_calls == []

    with pytest.raises(CompetitorListingInputError, match="最终加入数量"):
        finalize_competitor_listing_preview(preview, product_limit=None)
    with pytest.raises(CompetitorListingInputError, match="不能超过本次冻结的 30 个候选"):
        finalize_competitor_listing_preview(preview, product_limit=31)


def test_preview_registry_is_user_scoped_and_discardable() -> None:
    registry = CompetitorListingPreviewRegistry()
    token = registry.issue(user_id=7, payload={"products": [{"plid": "1"}]})

    assert registry.resolve(token=token, user_id=7)["products"] == [{"plid": "1"}]
    with pytest.raises(CompetitorListingPreviewExpiredError):
        registry.resolve(token=token, user_id=8)
    registry.discard(token)
    with pytest.raises(CompetitorListingPreviewExpiredError):
        registry.resolve(token=token, user_id=7)
