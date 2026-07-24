from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from takealot_ops.competitors.api import CompetitorPublicClient, extract_plid
from takealot_ops.competitors.domain import (
    CompetitorReviewRecord,
    PreviousObservation,
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.service import parse_competitor_urls
from takealot_ops.competitors.stock import _parse_warehouse_stock_message


async def _fake_delay(self: object, a: float, b: float) -> None:
    pass


def test_parse_competitor_urls_deduplicates_by_plid() -> None:
    urls = parse_competitor_urls(
        "\n".join(
            [
                "https://www.takealot.com/a/PLID72189176",
                "https://www.takealot.com/a/PLID72189176?size=Right",
                "https://www.takealot.com/b/PLID95526981",
            ]
        )
    )

    assert urls == [
        "https://www.takealot.com/a/PLID72189176",
        "https://www.takealot.com/b/PLID95526981",
    ]
    assert extract_plid(urls[0]) == "72189176"


def test_review_summary_and_lifetime_estimate_use_fixed_rules() -> None:
    reviews = [
        _review("one", 5),
        _review("two", 4),
        _review("three", 3),
        _review("four", 2),
        _review("five", 1),
    ]

    summary = summarize_reviews(reviews)
    assert (summary.total, summary.positive, summary.neutral, summary.negative) == (
        5,
        2,
        1,
        2,
    )
    assert estimate_lifetime_sales(12) == (240, 600)
    assert estimate_lifetime_sales(0) == (0, 0)


def test_sales_signal_requires_comparable_snapshots_and_labels_single_signal() -> None:
    baseline = analyze_sales_signal(
        None,
        current_stock_quantity=5,
        current_stock_exact=True,
        current_review_count=10,
    )
    previous = PreviousObservation(
        snapshot_id=7,
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        stock_quantity=5,
        stock_exact=True,
        review_count=10,
    )
    stock_only = analyze_sales_signal(
        previous,
        current_stock_quantity=3,
        current_stock_exact=True,
        current_review_count=10,
    )
    combined = analyze_sales_signal(
        previous,
        current_stock_quantity=3,
        current_stock_exact=True,
        current_review_count=11,
    )

    assert baseline.trend_label == "待建立基线"
    assert stock_only.observed_stock_outflow == 2
    assert stock_only.period_sales_min == 2
    assert stock_only.period_sales_max == 2
    assert stock_only.trend_label == "库存净流出（待验证）"
    assert combined.trend_label == "两个独立正向信号"
    assert combined.period_sales_min == 20
    assert combined.period_sales_max == 50


async def test_public_client_parses_product_offers_and_all_review_pages() -> None:
    canned: dict[str, dict[str, object]] = {
        "https://api.takealot.com/rest/v-1-10-0/product-details/PLID123": {
            "desktop_href": "https://www.takealot.com/example/PLID123",
            "core": {"title": "Example", "reviews": 2, "star_rating": 4.5},
            "buybox": {
                "tsin": "TSIN-1",
                "items": [
                    {
                        "is_selected": True,
                        "sku": "SKU-1",
                        "price": 199.0,
                        "stock_availability": {
                            "status": "Ships in 10 - 14 work days",
                            "is_leadtime": True,
                        },
                    }
                ],
            },
            "seller_detail": {
                "seller_id": "seller-1",
                "display_name": "Seller One",
            },
            "reviews": {"count": 2, "star_rating": 4.5},
            "gallery": {"images": ["https://img/{size}.jpg"]},
            "other_offers": {
                "conditions": [
                    {
                        "items": [
                            {
                                "sku": "SKU-2",
                                "price": 205,
                                "seller": {
                                    "seller_id": "seller-2",
                                    "display_name": "Seller Two",
                                },
                                "stock_availability": {"status": "In stock"},
                            }
                        ]
                    }
                ]
            },
        },
        "https://api.takealot.com/rest/v-1-10-0/product-reviews/plid/123?page=0": {
            "page_info": {"total_pages": 2},
            "reviews": [
                {
                    "uuid": "review-0",
                    "rating": 5,
                    "customer_name": "Customer",
                    "date": "2026-07-20",
                    "text": {"title": "Title", "body": "Body"},
                }
            ],
        },
        "https://api.takealot.com/rest/v-1-10-0/product-reviews/plid/123?page=1": {
            "page_info": {"total_pages": 2},
            "reviews": [
                {
                    "uuid": "review-1",
                    "rating": 2,
                    "customer_name": "Customer",
                    "date": "2026-07-21",
                    "text": {"title": "Title", "body": "Body"},
                }
            ],
        },
    }

    async def fake_get_json(self, url: str, **kw: object) -> dict[str, object]:
        return canned[url]  # type: ignore[return-value]

    with (
        patch.object(CompetitorPublicClient, "__init__", lambda self, **kw: None),
        patch.object(CompetitorPublicClient, "_get_json", fake_get_json),
        patch.object(CompetitorPublicClient, "close", lambda self: None),
        patch.object(CompetitorPublicClient, "_human_delay", _fake_delay),
    ):
        client = CompetitorPublicClient()
        client._page = MagicMock()
        product = await client.fetch_product("https://www.takealot.com/example/PLID123")
        reviews = await client.fetch_all_reviews("123", page_delay_seconds=0)

    assert product.plid == "123"
    assert product.seller_name == "Seller One"
    assert product.is_leadtime is True
    assert product.stock_status == "没货（非平台仓/供应商调货）"
    assert len(product.offers) == 2
    assert len(product.variants) == 1
    assert product.variants[0].label == "默认款"
    assert product.image_url == "https://img/zoom.jpg"
    assert [review.rating for review in reviews] == [2, 5]


async def test_public_client_enumerates_variants_under_one_plid() -> None:
    def variant_detail(size: str, *, available: bool) -> dict[str, object]:
        return {
            "title": f"Brace - {size}",
            "desktop_href": f"https://www.takealot.com/brace/PLID96909926?size={size}",
            "buybox": {
                "tsin": f"TSIN-{size}",
                "items": [
                    {
                        "is_selected": True,
                        "is_add_to_cart_available": available,
                        "sku": f"SKU-{size}",
                        "price": 100,
                        "stock_availability": {"status": "In stock"},
                    }
                ],
            },
            "variants": {
                "selectors": [
                    {
                        "title": "Size",
                        "options": [
                            {
                                "value": value,
                                "is_selected": value == size,
                                "href": (
                                    "https://api.takealot.com/rest/v-1-13-0/"
                                    f"product-details/PLID96909926?size={value}"
                                ),
                            }
                            for value in ("Right", "Left")
                        ],
                    }
                ]
            },
        }

    canned: dict[str, dict[str, object]] = {
        "https://api.takealot.com/rest/v-1-10-0/product-details/PLID96909926": {
            "title": "Brace",
            "desktop_href": "https://www.takealot.com/brace/PLID96909926",
            "core": {"title": "Brace"},
            "reviews": {"count": 8, "star_rating": 4.2},
            "variants": {
                "selectors": [
                    {
                        "title": "Size",
                        "options": [
                            {
                                "value": value,
                                "is_selected": False,
                                "href": (
                                    "https://api.takealot.com/rest/v-1-13-0/"
                                    f"product-details/PLID96909926?size={value}"
                                ),
                            }
                            for value in ("Right", "Left")
                        ],
                    }
                ]
            },
        },
        "https://api.takealot.com/rest/v-1-13-0/product-details/PLID96909926?size=Right": variant_detail("Right", available=True),
        "https://api.takealot.com/rest/v-1-13-0/product-details/PLID96909926?size=Left": variant_detail("Left", available=True),
    }

    async def fake_get_json(self, url: str, **kw: object) -> dict[str, object]:
        return canned[url]  # type: ignore[return-value]

    with (
        patch.object(CompetitorPublicClient, "__init__", lambda self, **kw: None),
        patch.object(CompetitorPublicClient, "_get_json", fake_get_json),
        patch.object(CompetitorPublicClient, "close", lambda self: None),
        patch.object(CompetitorPublicClient, "_human_delay", _fake_delay),
    ):
        client = CompetitorPublicClient()
        client._page = MagicMock()
        product = await client.fetch_product(
            "https://www.takealot.com/brace/PLID96909926?size=Left"
        )

    assert product.plid == "96909926"
    assert product.title == "Brace"
    assert [variant.label for variant in product.variants] == [
        "Size：Right",
        "Size：Left",
    ]
    assert [variant.sku for variant in product.variants] == [
        "SKU-Right",
        "SKU-Left",
    ]
    assert product.sku == "SKU-Left"


def test_parse_explicit_warehouse_stock_warning() -> None:
    warning = (
        "You've attempted to order more stock than currently available at our "
        "warehouse (current stock = 50). The products will need to be ordered "
        "from our supplier."
    )

    assert _parse_warehouse_stock_message(warning) == 50
    assert _parse_warehouse_stock_message("current stock = 1,250") == 1250
    assert _parse_warehouse_stock_message("In stock") is None


def _review(review_id: str, rating: int) -> CompetitorReviewRecord:
    return CompetitorReviewRecord(
        review_id=review_id,
        rating=rating,
        title="",
        body="",
        customer_name="",
        review_date="",
    )
