from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import (
    CompetitorNetworkError,
    CompetitorNotFoundError,
    CompetitorPageValidationError,
    CompetitorPublicClient,
    extract_plid,
)
from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
    PreviousObservation,
    StockProbeResult,
    VariantStockObservation,
    analyze_sales_signal,
    competitor_offer_identity,
    competitor_offer_stock_state,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.service import (
    CompetitorCollector,
    _discovered_offer_targets,
    _interval_price_signal,
    load_competitor_link_health,
    parse_competitor_urls,
)
from takealot_ops.competitors.stock import _parse_warehouse_stock_message
from takealot_ops.storage.models import (
    Base,
    CompetitorLinkHealth,
    CompetitorSnapshot,
    CompetitorTarget,
)


async def _fake_delay(self: object, a: float, b: float) -> None:
    pass


def _failing_browser_stack(
    error: BaseException,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    page = MagicMock()
    page.add_init_script = AsyncMock()
    page.goto = AsyncMock(side_effect=error)
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    playwright.stop = AsyncMock()
    manager = MagicMock()
    manager.start = AsyncMock(return_value=playwright)
    return manager, playwright, browser, context, page


async def test_public_client_cleans_up_when_proxy_fails_during_warmup() -> None:
    manager, playwright, browser, context, page = _failing_browser_stack(
        OSError("proxy unavailable")
    )
    client = CompetitorPublicClient()

    with (
        patch("takealot_ops.competitors.api.async_playwright", return_value=manager),
        patch(
            "takealot_ops.competitors.api._find_browser_executable",
            return_value=Path("chrome.exe"),
        ),
        patch.object(client, "_human_delay", AsyncMock()),
        pytest.raises(CompetitorNetworkError, match="梯子或代理"),
    ):
        await client.start()

    assert page.goto.await_count == 3
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert client._playwright is None
    assert client._browser is None
    assert client._context is None
    assert client._page is None


async def test_public_client_cleans_up_when_start_is_cancelled() -> None:
    manager, playwright, browser, context, _ = _failing_browser_stack(asyncio.CancelledError())
    client = CompetitorPublicClient()

    with (
        patch("takealot_ops.competitors.api.async_playwright", return_value=manager),
        patch(
            "takealot_ops.competitors.api._find_browser_executable",
            return_value=Path("chrome.exe"),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await client.start()

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_public_client_uses_conservative_warmup_delay() -> None:
    manager, playwright, browser, context, page = _failing_browser_stack(
        OSError("unused")
    )
    page.goto = AsyncMock()
    client = CompetitorPublicClient()

    with (
        patch("takealot_ops.competitors.api.async_playwright", return_value=manager),
        patch(
            "takealot_ops.competitors.api._find_browser_executable",
            return_value=Path("chrome.exe"),
        ),
        patch.object(client, "_human_delay", AsyncMock()) as delay,
    ):
        await client.start()
        await client.close()

    delay.assert_awaited_once_with(4.0, 7.0)
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_collector_marks_takealot_network_failure_as_retryable(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    client.fetch_product = AsyncMock(side_effect=CompetitorNetworkError("Takealot 暂时不可访问"))
    collector = CompetitorCollector(
        engine=MagicMock(),
        project_root=tmp_path,
        client=client,
    )

    result = await collector.collect(
        "https://www.takealot.com/example/PLID12345678",
        with_stock_probe=False,
    )

    assert result.succeeded is False
    assert result.retryable is True
    assert result.message == "Takealot 暂时不可访问"


async def test_collector_retries_after_persisting_failed_stock_probe(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    url = "https://www.takealot.com/example/PLID12345678"
    variant = CompetitorVariant(
        key="default",
        label="默认款",
        url=url,
        title="Example",
        sku="SKU-1",
        seller_id="seller-1",
        seller_name="Seller",
        price=100.0,
        stock_status="In stock",
        is_leadtime=False,
        is_add_to_cart_available=True,
        image_url=None,
    )
    product = CompetitorProduct(
        plid="12345678",
        url=url,
        title="Example",
        image_url=None,
        sku="SKU-1",
        seller_id="seller-1",
        seller_name="Seller",
        price=100.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=0,
        rating=0.0,
        offers=(
            CompetitorOffer(
                selected=True,
                sku="SKU-1",
                seller_id="seller-1",
                seller_name="Seller",
                price=100.0,
                stock_status="In stock",
                is_buybox=True,
            ),
        ),
        variants=(variant,),
    )
    failed_stock = StockProbeResult(
        quantity=None,
        exact=False,
        method="failed",
        note="购物车未完整加载",
    )
    client = MagicMock()
    client.fetch_product = AsyncMock(return_value=product)
    client.fetch_all_reviews = AsyncMock(return_value=[])
    stages: list[str] = []
    collector = CompetitorCollector(
        engine=engine,
        project_root=tmp_path,
        client=client,
        progress_callback=stages.append,
    )

    with patch(
        "takealot_ops.competitors.service.probe_product_stocks",
        AsyncMock(
            return_value=(
                [VariantStockObservation(variant=variant, stock=failed_stock)],
                [],
            )
        ),
    ):
        result = await collector.collect(url, with_stock_probe=True)

    assert result.succeeded is False
    assert result.retryable is True
    assert result.failure_kind == "stock-unprobed"
    assert "失败原因：默认款（SKU SKU-1）：购物车未完整加载" in result.message
    assert "已加入本轮其他链接结束后的库存复探" in result.message
    assert stages == [
        "正在读取商品与变体",
        "正在读取全部评论",
        "正在启动库存探测浏览器",
        "正在保存商品快照",
    ]
    with Session(engine) as session:
        snapshot = session.scalar(select(CompetitorSnapshot))
        assert snapshot is not None
        assert snapshot.plid == "12345678"
        assert snapshot.stock_quantity is None
    engine.dispose()


async def test_own_store_collection_skips_buybox_stock_and_reviews_without_followers(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    url = "https://www.takealot.com/p/PLID12345678"
    buybox = CompetitorOffer(
        selected=True,
        sku="PUBLIC-SKU",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=99.0,
        stock_status="In stock",
        is_buybox=True,
        is_add_to_cart_available=True,
        plid="12345678",
        url=url,
    )
    product = CompetitorProduct(
        plid="12345678",
        url=url,
        title="Own Product",
        image_url=None,
        sku="PUBLIC-SKU",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=99.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=12,
        rating=4.5,
        offers=(buybox,),
        variants=(
            CompetitorVariant(
                key="default",
                label="默认款",
                url=url,
                title="Own Product",
                sku="PUBLIC-SKU",
                seller_id="seller-main",
                seller_name="Main Seller",
                price=99.0,
                stock_status="In stock",
                is_leadtime=False,
                is_add_to_cart_available=True,
            ),
        ),
    )
    client = MagicMock()
    client.fetch_product = AsyncMock(return_value=product)
    client.fetch_all_reviews = AsyncMock(return_value=[])
    collector = CompetitorCollector(engine=engine, project_root=tmp_path, client=client)

    result = await collector.collect(
        url,
        with_stock_probe=True,
        followers_only=True,
    )

    assert result.succeeded is True
    assert "未发现跟卖报价" in result.message
    client.fetch_all_reviews.assert_not_awaited()
    with Session(engine) as session:
        snapshot = session.scalar(select(CompetitorSnapshot))
        assert snapshot is not None
        assert snapshot.offers == []
        assert snapshot.review_count == 0
        assert session.scalar(select(CompetitorTarget)) is None
    engine.dispose()


async def test_public_client_keeps_api_404_separate_from_network_failure() -> None:
    response = MagicMock()
    response.status = 404
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    client = CompetitorPublicClient()
    client._page = page

    with pytest.raises(CompetitorNotFoundError, match="商品数据返回 404"):
        await client._get_json(
            "https://api.takealot.com/rest/v-1-10-0/product-details/PLID123",
        )

    page.goto.assert_awaited_once()


async def test_public_client_retries_api_403_and_recovers() -> None:
    blocked = MagicMock()
    blocked.status = 403
    recovered = MagicMock()
    recovered.status = 200
    recovered.json = AsyncMock(return_value={"core": {"title": "Recovered"}})
    page = MagicMock()
    page.goto = AsyncMock(side_effect=[blocked, recovered])
    client = CompetitorPublicClient()
    client._page = page

    with patch.object(client, "_human_delay", AsyncMock()) as delay:
        payload = await client._get_json(
            "https://api.takealot.com/rest/v-1-10-0/product-details/PLID123",
        )

    assert payload == {"core": {"title": "Recovered"}}
    assert page.goto.await_count == 2
    delay.assert_awaited_once()


async def test_public_client_marks_persistent_api_403_as_network_failure() -> None:
    blocked = MagicMock()
    blocked.status = 403
    page = MagicMock()
    page.goto = AsyncMock(return_value=blocked)
    client = CompetitorPublicClient()
    client._page = page

    with (
        patch.object(client, "_human_delay", AsyncMock()),
        pytest.raises(CompetitorNetworkError, match="403"),
    ):
        await client._get_json(
            "https://api.takealot.com/rest/v-1-10-0/product-details/PLID123",
            retries=1,
        )

    assert page.goto.await_count == 2


async def test_public_client_marks_product_page_403_as_network_failure() -> None:
    blocked = MagicMock()
    blocked.status = 403
    page = MagicMock()
    page.goto = AsyncMock(return_value=blocked)
    client = CompetitorPublicClient()
    client._page = page

    with pytest.raises(CompetitorNetworkError, match="网络问题"):
        await client._product_page_state(  # type: ignore[attr-defined]
            "https://www.takealot.com/example/PLID123"
        )

    page.goto.assert_awaited_once()


async def test_public_client_cross_checks_target_against_known_good_page() -> None:
    client = CompetitorPublicClient()
    client._product_page_state = AsyncMock(  # type: ignore[method-assign]
        side_effect=["not-found", "product"]
    )

    await client.confirm_product_page_absent(
        "https://www.takealot.com/missing/PLID111",
        "https://www.takealot.com/control/PLID222",
    )

    client._product_page_state = AsyncMock(  # type: ignore[method-assign]
        side_effect=["product", "product"]
    )
    with pytest.raises(CompetitorPageValidationError, match="接口暂时返回 404"):
        await client.confirm_product_page_absent(
            "https://www.takealot.com/visible/PLID111",
            "https://www.takealot.com/control/PLID222",
        )


@pytest.mark.parametrize(
    ("status", "title", "headings", "expected"),
    [
        (200, "404, Page Not found", ["404, Page Not found"], "not-found"),
        (
            200,
            "Product | Shop Today. Get it Tomorrow! | takealot.com",
            ["", "Actual product title"],
            "product",
        ),
        (404, "404, Page Not found", ["404, Page Not found"], "not-found"),
        (200, "Takealot.com", [], "uncertain"),
    ],
)
async def test_public_client_classifies_rendered_product_page(
    status: int,
    title: str,
    headings: list[str],
    expected: str,
) -> None:
    response = MagicMock()
    response.status = status
    locator = MagicMock()
    locator.all_text_contents = AsyncMock(return_value=headings)
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.title = AsyncMock(return_value=title)
    page.locator.return_value = locator
    page.wait_for_timeout = AsyncMock()
    client = CompetitorPublicClient()
    client._page = page

    assert (
        await client._product_page_state(  # type: ignore[attr-defined]
            "https://www.takealot.com/example/PLID123"
        )
        == expected
    )


async def test_collector_keeps_uncertain_page_validation_out_of_network_failures(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    client.fetch_product = AsyncMock(side_effect=CompetitorNotFoundError())
    client.confirm_product_page_absent = AsyncMock(
        side_effect=CompetitorPageValidationError("页面复核结果不确定")
    )
    collector = CompetitorCollector(
        engine=MagicMock(),
        project_root=tmp_path,
        client=client,
    )
    collector._latest_control_product = MagicMock(  # type: ignore[method-assign]
        return_value=("222", "https://www.takealot.com/control/PLID222")
    )
    collector._is_confirmed_invalid = MagicMock(return_value=False)  # type: ignore[method-assign]

    result = await collector.collect(
        "https://www.takealot.com/example/PLID12345678",
        with_stock_probe=False,
    )

    assert result.succeeded is False
    assert result.retryable is True
    assert result.failure_kind == "validation-uncertain"
    assert result.message == "页面复核结果不确定"


async def test_previously_confirmed_link_uses_one_future_404_as_terminal(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    checked_at = datetime(2026, 7, 30, tzinfo=UTC)
    url = "https://www.takealot.com/missing/PLID111"
    with Session(engine) as session, session.begin():
        session.add(
            CompetitorLinkHealth(
                plid="111",
                url=url,
                status="confirmed_invalid",
                confirmed_not_found_count=3,
                first_not_found_at=checked_at - timedelta(days=1),
                last_evidence_at=checked_at - timedelta(days=1),
                last_checked_at=checked_at - timedelta(days=1),
                last_success_at=None,
                control_plid="222",
                control_check_ok=True,
                last_error="Takealot 商品数据返回 404",
            )
        )
    client = MagicMock()
    client.fetch_product = AsyncMock(side_effect=CompetitorNotFoundError())
    client.confirm_product_page_absent = AsyncMock(
        side_effect=AssertionError("历史确认失效链接不应再次进入三次交叉复核")
    )
    collector = CompetitorCollector(
        engine=engine,
        project_root=tmp_path,
        client=client,
    )

    result = await collector.collect(url, with_stock_probe=False)

    assert result.succeeded is False
    assert result.retryable is False
    assert result.failure_kind == "confirmed-invalid"
    assert "一次复核规则" in result.message
    client.confirm_product_page_absent.assert_not_awaited()
    with Session(engine) as session:
        row = session.get(CompetitorLinkHealth, "111")
        assert row is not None
        assert row.status == "confirmed_invalid"
        assert row.confirmed_not_found_count == 3
        assert row.control_plid == "222"
        assert row.control_check_ok is True
    engine.dispose()


def test_link_health_requires_three_spaced_control_verified_404s() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    start = datetime(2026, 7, 27, tzinfo=UTC)
    url = "https://www.takealot.com/missing/PLID111"

    decisions = []
    for checked_at in (
        start,
        start + timedelta(minutes=5),
        start + timedelta(minutes=10),
        start + timedelta(minutes=20),
    ):
        with Session(engine) as session, session.begin():
            decisions.append(
                CompetitorRepository(session).record_not_found(
                    plid="111",
                    url=url,
                    checked_at=checked_at,
                    control_plid="222",
                    control_check_ok=True,
                )
            )

    assert [decision.confirmed_not_found_count for decision in decisions] == [
        1,
        1,
        2,
        3,
    ]
    assert [decision.evidence_counted for decision in decisions] == [
        True,
        False,
        True,
        True,
    ]
    assert decisions[-1].status == "confirmed_invalid"
    assert load_competitor_link_health(engine)[0]["status"] == "confirmed_invalid"

    with Session(engine) as session, session.begin():
        CompetitorRepository(session).mark_link_healthy(
            plid="111",
            url=url,
            checked_at=start + timedelta(minutes=21),
        )
    with Session(engine) as session:
        row = session.get(CompetitorLinkHealth, "111")
        assert row is not None
        assert row.status == "healthy"
        assert row.confirmed_not_found_count == 0
    assert load_competitor_link_health(engine) == []
    engine.dispose()


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


@pytest.mark.parametrize(
    ("old_price", "new_price", "expected_change", "expected_label"),
    [
        (Decimal("200.00"), Decimal("180.00"), -20.0, "降价"),
        (Decimal("180.00"), Decimal("200.00"), 20.0, "涨价"),
        (Decimal("200.00"), Decimal("200.00"), 0.0, "价格不变"),
        (None, Decimal("200.00"), None, "价格不可比"),
    ],
)
def test_interval_price_signal_labels_selected_range_endpoints(
    old_price: Decimal | None,
    new_price: Decimal | None,
    expected_change: float | None,
    expected_label: str,
) -> None:
    oldest = SimpleNamespace(id=1, price=old_price)
    latest = SimpleNamespace(id=2, price=new_price)

    start, change, label = _interval_price_signal(oldest, latest)

    assert start == (float(old_price) if old_price is not None else None)
    assert change == expected_change
    assert label == expected_label


def test_competitor_offer_identity_prefers_offer_id_and_never_uses_plid() -> None:
    assert (
        competitor_offer_identity(
            offer_id="OFFER-123",
            seller_id="seller-1",
            sku="SKU-1",
        )
        == "offer:offer-123"
    )
    assert (
        competitor_offer_identity(
            seller_id="seller-1",
            sku="SKU-1",
            variant_key="colour=black",
            condition="New",
        )
        == "fallback:seller-1|sku-1|colour=black|new"
    )
    assert competitor_offer_identity(seller_id="seller-1") is None


def test_competitor_offer_stock_state_keeps_unknown_separate_from_zero() -> None:
    assert competitor_offer_stock_state("In stock") == "有货"
    assert competitor_offer_stock_state("Out of stock") == "没货"
    assert competitor_offer_stock_state("Ships in 10 days", is_leadtime=True) == "没货"
    assert competitor_offer_stock_state("Status pending") == "未知"


async def test_public_client_parses_product_offers_and_all_review_pages() -> None:
    canned: dict[str, dict[str, object]] = {
        "https://api.takealot.com/rest/v-1-10-0/product-details/PLID123": {
            "desktop_href": "https://www.takealot.com/example/PLID123",
            "core": {"title": "Example", "reviews": 2, "star_rating": 4.5},
            "buybox": {
                "tsin": "TSIN-1",
                "items": [
                    {
                        "offer_id": "offer-1",
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
                        "display_name": "New",
                        "items": [
                            {
                                "id": "other-buying-option-SKU-2",
                                "sku": "SKU-2",
                                "price": 205,
                                "product": {
                                    "desktop_href": "https://www.takealot.com/example/PLID123"
                                },
                                "seller": {
                                    "seller_id": "seller-2",
                                    "display_name": "Seller Two",
                                },
                                "stock_availability": {
                                    "status": "In stock",
                                    "is_in_stock": True,
                                },
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
    assert product.offers[0].plid == "123"
    assert product.offers[0].url == "https://www.takealot.com/example/PLID123"
    assert product.offers[0].offer_id == "offer-1"
    assert product.offers[0].is_buybox is True
    assert product.offers[0].is_leadtime is True
    assert product.offers[1].plid == "123"
    assert product.offers[1].url == "https://www.takealot.com/example/PLID123"
    assert product.offers[1].offer_id == "other-buying-option-SKU-2"
    assert product.offers[1].is_buybox is False
    assert product.offers[1].is_add_to_cart_available is True
    assert product.offers[1].condition == "New"
    assert product.offers[0].identity_key == "offer:offer-1"
    assert product.offers[1].identity_key == "offer:other-buying-option-sku-2"
    discovered = _discovered_offer_targets(
        product,
        submitted_url="https://www.takealot.com/example/PLID123",
    )
    assert [(target.plid, target.url) for target in discovered] == [
        ("123", "https://www.takealot.com/example/PLID123")
    ]
    assert len(product.variants) == 1
    assert product.variants[0].label == "默认款"
    assert product.image_url == "https://img/zoom.jpg"
    assert [review.rating for review in reviews] == [2, 5]


async def test_public_client_uses_conservative_review_page_delay() -> None:
    client = CompetitorPublicClient()
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"page_info": {"total_pages": 2}, "reviews": []},
            {"page_info": {"total_pages": 2}, "reviews": []},
        ]
    )

    with (
        patch(
            "takealot_ops.competitors.api.random.uniform",
            return_value=3.5,
        ) as random_delay,
        patch(
            "takealot_ops.competitors.api.asyncio.sleep",
            AsyncMock(),
        ) as sleep,
    ):
        reviews = await client.fetch_all_reviews("123")

    assert reviews == []
    random_delay.assert_called_once_with(2.0, 5.0)
    sleep.assert_awaited_once_with(3.5)


async def test_public_client_enumerates_variants_under_one_plid() -> None:
    def variant_detail(size: str, *, available: bool) -> dict[str, object]:
        return {
            "title": f"Brace - {size}",
            "desktop_href": f"https://www.takealot.com/brace/PLID96909926?size={size}",
            "gallery": {
                "images": [f"https://img/{size.lower()}-{{size}}.jpg"],
            },
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
                                "value": (
                                    {
                                        "name": value,
                                        "value": value,
                                        "type": "size_variant",
                                    }
                                    if value == size
                                    else value
                                ),
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
        "https://api.takealot.com/rest/v-1-13-0/product-details/PLID96909926?size=Right": variant_detail(
            "Right", available=True
        ),
        "https://api.takealot.com/rest/v-1-13-0/product-details/PLID96909926?size=Left": variant_detail(
            "Left", available=True
        ),
    }

    async def fake_get_json(self, url: str, **kw: object) -> dict[str, object]:
        return canned[url]  # type: ignore[return-value]

    with (
        patch.object(CompetitorPublicClient, "__init__", lambda self, **kw: None),
        patch.object(CompetitorPublicClient, "_get_json", fake_get_json),
        patch.object(CompetitorPublicClient, "close", lambda self: None),
        patch.object(
            CompetitorPublicClient,
            "_human_delay",
            AsyncMock(),
        ) as variant_delay,
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
    assert [variant.image_url for variant in product.variants] == [
        "https://img/right-zoom.jpg",
        "https://img/left-zoom.jpg",
    ]
    assert product.sku == "SKU-Left"
    assert variant_delay.await_args_list == [
        call(3.0, 6.0),
        call(3.0, 6.0),
    ]


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
