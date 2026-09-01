"""Batch-friendly competitor collection and read-only dashboard loading."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import (
    CompetitorNetworkError,
    CompetitorNotFoundError,
    CompetitorPageValidationError,
    CompetitorPublicClient,
    extract_plid,
)
from takealot_ops.competitors.batch import configure_collection_logger
from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    OfferStockObservation,
    PreviousObservation,
    SalesSignal,
    StockProbeResult,
    VariantStockObservation,
    analyze_sales_signal,
    competitor_offer_identity,
    competitor_offer_stock_state,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import (
    NOT_FOUND_CONFIRMATION_COUNT,
    CompetitorRepository,
)
from takealot_ops.competitors.stock import (
    probe_product_stocks,
    skipped_stock_probe,
)
from takealot_ops.competitors.own_store import (
    ConnectedStoreOffer,
    ConnectedStoreOfferPoint,
    connected_store_plids,
    load_connected_store_offer_points,
    load_connected_store_offers,
    own_store_offer_identity,
)
from takealot_ops.storage.models import (
    CompetitorLinkHealth,
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorVariantSnapshot,
)


StoreOfferPoint = ConnectedStoreOfferPoint


@dataclass(frozen=True)
class CompetitorCollectionResult:
    """One URL collection outcome returned to the operator page."""

    plid: str
    title: str
    succeeded: bool
    message: str
    retryable: bool = False
    failure_kind: str | None = None
    discovered_targets: tuple[CompetitorDiscoveredTarget, ...] = ()
    added_target_count: int = 0


@dataclass(frozen=True)
class CompetitorDiscoveredTarget:
    """One crawlable public offer target found while collecting a product."""

    plid: str
    url: str
    title: str
    seller_name: str | None
    price: float | None
    selected: bool


@dataclass(frozen=True)
class CompetitorDataset:
    """Read-only competitor tables used by the Streamlit module."""

    current: pd.DataFrame
    history: pd.DataFrame
    reviews: pd.DataFrame
    variants: pd.DataFrame
    category_paths: dict[str, list[dict[str, str | None]]] = field(
        default_factory=dict
    )
    store_current: pd.DataFrame = field(default_factory=pd.DataFrame)
    store_history: pd.DataFrame = field(default_factory=pd.DataFrame)
    own_follower_events: list[dict[str, object]] = field(default_factory=list)
    available_start_date: date | None = None
    available_end_date: date | None = None
    selected_start_date: date | None = None
    selected_end_date: date | None = None

    def date_range_payload(self) -> dict[str, str | None]:
        """Return API-safe observation range metadata."""
        return {
            "available_start": (
                self.available_start_date.isoformat()
                if self.available_start_date is not None
                else None
            ),
            "available_end": (
                self.available_end_date.isoformat() if self.available_end_date is not None else None
            ),
            "selected_start": (
                self.selected_start_date.isoformat()
                if self.selected_start_date is not None
                else None
            ),
            "selected_end": (
                self.selected_end_date.isoformat() if self.selected_end_date is not None else None
            ),
        }


@dataclass(frozen=True)
class _InventoryTurnoverObservation:
    """One ordered stock/price point used only for read-only interval turnover."""

    scope: tuple[object, ...]
    stock_quantity: int | None
    stock_exact: bool
    price: Decimal | None
    display_date: date | None = None


@dataclass(frozen=True)
class _PeriodInventoryTurnover:
    """Cumulative exact-stock decreases and replenishments for one interval."""

    sales_units: int | None = None
    sales_amount: float | None = None
    replenishment_units: int | None = None
    replenishment_value: float | None = None
    turnover_value: float | None = None


OBSERVED_SALES_WINDOW_DAYS = (7, 15, 30, 60, 90)


def _discovered_offer_targets(
    product: CompetitorProduct,
    *,
    submitted_url: str,
) -> tuple[CompetitorDiscoveredTarget, ...]:
    """Keep the submitted product plus every offer with an explicit public target."""
    targets: dict[str, CompetitorDiscoveredTarget] = {
        product.plid: CompetitorDiscoveredTarget(
            plid=product.plid,
            url=product.url or submitted_url,
            title=product.title,
            seller_name=product.seller_name or None,
            price=product.price,
            selected=True,
        )
    }
    for offer in product.offers:
        if not offer.plid or not offer.url or offer.plid in targets:
            continue
        targets[offer.plid] = CompetitorDiscoveredTarget(
            plid=offer.plid,
            url=offer.url,
            title=product.title,
            seller_name=offer.seller_name or None,
            price=offer.price,
            selected=offer.selected,
        )
    return tuple(targets.values())


class CompetitorCollector:
    """Collect public data and persist one snapshot per explicit target."""

    def __init__(
        self,
        *,
        engine: Engine,
        project_root: Path,
        client: CompetitorPublicClient | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._project_root = project_root
        self._client = client or CompetitorPublicClient()
        self._owns_client = client is None
        self._progress_callback = progress_callback
        self._collection_logger = configure_collection_logger(project_root)

    def _report_stage(self, stage: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(stage)

    def _log_page_validation(self, message: str) -> None:
        self._collection_logger.info(message)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> CompetitorCollector:
        self._report_stage(
            "正在复用后台数据浏览器"
            if self._client.ready
            else "正在启动后台数据浏览器"
        )
        await self._client.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def collect(
        self,
        url: str,
        *,
        with_stock_probe: bool,
        visible_browser: bool = False,
        followers_only: bool = False,
    ) -> CompetitorCollectionResult:
        plid = extract_plid(url)
        try:
            self._report_stage(
                "正在读取公开报价并识别跟卖"
                if followers_only
                else "正在读取商品与变体"
            )
            product = await self._client.fetch_product(url)
            follower_offers = (
                self._own_store_follower_offers(product)
                if followers_only
                else tuple(offer for offer in product.offers if offer.is_follower_offer)
            )
            discovered_targets = (
                ()
                if followers_only
                else _discovered_offer_targets(product, submitted_url=url)
            )
            product_for_storage = (
                replace(
                    product,
                    offers=follower_offers,
                    variants=product.variants,
                    review_count=product.review_count,
                    rating=product.rating,
                )
                if followers_only
                else product
            )
            self._report_stage(
                "正在读取或复用PLID商品共用评论"
                if followers_only
                else "正在读取全部评论"
            )
            reviews = (
                await self._load_own_store_reviews(product)
                if followers_only
                else await self._client.fetch_all_reviews(product.plid)
            )
            self._report_stage(
                (
                    "正在探测跟卖报价库存"
                    if with_stock_probe
                    else "本条未启用跟卖库存探测"
                )
                if followers_only
                else (
                    "正在启动库存探测浏览器"
                    if with_stock_probe
                    else "本条未启用库存探测"
                )
            )
            variant_stocks, offer_stocks = await self._collect_product_stocks(
                product_for_storage,
                enabled=with_stock_probe,
                visible_browser=visible_browser,
                followers_only=followers_only,
            )
            if followers_only:
                variant_stocks = [
                    VariantStockObservation(
                        variant=variant,
                        stock=skipped_stock_probe(),
                    )
                    for variant in product.variants
                ]
            stock = (
                skipped_stock_probe()
                if followers_only
                else _aggregate_variant_stock(variant_stocks)
            )
            self._report_stage(
                "正在保存跟卖观察快照"
                if followers_only
                else "正在保存商品快照"
            )
            collected_at = datetime.now(UTC)
            with Session(self._engine) as session:
                repository = CompetitorRepository(session)
                with session.begin():
                    previous = repository.latest_compatible_snapshot(product_for_storage)
                    summary = summarize_reviews(reviews)
                    lifetime_sales = estimate_lifetime_sales(product_for_storage.review_count)
                    signal = analyze_sales_signal(
                        previous,
                        current_stock_quantity=stock.quantity,
                        current_stock_exact=stock.exact,
                        current_review_count=product_for_storage.review_count,
                    )
                    repository.save_observation(
                        product=product_for_storage,
                        reviews=reviews,
                        review_summary=summary,
                        stock=stock,
                        variant_stocks=variant_stocks,
                        offer_stocks=offer_stocks,
                        lifetime_sales=lifetime_sales,
                        signal=signal,
                        collected_at=collected_at,
                        register_target=not followers_only,
                    )
            failed_stock_count = sum(
                observation.stock.method == "failed" for observation in variant_stocks
            ) + sum(
                observation.stock.method == "failed" for observation in offer_stocks
            )
            if with_stock_probe and failed_stock_count:
                failure_summary = _stock_probe_failure_summary(
                    variant_stocks,
                    offer_stocks,
                )
                seller_quote_count = len(variant_stocks) + len(offer_stocks)
                return CompetitorCollectionResult(
                    plid=plid,
                    title=product.title,
                    succeeded=False,
                    message=(
                        "商品与评论快照已保存，"
                        f"有{len(variant_stocks)}个变体/"
                        f"{seller_quote_count}个卖家报价，"
                        f"其中{failed_stock_count}个报价库存仍未探测；"
                        f"失败原因：{failure_summary}；"
                        "已加入本轮其他链接结束后的库存复探"
                    ),
                    retryable=True,
                    failure_kind="stock-unprobed",
                    discovered_targets=discovered_targets,
                )
            return CompetitorCollectionResult(
                plid=plid,
                title=product.title,
                succeeded=True,
                message=(
                    "自有商品已检查，本次未发现跟卖报价，"
                    f"已保留 {len(variant_stocks)} 个公开变体并同步 "
                    f"{len(reviews)} 条PLID共用评论；未探测主报价库存。"
                    if followers_only and not follower_offers
                    else (
                        "自有商品本身继续使用 Seller API 首拉基准；"
                        "排除全部已接入店铺自有Offer后"
                        f"已记录 {len(offer_stocks)} 个其他卖家报价，"
                        "包含竞争卖家主报价；库存只探测这些跟卖，评论按 PLID 商品共用。"
                    )
                    if followers_only
                    else _collection_message(
                        stock,
                        len(variant_stocks),
                        len(offer_stocks),
                    )
                ),
                discovered_targets=discovered_targets,
            )
        except CompetitorNotFoundError:
            self._report_stage("正在复核疑似失效链接")
            try:
                previously_confirmed = self._is_confirmed_invalid(plid)
                controls = (
                    [] if previously_confirmed else self._recent_control_products(plid)
                )
                control_verified = False
                control_plid: str | None = None
                if controls:
                    try:
                        control_plid = await self._client.confirm_product_page_absent(
                            url,
                            controls,
                            diagnostic_callback=self._log_page_validation,
                        )
                    except CompetitorPageValidationError as exc:
                        return CompetitorCollectionResult(
                            plid=plid,
                            title=f"PLID{plid}",
                            succeeded=False,
                            message=str(exc),
                            retryable=True,
                            failure_kind="validation-uncertain",
                        )
                    except CompetitorNetworkError as exc:
                        return CompetitorCollectionResult(
                            plid=plid,
                            title=f"PLID{plid}",
                            succeeded=False,
                            message=str(exc),
                            retryable=True,
                            failure_kind="network",
                        )
                    control_verified = True
                checked_at = datetime.now(UTC)
                with Session(self._engine) as session:
                    repository = CompetitorRepository(session)
                    with session.begin():
                        decision = repository.record_not_found(
                            plid=plid,
                            url=url,
                            checked_at=checked_at,
                            control_plid=control_plid,
                            control_check_ok=control_verified,
                        )
            except SQLAlchemyError as exc:
                return CompetitorCollectionResult(
                    plid=plid,
                    title=f"PLID{plid}",
                    succeeded=False,
                    message=f"记录链接复核状态失败：{exc}",
                    failure_kind="other",
                )
            confirmed = decision.status == "confirmed_invalid"
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=False,
                message=_not_found_message(
                    confirmed=confirmed,
                    count=decision.confirmed_not_found_count,
                    evidence_counted=decision.evidence_counted,
                    control_verified=control_verified,
                    previously_confirmed=previously_confirmed,
                ),
                failure_kind=("confirmed-invalid" if confirmed else "suspected-invalid"),
            )
        except CompetitorNetworkError as exc:
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=False,
                message=str(exc),
                retryable=True,
                failure_kind="network",
            )
        except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=False,
                message=str(exc),
                failure_kind="other",
            )

    async def _load_own_store_reviews(
        self,
        product: CompetitorProduct,
    ) -> list[CompetitorReviewRecord]:
        """Fetch own-store comments on first sight/change, otherwise reuse stored bodies."""
        with Session(self._engine) as session:
            stored = list(
                session.scalars(
                    select(CompetitorReview)
                    .where(CompetitorReview.plid == product.plid)
                    .order_by(CompetitorReview.review_date.desc())
                )
            )
        if len(stored) == product.review_count:
            return [
                CompetitorReviewRecord(
                    review_id=row.review_id,
                    rating=row.rating,
                    title=row.title or "",
                    body=row.body or "",
                    customer_name=row.customer_name or "",
                    review_date=row.review_date or "",
                )
                for row in stored
            ]
        return await self._client.fetch_all_reviews(product.plid)

    def _own_store_follower_offers(
        self,
        product: CompetitorProduct,
    ) -> tuple[CompetitorOffer, ...]:
        """Exclude exact Seller API Offer IDs/SKUs, regardless of Buy Box position."""
        with Session(self._engine) as session:
            own_identity = own_store_offer_identity(session, product.plid)

        def is_own_offer(offer: CompetitorOffer) -> bool:
            offer_id = _normalized_offer_scope(offer.offer_id)
            sku = _normalized_offer_scope(offer.sku)
            own_scopes = own_identity.offer_ids | own_identity.skus
            return bool(
                (offer_id and offer_id in own_scopes)
                or (sku and sku in own_scopes)
            )

        return tuple(offer for offer in product.offers if not is_own_offer(offer))

    def _recent_control_products(self, plid: str) -> list[tuple[str, str]]:
        with Session(self._engine) as session:
            return CompetitorRepository(session).recent_control_products(
                exclude_plid=plid,
                limit=3,
            )

    def _is_confirmed_invalid(self, plid: str) -> bool:
        with Session(self._engine) as session:
            return CompetitorRepository(session).is_confirmed_invalid(plid)

    async def _collect_product_stocks(
        self,
        product: CompetitorProduct,
        *,
        enabled: bool,
        visible_browser: bool,
        followers_only: bool = False,
    ) -> tuple[list[VariantStockObservation], list[OfferStockObservation]]:
        if not enabled:
            variant_stocks = [
                VariantStockObservation(variant=variant, stock=skipped_stock_probe())
                for variant in (() if followers_only else product.variants)
            ]
            offer_stocks = [
                OfferStockObservation(offer=offer, stock=skipped_stock_probe())
                for offer in product.offers
                if followers_only or offer.is_follower_offer
            ]
            return variant_stocks, offer_stocks
        try:
            return await probe_product_stocks(
                product,
                profile_dir=(
                    self._project_root
                    / "data"
                    / (
                        "own-store-follower-browser-profile"
                        if followers_only
                        else "competitor-browser-profile"
                    )
                ),
                visible=visible_browser,
                probe_buyboxes=not followers_only,
                probe_offer_buyboxes=followers_only,
            )
        except (OSError, RuntimeError) as exc:
            failed = StockProbeResult(quantity=None, exact=False, method="failed", note=str(exc))
            variant_stocks = [
                VariantStockObservation(variant=variant, stock=failed)
                for variant in (() if followers_only else product.variants)
            ]
            offer_stocks = [
                OfferStockObservation(offer=offer, stock=failed)
                for offer in product.offers
                if followers_only or offer.is_follower_offer
            ]
            return variant_stocks, offer_stocks


def parse_competitor_urls(raw: str) -> list[str]:
    """Parse newline-separated URLs and deduplicate them by PLID."""
    by_plid: dict[str, str] = {}
    for line in raw.splitlines():
        url = line.strip()
        if not url:
            continue
        plid = extract_plid(url)
        by_plid.setdefault(plid, url)
    return list(by_plid.values())


def _aggregate_variant_stock(
    observations: list[VariantStockObservation],
) -> StockProbeResult:
    if not observations:
        return StockProbeResult(
            quantity=None,
            exact=False,
            method="failed",
            note="公开接口没有返回可识别的变体。",
        )
    stocks = [item.stock for item in observations]
    if all(stock.method == "skipped" for stock in stocks):
        return skipped_stock_probe()
    quantities = [stock.quantity for stock in stocks]
    quantity = (
        sum(value for value in quantities if value is not None)
        if all(value is not None for value in quantities)
        else None
    )
    exact = quantity is not None and all(stock.exact for stock in stocks)
    all_unavailable = all(
        stock.method in {"not-platform-stock", "out-of-stock"} for stock in stocks
    )
    limited_variant_count = sum(stock.customer_purchase_limit is not None for stock in stocks)
    note = f"汇总 {len(observations)} 个变体的平台仓有效库存；供应商调货与长时效到货按0计。"
    if limited_variant_count:
        note += (
            f"其中 {limited_variant_count} 个变体存在每位客户限购，"
            "达到限购数的变体只保守计入至少数量。"
        )
    return StockProbeResult(
        quantity=quantity,
        exact=exact,
        method="all-variants-out-of-stock" if all_unavailable else "variant-aggregate",
        note=note,
    )


def _stock_probe_failure_summary(
    observations: list[VariantStockObservation],
    offer_observations: list[OfferStockObservation],
) -> str:
    failed = [item for item in observations if item.stock.method == "failed"]
    details: list[str] = []
    for observation in failed[:3]:
        label = observation.variant.label.strip() or "默认变体"
        sku = observation.variant.sku.strip()
        identity = f"{label}（SKU {sku}）" if sku else label
        note = " ".join(observation.stock.note.split())
        note = note.split("Call log:", 1)[0].strip() or "未返回具体原因"
        details.append(f"{identity}：{note[:240]}")
    failed_offers = [
        item for item in offer_observations if item.stock.method == "failed"
    ]
    for offer_observation in failed_offers[: max(0, 3 - len(details))]:
        seller = offer_observation.offer.seller_name.strip() or "未知卖家"
        sku = offer_observation.offer.sku.strip()
        offer_id = offer_observation.offer.offer_id or "无 Offer ID"
        identity = f"跟卖 {seller}（SKU {sku or '未知'}，{offer_id}）"
        note = " ".join(offer_observation.stock.note.split())
        note = note.split("Call log:", 1)[0].strip() or "未返回具体原因"
        details.append(f"{identity}：{note[:240]}")
    remaining = len(failed) + len(failed_offers) - len(details)
    if remaining > 0:
        details.append(f"另 {remaining} 个失败库存项详见库存说明")
    return "；".join(details) or "未返回具体原因"


def _collection_message(
    stock: StockProbeResult,
    variant_count: int,
    follower_offer_count: int,
) -> str:
    if stock.method == "failed":
        return f"公开数据已保存；库存探测未取得：{stock.note}"
    return (
        f"采集成功；已记录 {variant_count} 个变体和 {follower_offer_count} 个跟卖报价，"
        "评论按商品共用一份"
    )


def _not_found_message(
    *,
    confirmed: bool,
    count: int,
    evidence_counted: bool,
    control_verified: bool,
    previously_confirmed: bool,
) -> str:
    if previously_confirmed:
        return (
            "该链接此前已确认失效，本次公开商品数据再次返回 404；"
            "已按一次复核规则维持确认失效，本批不再重复复核"
        )
    if confirmed:
        return (
            f"商品页持续为空且正常对照商品可用，已完成 {count} 次间隔复核，"
            "确认链接失效；本批后续续爬将自动跳过，重新点击“开始采集”仍可人工复核"
        )
    if not control_verified:
        return (
            "Takealot 商品数据返回 404，暂标记为疑似失效；"
            "当前没有可用的正常对照商品，本次不做永久判定并保留重试"
        )
    if not evidence_counted:
        return (
            f"商品页为空且正常对照商品可用，仍为疑似失效（有效复核 "
            f"{count}/{NOT_FOUND_CONFIRMATION_COUNT}）；距离上次复核不足 10 分钟，"
            "本次不累计并保留重试"
        )
    return (
        f"商品页为空且正常对照商品可用，暂标记为疑似失效（有效复核 "
        f"{count}/{NOT_FOUND_CONFIRMATION_COUNT}）；至少间隔 10 分钟后再次复核"
    )


def load_competitor_link_health(engine: Engine) -> list[dict[str, object]]:
    """Load suspected/confirmed invalid links for the operator review list."""
    latest_snapshots: dict[str, CompetitorSnapshot] = {}
    try:
        with Session(engine) as session:
            store_plids = connected_store_plids(session)
            rows = list(
                session.scalars(
                    select(CompetitorLinkHealth)
                    .where(
                        CompetitorLinkHealth.status != "healthy",
                        CompetitorLinkHealth.plid.not_in(store_plids),
                    )
                    .order_by(
                        CompetitorLinkHealth.status.desc(),
                        CompetitorLinkHealth.last_checked_at.desc(),
                    )
                )
            )
            if rows:
                plids = [row.plid for row in rows]
                snapshots = session.scalars(
                    select(CompetitorSnapshot)
                    .where(CompetitorSnapshot.plid.in_(plids))
                    .order_by(CompetitorSnapshot.collected_at.desc())
                )
                for snapshot in snapshots:
                    latest_snapshots.setdefault(snapshot.plid, snapshot)
    except SQLAlchemyError:
        return []
    return [
        {
            "plid": row.plid,
            "url": row.url,
            "商品": (latest_snapshots[row.plid].title if row.plid in latest_snapshots else None),
            "图片": (
                latest_snapshots[row.plid].image_url if row.plid in latest_snapshots else None
            ),
            "status": row.status,
            "confirmed_not_found_count": row.confirmed_not_found_count,
            "first_not_found_at": row.first_not_found_at,
            "last_checked_at": row.last_checked_at,
            "control_plid": row.control_plid,
            "control_check_ok": row.control_check_ok,
            "last_error": row.last_error,
        }
        for row in rows
    ]


COMPETITOR_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _period_inventory_turnover(
    observations: list[_InventoryTurnoverObservation],
) -> _PeriodInventoryTurnover:
    """Value comparable exact-stock movements at each later point's price.

    Inventory scopes are isolated before calculation.  Inexact observations and
    scopes with fewer than two exact points are skipped, while repeated exact points
    from the same scope remain comparable across missing or unusable observations.
    Only an interval with no comparable exact pair remains unavailable.  Amounts are
    inventory observations, not Takealot order revenue: cart occupancy,
    cancellations and replenishment can all affect the underlying stock sequence.
    """

    exact_timelines_by_scope: dict[
        tuple[object, ...], list[_InventoryTurnoverObservation]
    ] = {}
    for observation in observations:
        if not observation.stock_exact or observation.stock_quantity is None:
            continue
        exact_timelines_by_scope.setdefault(observation.scope, []).append(observation)

    comparable_timelines = [
        timeline for timeline in exact_timelines_by_scope.values() if len(timeline) >= 2
    ]
    if not comparable_timelines:
        return _PeriodInventoryTurnover()

    sales_units = 0
    replenishment_units = 0
    sales_amount: Decimal | None = Decimal("0")
    replenishment_value: Decimal | None = Decimal("0")
    for timeline in comparable_timelines:
        for previous, current in zip(timeline, timeline[1:]):
            previous_quantity = previous.stock_quantity
            current_quantity = current.stock_quantity
            if previous_quantity is None or current_quantity is None:
                continue
            change = current_quantity - previous_quantity
            if change < 0:
                units = abs(change)
                sales_units += units
                if current.price is None:
                    sales_amount = None
                elif sales_amount is not None:
                    sales_amount += current.price * units
            elif change > 0:
                replenishment_units += change
                if current.price is None:
                    replenishment_value = None
                elif replenishment_value is not None:
                    replenishment_value += current.price * change

    turnover_value = (
        sales_amount + replenishment_value
        if sales_amount is not None and replenishment_value is not None
        else None
    )
    return _PeriodInventoryTurnover(
        sales_units=sales_units,
        sales_amount=float(sales_amount) if sales_amount is not None else None,
        replenishment_units=replenishment_units,
        replenishment_value=(
            float(replenishment_value) if replenishment_value is not None else None
        ),
        turnover_value=float(turnover_value) if turnover_value is not None else None,
    )


def _recent_observed_sales_units(
    observations: list[_InventoryTurnoverObservation],
) -> tuple[dict[str, int | None], date | None]:
    """Calculate fixed inclusive windows ending on the latest available local date."""

    dated_observations = [
        observation
        for observation in observations
        if observation.display_date is not None
    ]
    if not dated_observations:
        return ({str(days): None for days in OBSERVED_SALES_WINDOW_DAYS}, None)
    through_date = max(
        cast(date, observation.display_date) for observation in dated_observations
    )
    values: dict[str, int | None] = {}
    for days in OBSERVED_SALES_WINDOW_DAYS:
        start_date = through_date - timedelta(days=days - 1)
        window = [
            observation
            for observation in dated_observations
            if start_date <= cast(date, observation.display_date) <= through_date
        ]
        values[str(days)] = _period_inventory_turnover(window).sales_units
    return values, through_date


def _snapshot_inventory_turnover_observations(
    snapshots: list[CompetitorSnapshot],
    *,
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]],
) -> list[_InventoryTurnoverObservation]:
    ordered = sorted(snapshots, key=lambda row: (row.collected_at, row.id))
    return [
        _InventoryTurnoverObservation(
            scope=(
                row.sku,
                row.seller_id,
                variant_signatures.get(row.id, frozenset()),
            ),
            stock_quantity=row.stock_quantity,
            stock_exact=row.stock_exact,
            price=Decimal(str(row.price)) if row.price is not None else None,
            display_date=_competitor_display_date(row.collected_at),
        )
        for row in ordered
    ]


def _snapshot_period_inventory_turnover(
    snapshots: list[CompetitorSnapshot],
    *,
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]],
) -> _PeriodInventoryTurnover:
    return _period_inventory_turnover(
        _snapshot_inventory_turnover_observations(
            snapshots,
            variant_signatures=variant_signatures,
        ),
    )


def _snapshot_recent_observed_sales_units(
    snapshots: list[CompetitorSnapshot],
    *,
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]],
) -> tuple[dict[str, int | None], date | None]:
    return _recent_observed_sales_units(
        _snapshot_inventory_turnover_observations(
            snapshots,
            variant_signatures=variant_signatures,
        )
    )


def load_competitor_dataset(
    engine: Engine,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    own_store_codes: set[str] | None = None,
    plids: set[str] | None = None,
    include_detail_frames: bool = True,
    own_store_only: bool = False,
    include_store_projection: bool = True,
) -> CompetitorDataset:
    """Load competitor views and recompute signals across the selected interval.

    True competitors always exclude private PLIDs from every connected store.  The
    optional ``own_store_codes`` filter only controls which private-store cards are
    projected, so a single-store view cannot leak or misclassify another store's
    private products. List-only callers can skip detail-only frames, while scope
    switches can request only the private-store partition without rebuilding the
    invariant true-competitor list. The main page can likewise omit the private
    partition so its default true-competitor cards return before the larger Seller
    API history projection finishes independently.
    """
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    normalized_plids = (
        {str(plid).strip() for plid in plids if str(plid).strip()}
        if plids is not None
        else None
    )
    if normalized_plids is not None and not normalized_plids:
        return CompetitorDataset(
            current=pd.DataFrame(),
            history=pd.DataFrame(),
            reviews=pd.DataFrame(),
            variants=pd.DataFrame(),
            selected_start_date=start_date,
            selected_end_date=end_date,
        )
    try:
        with Session(engine) as session:
            connected_store_offers = load_connected_store_offers(
                session,
                plids=normalized_plids,
            )
            selected_store_plids_for_query = {
                str(item.offer.productline_id).strip()
                for item in connected_store_offers
                if (
                    (own_store_codes is None or item.store_code in own_store_codes)
                    and str(item.offer.productline_id or "").strip()
                )
            }
            target_statement = (
                select(CompetitorTarget)
                .where(CompetitorTarget.active.is_(True))
                .order_by(CompetitorTarget.updated_at.desc())
            )
            snapshot_statement = select(CompetitorSnapshot).order_by(
                CompetitorSnapshot.collected_at.desc()
            )
            review_statement = select(CompetitorReview).order_by(
                CompetitorReview.review_date.desc()
            )
            variant_statement = select(CompetitorVariantSnapshot).order_by(
                CompetitorVariantSnapshot.collected_at.desc(),
                CompetitorVariantSnapshot.id.asc(),
            )
            if own_store_only:
                selected_plids = selected_store_plids_for_query
                snapshot_statement = snapshot_statement.where(
                    CompetitorSnapshot.plid.in_(selected_plids)
                )
                review_statement = review_statement.where(
                    CompetitorReview.plid.in_(selected_plids)
                )
                variant_statement = variant_statement.where(
                    CompetitorVariantSnapshot.plid.in_(selected_plids)
                )
            elif normalized_plids is not None:
                target_statement = target_statement.where(
                    CompetitorTarget.plid.in_(normalized_plids)
                )
                snapshot_statement = snapshot_statement.where(
                    CompetitorSnapshot.plid.in_(normalized_plids)
                )
                review_statement = review_statement.where(
                    CompetitorReview.plid.in_(normalized_plids)
                )
                variant_statement = variant_statement.where(
                    CompetitorVariantSnapshot.plid.in_(normalized_plids)
                )
            targets = (
                [] if own_store_only else list(session.scalars(target_statement))
            )
            if not include_store_projection and not own_store_only:
                all_store_plids_for_query = {
                    str(item.offer.productline_id).strip()
                    for item in connected_store_offers
                    if str(item.offer.productline_id or "").strip()
                }
                true_competitor_plids = {
                    target.plid for target in targets
                } - all_store_plids_for_query
                snapshot_statement = snapshot_statement.where(
                    CompetitorSnapshot.plid.in_(true_competitor_plids)
                )
                review_statement = review_statement.where(
                    CompetitorReview.plid.in_(true_competitor_plids)
                )
                variant_statement = variant_statement.where(
                    CompetitorVariantSnapshot.plid.in_(true_competitor_plids)
                )
            should_load_projection_rows = (
                not own_store_only or bool(selected_store_plids_for_query)
            )
            snapshots = (
                list(session.scalars(snapshot_statement))
                if should_load_projection_rows
                else []
            )
            reviews = (
                list(session.scalars(review_statement))
                if include_detail_frames and should_load_projection_rows
                else []
            )
            variants = (
                list(session.scalars(variant_statement))
                if should_load_projection_rows
                else []
            )
            store_baselines = (
                load_connected_store_offer_points(
                    session,
                    plids=normalized_plids,
                    store_codes=own_store_codes,
                )
                if include_store_projection
                else []
            )
    except SQLAlchemyError:
        return CompetitorDataset(
            current=pd.DataFrame(),
            history=pd.DataFrame(),
            reviews=pd.DataFrame(),
            variants=pd.DataFrame(),
            selected_start_date=start_date,
            selected_end_date=end_date,
        )

    all_store_plids = {
        str(item.offer.productline_id).strip()
        for item in connected_store_offers
        if str(item.offer.productline_id or "").strip()
    }
    own_offer_ids_by_plid: dict[str, set[str]] = {}
    own_skus_by_plid: dict[str, set[str]] = {}
    for item in connected_store_offers:
        plid = str(item.offer.productline_id or "").strip()
        if not plid:
            continue
        offer_id = _normalized_offer_scope(item.offer.offer_id)
        sku = _normalized_offer_scope(item.offer.sku)
        if offer_id:
            own_offer_ids_by_plid.setdefault(plid, set()).add(offer_id)
        if sku:
            own_skus_by_plid.setdefault(plid, set()).add(sku)
    selected_store_offers = (
        [
            item
            for item in connected_store_offers
            if own_store_codes is None or item.store_code in own_store_codes
        ]
        if include_store_projection
        else []
    )
    selected_store_plids = {
        str(item.offer.productline_id).strip()
        for item in selected_store_offers
        if str(item.offer.productline_id or "").strip()
    }
    store_names_by_code = {
        item.store_code: item.store_name for item in selected_store_offers
    }
    store_tsin_by_offer = {
        (item.store_code, str(item.offer.offer_id)): item.offer.tsin_id
        for item in selected_store_offers
        if item.offer.tsin_id
    }
    store_baselines = [
        row
        for row in store_baselines
        if (
            (own_store_codes is None or row.store_code in own_store_codes)
            and str(row.productline_id or "").strip() in selected_store_plids
        )
    ]
    own_store_start_dates: dict[str, date] = {}
    for row in store_baselines:
        plid = str(row.productline_id or "").strip()
        current_start = own_store_start_dates.get(plid)
        if plid and (current_start is None or row.display_date < current_start):
            own_store_start_dates[plid] = row.display_date
    active_plids = {target.plid for target in targets} - all_store_plids
    active_snapshots = [row for row in snapshots if row.plid in active_plids]
    store_snapshots = [row for row in snapshots if row.plid in selected_store_plids]
    category_paths: dict[str, list[dict[str, str | None]]] = {}
    for snapshot in snapshots:
        if snapshot.plid in category_paths:
            continue
        category_path = _snapshot_category_path(snapshot)
        if category_path:
            category_paths[snapshot.plid] = category_path
    snapshot_dates = [
        *(_competitor_display_date(row.collected_at) for row in active_snapshots),
        *(_competitor_display_date(row.collected_at) for row in store_snapshots),
        *(row.display_date for row in store_baselines),
    ]
    available_start_date = min(snapshot_dates, default=None)
    available_end_date = max(snapshot_dates, default=None)
    selected_start_date = start_date or available_start_date
    selected_end_date = end_date or available_end_date
    interval_snapshots = [
        row
        for row in active_snapshots
        if (
            selected_start_date is None
            or _competitor_display_date(row.collected_at) >= selected_start_date
        )
        and (
            selected_end_date is None
            or _competitor_display_date(row.collected_at) <= selected_end_date
        )
    ]
    interval_store_snapshots = [
        row
        for row in store_snapshots
        if (
            selected_start_date is None
            or _competitor_display_date(row.collected_at) >= selected_start_date
        )
        and (
            selected_end_date is None
            or _competitor_display_date(row.collected_at) <= selected_end_date
        )
    ]
    competitor_follower_timelines = (
        _follower_seller_timelines(
            active_snapshots,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
        )
        if not own_store_only
        else {}
    )
    store_follower_timelines = (
        _follower_seller_timelines(
            store_snapshots,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
            own_offer_ids_by_plid=own_offer_ids_by_plid,
            own_skus_by_plid=own_skus_by_plid,
            not_before_by_plid=own_store_start_dates,
        )
        if include_store_projection
        else {}
    )

    latest_by_plid: dict[str, CompetitorSnapshot] = {}
    snapshots_by_plid: dict[str, list[CompetitorSnapshot]] = {}
    for snapshot in interval_snapshots:
        latest_by_plid.setdefault(snapshot.plid, snapshot)
        snapshots_by_plid.setdefault(snapshot.plid, []).append(snapshot)
    all_snapshots_by_plid: dict[str, list[CompetitorSnapshot]] = {}
    for snapshot in active_snapshots:
        all_snapshots_by_plid.setdefault(snapshot.plid, []).append(snapshot)
    variants_by_snapshot: dict[int, list[CompetitorVariantSnapshot]] = {}
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]] = {}
    for variant in variants:
        variants_by_snapshot.setdefault(variant.snapshot_id, []).append(variant)
        signature = variant_signatures.setdefault(variant.snapshot_id, frozenset())
        variant_signatures[variant.snapshot_id] = signature | {
            (
                variant.variant_key,
                variant.sku or "",
                variant.seller_id or "",
            )
        }
    stale_stock_by_plid: dict[str, CompetitorSnapshot] = {}
    for plid, latest in latest_by_plid.items():
        if latest.stock_quantity is not None:
            continue
        latest_signature = variant_signatures.get(latest.id, frozenset())
        for candidate in interval_snapshots:
            if candidate.plid != plid or candidate.id == latest.id:
                continue
            if candidate.stock_quantity is None:
                continue
            if candidate.sku != latest.sku or candidate.seller_id != latest.seller_id:
                continue
            if variant_signatures.get(candidate.id, frozenset()) != latest_signature:
                continue
            stale_stock_by_plid[plid] = candidate
            break
    current_rows: list[dict[str, object]] = []
    for plid, latest in latest_by_plid.items():
        if plid not in active_plids:
            continue
        interval = snapshots_by_plid[plid]
        all_plid_snapshots = all_snapshots_by_plid[plid]
        first_monitored_at = min(row.collected_at for row in all_plid_snapshots)
        latest_review_snapshot = max(
            all_plid_snapshots,
            key=lambda row: (row.collected_at, row.id),
        )
        oldest = interval[-1]
        signal, stock_change, stock_comparable = _interval_sales_signal(
            oldest,
            latest,
            variant_signatures=variant_signatures,
        )
        positive_review_delta, negative_review_delta = _interval_review_category_deltas(
            oldest,
            latest,
        )
        inventory_turnover = _snapshot_period_inventory_turnover(
            interval,
            variant_signatures=variant_signatures,
        )
        recent_observed_sales, recent_observed_sales_through = (
            _snapshot_recent_observed_sales_units(
                all_snapshots_by_plid[plid],
                variant_signatures=variant_signatures,
            )
        )
        price_start, price_change, price_signal = _interval_price_signal(oldest, latest)
        offer_rows = _interval_offer_rows(
            oldest,
            latest,
            oldest_variants=variants_by_snapshot.get(oldest.id, []),
            latest_variants=variants_by_snapshot.get(latest.id, []),
        )
        current_rows.append(
            _snapshot_row(
                latest,
                stale_stock=stale_stock_by_plid.get(plid),
                signal=signal,
                signal_start=oldest.collected_at,
                signal_end=latest.collected_at,
                interval_snapshot_count=len(interval),
                stock_change=stock_change,
                stock_comparable=stock_comparable,
                inventory_turnover=inventory_turnover,
                recent_observed_sales=recent_observed_sales,
                recent_observed_sales_through=recent_observed_sales_through,
                positive_review_delta=positive_review_delta,
                negative_review_delta=negative_review_delta,
                price_start=price_start,
                price_change=price_change,
                price_signal=price_signal,
                offer_rows=offer_rows,
                follower_timeline=competitor_follower_timelines.get(plid),
                first_monitored_at=first_monitored_at,
                latest_review_count=latest_review_snapshot.review_count,
                latest_review_collected_at=latest_review_snapshot.collected_at,
            )
        )
    current = pd.DataFrame(current_rows)
    history = (
        pd.DataFrame(
            [
                _snapshot_row(
                    row,
                    offer_rows=_interval_offer_rows(
                        row,
                        row,
                        oldest_variants=variants_by_snapshot.get(row.id, []),
                        latest_variants=variants_by_snapshot.get(row.id, []),
                        raw_history=True,
                    ),
                    raw_history=True,
                )
                for row in interval_snapshots
            ]
        )
        if include_detail_frames
        else pd.DataFrame()
    )
    review_frame = (
        pd.DataFrame(
            [
                {
                    "plid": row.plid,
                    "评论日期": row.review_date,
                    "星级": row.rating,
                    "标题": row.title,
                    "评论内容": row.body,
                    "评论人": row.customer_name,
                }
                for row in reviews
            ]
        )
        if include_detail_frames
        else pd.DataFrame()
    )
    latest_store_snapshots: dict[str, CompetitorSnapshot] = {}
    for snapshot in interval_store_snapshots:
        latest_store_snapshots.setdefault(snapshot.plid, snapshot)
    detail_snapshots = [*interval_snapshots, *latest_store_snapshots.values()]
    interval_snapshot_ids = {snapshot.id for snapshot in detail_snapshots}
    snapshot_images = {snapshot.id: snapshot.image_url for snapshot in detail_snapshots}
    variant_frame = (
        pd.DataFrame(
            [
                _variant_row(
                    row,
                    default_image_url=snapshot_images.get(row.snapshot_id),
                )
                for row in variants
                if row.snapshot_id in interval_snapshot_ids
            ]
        )
        if include_detail_frames
        else pd.DataFrame()
    )
    store_current = pd.DataFrame(
        _store_snapshot_rows(
            store_baselines,
            interval_store_snapshots,
            all_follower_snapshots=store_snapshots,
            current_store_offers=selected_store_offers,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
            store_names_by_code=store_names_by_code,
            own_offer_ids_by_plid=own_offer_ids_by_plid,
            own_skus_by_plid=own_skus_by_plid,
            follower_timelines=store_follower_timelines,
            store_tsin_by_offer=store_tsin_by_offer,
        )
    )
    store_history = (
        pd.DataFrame(
            _store_history_rows(
                store_baselines,
                interval_store_snapshots,
                selected_start_date=selected_start_date,
                selected_end_date=selected_end_date,
                store_names_by_code=store_names_by_code,
                own_offer_ids_by_plid=own_offer_ids_by_plid,
                own_skus_by_plid=own_skus_by_plid,
            )
        )
        if include_detail_frames
        else pd.DataFrame()
    )
    return CompetitorDataset(
        current=current,
        history=history,
        reviews=review_frame,
        variants=variant_frame,
        category_paths=category_paths,
        store_current=store_current,
        store_history=store_history,
        own_follower_events=_own_follower_event_rows(
            selected_store_offers,
            store_follower_timelines,
        ),
        available_start_date=available_start_date,
        available_end_date=available_end_date,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
    )


def _competitor_display_date(value: datetime) -> date:
    captured_at = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return captured_at.astimezone(COMPETITOR_DISPLAY_TIMEZONE).date()


def _interval_sales_signal(
    oldest: CompetitorSnapshot,
    latest: CompetitorSnapshot,
    *,
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]],
) -> tuple[SalesSignal, int | None, bool]:
    if oldest.id == latest.id:
        return (
            analyze_sales_signal(
                None,
                current_stock_quantity=latest.stock_quantity,
                current_stock_exact=latest.stock_exact,
                current_review_count=latest.review_count,
            ),
            None,
            False,
        )

    same_inventory_scope = (
        oldest.sku == latest.sku
        and oldest.seller_id == latest.seller_id
        and variant_signatures.get(oldest.id, frozenset())
        == variant_signatures.get(latest.id, frozenset())
    )
    stock_comparable = (
        same_inventory_scope
        and oldest.stock_exact
        and latest.stock_exact
        and oldest.stock_quantity is not None
        and latest.stock_quantity is not None
    )
    previous = PreviousObservation(
        snapshot_id=oldest.id,
        collected_at=oldest.collected_at,
        stock_quantity=oldest.stock_quantity if same_inventory_scope else None,
        stock_exact=oldest.stock_exact if same_inventory_scope else False,
        review_count=oldest.review_count,
    )
    signal = analyze_sales_signal(
        previous,
        current_stock_quantity=latest.stock_quantity if same_inventory_scope else None,
        current_stock_exact=latest.stock_exact if same_inventory_scope else False,
        current_review_count=latest.review_count,
    )
    stock_change = (
        latest.stock_quantity - oldest.stock_quantity
        if stock_comparable
        and latest.stock_quantity is not None
        and oldest.stock_quantity is not None
        else None
    )
    if not stock_comparable and signal.review_delta == 0:
        reason = (
            "区间首尾变体键、SKU或卖家集合不同"
            if not same_inventory_scope
            else "区间首尾库存缺失或不是精确值"
        )
        signal = replace(
            signal,
            trend_label="库存不可比，评论无新增",
            trend_note=f"{reason}，因此不计算库存净变化；区间首尾评论数没有增加。",
        )
    elif not stock_comparable:
        reason = (
            "区间首尾变体键、SKU或卖家集合不同"
            if not same_inventory_scope
            else "区间首尾库存缺失或不是精确值"
        )
        signal = replace(
            signal,
            trend_note=f"{reason}，库存不参与本区间信号；{signal.trend_note}",
        )
    return signal, stock_change, stock_comparable


def _stock_text(row: CompetitorSnapshot) -> str:
    stock_text = "未探测"
    if row.stock_method in {"not-platform-stock", "all-variants-out-of-stock"}:
        stock_text = "没货"
    elif row.stock_quantity is not None:
        stock_text = str(row.stock_quantity) if row.stock_exact else f"至少{row.stock_quantity}"
    return stock_text


def _snapshot_category_path(
    row: CompetitorSnapshot,
) -> list[dict[str, str | None]]:
    """Return only persisted public breadcrumb evidence in its original order."""

    raw_path: object = row.category_path
    if not isinstance(raw_path, list):
        return []
    result: list[dict[str, str | None]] = []
    for raw_item_value in raw_path[:12]:
        raw_item: object = raw_item_value
        if not isinstance(raw_item, Mapping):
            continue
        name = " ".join(str(raw_item.get("name") or "").split())[:200]
        if not name:
            continue
        result.append(
            {
                "name": name,
                "id": str(raw_item.get("id") or "").strip()[:100] or None,
                "type": str(raw_item.get("type") or "").strip()[:50] or None,
                "slug": str(raw_item.get("slug") or "").strip()[:255] or None,
            }
        )
    return result


def _interval_price_signal(
    oldest: CompetitorSnapshot,
    latest: CompetitorSnapshot,
) -> tuple[float | None, float | None, str]:
    """Compare price only across the selected interval's oldest/latest snapshots."""
    start_price = float(oldest.price) if oldest.price is not None else None
    if oldest.id == latest.id:
        return start_price, None, "待建立价格基线"
    if oldest.price is None or latest.price is None:
        return start_price, None, "价格不可比"
    change = latest.price - oldest.price
    if change < 0:
        label = "降价"
    elif change > 0:
        label = "涨价"
    else:
        label = "价格不变"
    return start_price, float(change), label


def _interval_review_category_deltas(
    oldest: CompetitorSnapshot,
    latest: CompetitorSnapshot,
) -> tuple[int | None, int | None]:
    """Compare PLID-level positive/negative review buckets across interval endpoints."""
    if oldest.id == latest.id:
        return None, None
    return (
        max(0, latest.positive_reviews - oldest.positive_reviews),
        max(0, latest.negative_reviews - oldest.negative_reviews),
    )


def _offer_identity_from_mapping(offer: Mapping[str, object]) -> str | None:
    identity = str(offer.get("identity_key") or "").strip()
    if identity:
        return identity
    return competitor_offer_identity(
        offer_id=offer.get("offer_id"),
        seller_id=offer.get("seller_id"),
        seller_name=offer.get("seller_name"),
        sku=offer.get("sku"),
        variant_key=offer.get("variant_key"),
        condition=offer.get("condition"),
    )


def _snapshot_offers(row: CompetitorSnapshot) -> list[Mapping[str, object]]:
    value: object = row.offers or []
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _follow_selling_opportunity(
    row: CompetitorSnapshot,
) -> tuple[bool, str | None, str, int | None]:
    """Classify only complete public-offer evidence from a successful snapshot."""

    value: object = row.offers
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            value = None
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        return (
            False,
            None,
            "旧快照没有完整公开报价列表，不能判定为跟卖机会。",
            None,
        )

    offers = cast(list[Mapping[str, object]], value)
    offer_count = len(offers)
    if offer_count == 0:
        return (
            True,
            "暂无卖家报价",
            "本次公开商品采集成功，但平台没有返回任何卖家报价。",
            0,
        )

    stock_states = [_offer_stock_state(offer) for offer in offers]
    if all(state == "没货" for state in stock_states):
        return (
            True,
            "全部报价售罄",
            f"本次共采集到 {offer_count} 个公开报价，且每个报价都有明确没货证据。",
            offer_count,
        )
    return (
        False,
        None,
        "当前公开报价中仍有可售或库存未知报价，不列为跟卖机会。",
        offer_count,
    )


def _normalized_offer_scope(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _variant_offer_identity(variant: CompetitorVariantSnapshot) -> str | None:
    identity = competitor_offer_identity(
        seller_id=variant.seller_id,
        seller_name=variant.seller_name,
        sku=variant.sku,
        variant_key=variant.variant_key,
    )
    if identity is not None:
        return identity
    sku = _normalized_offer_scope(variant.sku)
    variant_key = _normalized_offer_scope(variant.variant_key)
    if not sku or not variant_key:
        return None
    return f"variant-buybox:{sku}|{variant_key}"


def _variant_stock_state(variant: CompetitorVariantSnapshot) -> str:
    exact_quantity = (
        variant.stock_quantity
        if variant.stock_exact
        or (variant.stock_quantity is not None and variant.stock_quantity > 0)
        else None
    )
    return competitor_offer_stock_state(
        variant.stock_status,
        is_leadtime=variant.is_leadtime,
        exact_quantity=exact_quantity,
    )


def _variant_offer_mapping(
    snapshot: CompetitorSnapshot,
    variant: CompetitorVariantSnapshot,
) -> dict[str, object]:
    selected = bool(
        _normalized_offer_scope(variant.sku)
        and _normalized_offer_scope(variant.sku) == _normalized_offer_scope(snapshot.sku)
    )
    image_url = variant.image_url
    if image_url is None and variant.variant_key == "default":
        image_url = snapshot.image_url
    return {
        "selected": selected,
        "sku": variant.sku or "",
        "seller_id": variant.seller_id or "",
        "seller_name": variant.seller_name or "未知卖家",
        "price": float(variant.price) if variant.price is not None else None,
        "stock_status": variant.stock_status or "未知",
        "is_buybox": True,
        "is_leadtime": variant.is_leadtime,
        "plid": snapshot.plid,
        "url": variant.url,
        "offer_id": None,
        "condition": None,
        "variant_key": variant.variant_key,
        "variant_label": variant.variant_label,
        "image_url": image_url,
        "identity_key": _variant_offer_identity(variant),
        "buybox_rank": None,
        "is_follower_offer": False,
        "stock_state": _variant_stock_state(variant),
        "stock_quantity": variant.stock_quantity,
        "stock_exact": variant.stock_exact,
        "stock_method": variant.stock_method,
        "stock_note": variant.stock_note,
    }


def _matching_offer_variant(
    offer: Mapping[str, object],
    variants: list[CompetitorVariantSnapshot],
) -> CompetitorVariantSnapshot | None:
    if not (bool(offer.get("is_buybox")) or bool(offer.get("selected"))):
        return None
    candidates = variants
    variant_key = _normalized_offer_scope(offer.get("variant_key"))
    if variant_key and variant_key != "default":
        candidates = [
            variant
            for variant in candidates
            if _normalized_offer_scope(variant.variant_key) == variant_key
        ]
    sku = _normalized_offer_scope(offer.get("sku"))
    if sku:
        candidates = [
            variant
            for variant in candidates
            if _normalized_offer_scope(variant.sku) == sku
        ]
    if len(candidates) != 1 and not sku and variant_key in {"", "default"}:
        offer_price = _offer_price(offer)
        if offer_price is not None:
            candidates = [
                variant
                for variant in variants
                if variant.price is not None and float(variant.price) == offer_price
            ]
    return candidates[0] if len(candidates) == 1 else None


def _snapshot_offers_with_variants(
    snapshot: CompetitorSnapshot,
    variants: list[CompetitorVariantSnapshot],
) -> list[Mapping[str, object]]:
    offers: list[Mapping[str, object]] = []
    matched_variant_ids: set[int] = set()
    for offer in _snapshot_offers(snapshot):
        variant = _matching_offer_variant(offer, variants)
        if variant is None:
            offers.append(offer)
            continue
        matched_variant_ids.add(variant.id)
        variant_mapping = _variant_offer_mapping(snapshot, variant)
        enriched = dict(variant_mapping)
        enriched.update(
            {
                key: value
                for key, value in offer.items()
                if value is not None and value != ""
            }
        )
        enriched["sku"] = str(offer.get("sku") or variant.sku or "")
        enriched["seller_id"] = str(offer.get("seller_id") or variant.seller_id or "")
        seller_name = str(offer.get("seller_name") or "").strip()
        if _normalized_offer_scope(seller_name) in {"", "未知卖家".casefold()}:
            seller_name = str(variant.seller_name or "未知卖家")
        enriched["seller_name"] = seller_name
        enriched["variant_key"] = variant.variant_key
        enriched["variant_label"] = variant.variant_label
        enriched["identity_key"] = (
            _offer_identity_from_mapping(enriched) or _variant_offer_identity(variant)
        )
        for key in (
            "stock_state",
            "stock_quantity",
            "stock_exact",
            "stock_method",
            "stock_note",
        ):
            enriched[key] = variant_mapping[key]
        offers.append(enriched)
    offers.extend(
        _variant_offer_mapping(snapshot, variant)
        for variant in variants
        if variant.id not in matched_variant_ids
    )
    return offers


def _offer_price(offer: Mapping[str, object]) -> float | None:
    value = offer.get("price")
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _offer_stock_quantity(offer: Mapping[str, object]) -> int | None:
    value = offer.get("stock_quantity")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _offer_optional_bool(offer: Mapping[str, object], key: str) -> bool | None:
    value = offer.get(key)
    return value if isinstance(value, bool) else None


def _offer_stock_state(offer: Mapping[str, object]) -> str:
    persisted = str(offer.get("stock_state") or "").strip()
    if persisted in {"有货", "没货", "未知"}:
        return persisted
    quantity = _offer_stock_quantity(offer)
    exact = bool(offer.get("stock_exact"))
    exact_quantity = quantity if exact or (quantity is not None and quantity > 0) else None
    return competitor_offer_stock_state(
        offer.get("stock_status"),
        is_leadtime=bool(offer.get("is_leadtime")),
        is_add_to_cart_available=_offer_optional_bool(
            offer, "is_add_to_cart_available"
        ),
        exact_quantity=exact_quantity,
    )


def _offer_inventory_scope_matches(
    previous: Mapping[str, object],
    latest: Mapping[str, object],
) -> bool:
    keys = ("seller_id", "seller_name", "sku", "variant_key", "condition")
    return all(
        " ".join(str(previous.get(key) or "").casefold().split())
        == " ".join(str(latest.get(key) or "").casefold().split())
        for key in keys
    )


def _offer_inventory_signal(
    *,
    key: str,
    identity: str | None,
    offer: Mapping[str, object],
    previous: Mapping[str, object] | None,
    oldest_id: int,
    latest_id: int,
    oldest_ambiguous: set[str],
    latest_ambiguous: set[str],
    raw_history: bool,
) -> tuple[str | None, int | None, int | None, bool, str]:
    if raw_history:
        return None, None, None, False, "原始库存状态"
    if identity is None or oldest_id == latest_id or previous is None:
        return None, None, None, False, "待建立库存基线"
    start_state = _offer_stock_state(previous)
    start_quantity = _offer_stock_quantity(previous)
    if key in oldest_ambiguous or key in latest_ambiguous:
        return start_state, start_quantity, None, False, "库存不可比"
    if not _offer_inventory_scope_matches(previous, offer):
        return start_state, start_quantity, None, False, "库存不可比"

    latest_state = _offer_stock_state(offer)
    latest_quantity = _offer_stock_quantity(offer)
    exact_pair = (
        bool(previous.get("stock_exact"))
        and bool(offer.get("stock_exact"))
        and start_quantity is not None
        and latest_quantity is not None
    )
    if exact_pair:
        assert latest_quantity is not None
        assert start_quantity is not None
        quantity_change = latest_quantity - start_quantity
        if quantity_change < 0:
            signal = "库存减少"
        elif quantity_change > 0:
            signal = "库存增加"
        else:
            signal = "库存数量不变"
        return start_state, start_quantity, quantity_change, True, signal

    if start_state not in {"有货", "没货"} or latest_state not in {"有货", "没货"}:
        return start_state, start_quantity, None, False, "库存不可比"
    if start_state == "有货" and latest_state == "没货":
        signal = "转为没货"
    elif start_state == "没货" and latest_state == "有货":
        signal = "恢复有货"
    else:
        signal = "库存状态不变"
    return start_state, start_quantity, None, True, signal


def _offer_comparison_signature(offer: Mapping[str, object]) -> tuple[object, ...]:
    return (
        offer.get("seller_id"),
        offer.get("seller_name"),
        offer.get("sku"),
        offer.get("variant_key"),
        offer.get("condition"),
        _offer_price(offer),
        _offer_stock_state(offer),
        _offer_stock_quantity(offer),
        bool(offer.get("stock_exact")),
    )


def _indexed_snapshot_offers(
    row: CompetitorSnapshot,
    variants: list[CompetitorVariantSnapshot] | None = None,
) -> tuple[list[tuple[str, Mapping[str, object]]], set[str]]:
    indexed: dict[str, Mapping[str, object]] = {}
    order: list[str] = []
    ambiguous: set[str] = set()
    for index, offer in enumerate(_snapshot_offers_with_variants(row, variants or [])):
        identity = _offer_identity_from_mapping(offer)
        key = identity or f"unidentified:{row.id}:{index}"
        existing = indexed.get(key)
        if existing is None:
            indexed[key] = offer
            order.append(key)
            continue
        if _offer_comparison_signature(existing) != _offer_comparison_signature(offer):
            ambiguous.add(key)
        if bool(offer.get("selected")) and not bool(existing.get("selected")):
            indexed[key] = offer
    return [(key, indexed[key]) for key in order], ambiguous


def _interval_offer_rows(
    oldest: CompetitorSnapshot,
    latest: CompetitorSnapshot,
    *,
    oldest_variants: list[CompetitorVariantSnapshot] | None = None,
    latest_variants: list[CompetitorVariantSnapshot] | None = None,
    raw_history: bool = False,
) -> list[dict[str, object]]:
    """Compare each seller offer by offer_id, never by the shared product PLID."""

    oldest_items, oldest_ambiguous = _indexed_snapshot_offers(
        oldest,
        oldest_variants,
    )
    latest_items, latest_ambiguous = _indexed_snapshot_offers(
        latest,
        latest_variants,
    )
    oldest_by_key = dict(oldest_items)
    rows: list[dict[str, object]] = []
    for key, offer in latest_items:
        identity = _offer_identity_from_mapping(offer)
        latest_price = _offer_price(offer)
        previous = oldest_by_key.get(key) if identity is not None else None
        start_price = _offer_price(previous) if previous is not None else None
        price_change: float | None = None
        if raw_history:
            price_signal = "原始报价"
        elif identity is None or oldest.id == latest.id or previous is None:
            price_signal = "待建立报价基线"
        elif key in oldest_ambiguous or key in latest_ambiguous:
            price_signal = "报价不可比"
        elif start_price is None or latest_price is None:
            price_signal = "价格不可比"
        else:
            price_change = latest_price - start_price
            if price_change < 0:
                price_signal = "降价"
            elif price_change > 0:
                price_signal = "涨价"
            else:
                price_signal = "价格不变"
        (
            start_stock_state,
            start_stock_quantity,
            stock_quantity_change,
            stock_comparable,
            stock_signal,
        ) = _offer_inventory_signal(
            key=key,
            identity=identity,
            offer=offer,
            previous=previous,
            oldest_id=oldest.id,
            latest_id=latest.id,
            oldest_ambiguous=oldest_ambiguous,
            latest_ambiguous=latest_ambiguous,
            raw_history=raw_history,
        )
        stock_state = _offer_stock_state(offer)
        stock_quantity = _offer_stock_quantity(offer)
        rows.append(
            {
                "报价键": key,
                "报价来源": "public_offer",
                "offer_id": str(offer.get("offer_id") or "").strip() or None,
                "卖家ID": str(offer.get("seller_id") or "").strip() or None,
                "卖家": str(offer.get("seller_name") or "未知卖家"),
                "SKU": str(offer.get("sku") or "").strip() or None,
                "TSIN": str(offer.get("tsin_id") or "").strip() or None,
                "图片": str(offer.get("image_url") or "").strip() or None,
                "价格": latest_price,
                "库存状态": stock_state,
                "库存原始状态": str(offer.get("stock_status") or "未知"),
                "库存数量": stock_quantity,
                "库存精确": bool(offer.get("stock_exact")),
                "库存方式": str(offer.get("stock_method") or "public-offer-status"),
                "库存说明": str(offer.get("stock_note") or "").strip() or None,
                "条件": str(offer.get("condition") or "").strip() or None,
                "变体键": str(offer.get("variant_key") or "default"),
                "变体": str(offer.get("variant_label") or "默认款"),
                "是否主报价": bool(offer.get("selected")),
                "是否变体主报价": bool(offer.get("is_buybox")),
                "是否跟卖": bool(
                    offer.get(
                        "is_follower_offer",
                        not bool(offer.get("is_buybox")),
                    )
                ),
                "plid": str(offer.get("plid") or latest.plid),
                "链接": str(offer.get("url") or latest.url),
                "区间起始价格": start_price,
                "价格变化": price_change,
                "价格信号": price_signal,
                "区间起始库存状态": start_stock_state,
                "区间起始库存数量": start_stock_quantity,
                "库存数量变化": stock_quantity_change,
                "库存可比": stock_comparable,
                "库存信号": stock_signal,
            }
        )
    return rows


def _snapshot_row(
    row: CompetitorSnapshot,
    *,
    stale_stock: CompetitorSnapshot | None = None,
    signal: SalesSignal | None = None,
    signal_start: datetime | None = None,
    signal_end: datetime | None = None,
    interval_snapshot_count: int | None = None,
    stock_change: int | None = None,
    stock_comparable: bool | None = None,
    inventory_turnover: _PeriodInventoryTurnover | None = None,
    recent_observed_sales: dict[str, int | None] | None = None,
    recent_observed_sales_through: date | None = None,
    positive_review_delta: int | None = None,
    negative_review_delta: int | None = None,
    price_start: float | None = None,
    price_change: float | None = None,
    price_signal: str | None = None,
    offer_rows: list[dict[str, object]] | None = None,
    follower_timeline: dict[str, object] | None = None,
    first_monitored_at: datetime | None = None,
    latest_review_count: int | None = None,
    latest_review_collected_at: datetime | None = None,
    raw_history: bool = False,
) -> dict[str, object]:
    stock_text = _stock_text(row)
    inventory_turnover = inventory_turnover or _PeriodInventoryTurnover()
    (
        follow_opportunity,
        follow_opportunity_type,
        follow_opportunity_note,
        public_offer_count,
    ) = _follow_selling_opportunity(row)
    if raw_history:
        observed_stock_outflow = None
        review_delta = None
        period_sales_min = None
        period_sales_max = None
        trend_label = "原始快照"
        trend_note = "历史快照只展示当时原始值；经营信号按所选区间首尾统一重算。"
        price_signal = "原始快照"
    elif signal is not None:
        observed_stock_outflow = signal.observed_stock_outflow
        review_delta = signal.review_delta
        period_sales_min = signal.period_sales_min
        period_sales_max = signal.period_sales_max
        trend_label = signal.trend_label
        trend_note = signal.trend_note
    else:
        observed_stock_outflow = row.observed_stock_outflow
        review_delta = row.review_delta
        period_sales_min = row.period_sales_min
        period_sales_max = row.period_sales_max
        trend_label = row.trend_label
        trend_note = row.trend_note
    period_range = "待积累"
    if period_sales_min is not None and period_sales_max is not None:
        period_range = (
            str(period_sales_min)
            if period_sales_min == period_sales_max
            else f"{period_sales_min}–{period_sales_max}"
        )
    follower_timeline = follower_timeline or {}
    return {
        "来源": "competitor",
        "快照ID": row.id,
        "plid": row.plid,
        "商品": row.title,
        "图片": row.image_url,
        "采集时间": row.collected_at,
        "当前卖家": row.seller_name,
        "价格": float(row.price) if row.price is not None else None,
        "区间起始价格": price_start,
        "价格变化": price_change,
        "价格信号": price_signal or "待建立价格基线",
        "库存上限": stock_text,
        "库存数量": row.stock_quantity,
        "库存精确": row.stock_exact,
        "库存说明": row.stock_note,
        "库存参考过期": stale_stock is not None,
        "上次成功库存": _stock_text(stale_stock) if stale_stock is not None else None,
        "上次成功库存数量": (stale_stock.stock_quantity if stale_stock is not None else None),
        "上次成功库存精确": (stale_stock.stock_exact if stale_stock is not None else False),
        "上次成功库存时间": (stale_stock.collected_at if stale_stock is not None else None),
        "首次监控时间": first_monitored_at,
        "评论数": row.review_count,
        "评论数可用": True,
        "最新评论数": latest_review_count,
        "最新评论获取时间": latest_review_collected_at,
        "评分": float(row.rating) if row.rating is not None else None,
        "好评": row.positive_reviews,
        "中评": row.neutral_reviews,
        "差评": row.negative_reviews,
        "观察期销量信号": period_range,
        "观察期估算下限": period_sales_min,
        "观察期估算上限": period_sales_max,
        "库存净变化": stock_change,
        "库存净流入": max(0, stock_change) if stock_change is not None else None,
        "库存净流出": observed_stock_outflow,
        "周期销售件数": inventory_turnover.sales_units,
        "周期销售额": inventory_turnover.sales_amount,
        "周期补货量": inventory_turnover.replenishment_units,
        "周期补货货值": inventory_turnover.replenishment_value,
        "周期库存周转金额": inventory_turnover.turnover_value,
        **(
            {
                "近期观察售出": recent_observed_sales,
                "近期观察售出截至": recent_observed_sales_through,
            }
            if recent_observed_sales is not None
            else {}
        ),
        "新增评论": review_delta,
        "新增好评": positive_review_delta,
        "新增差评": negative_review_delta,
        "趋势判断": trend_label,
        "判断说明": trend_note,
        "信号区间开始": signal_start,
        "信号区间结束": signal_end,
        "区间快照数": interval_snapshot_count,
        "库存可比": stock_comparable,
        "链接": row.url,
        "跟卖机会": follow_opportunity,
        "跟卖机会类型": follow_opportunity_type,
        "跟卖机会说明": follow_opportunity_note,
        "公开报价数": public_offer_count,
        "跟卖报价": offer_rows or [],
        "对比报价": offer_rows or [],
        "自有报价": [],
        "共享评论说明": None,
        "跟卖发现日期": list(
            cast(list[str], follower_timeline.get("跟卖发现日期", []))
        ),
        "新增跟卖卖家数": int(
            cast(int, follower_timeline.get("新增跟卖卖家数", 0))
        ),
        "新增跟卖卖家": list(
            cast(list[str], follower_timeline.get("新增跟卖卖家", []))
        ),
        "跟卖卖家明细": list(
            cast(
                list[dict[str, object]],
                follower_timeline.get("跟卖卖家明细", []),
            )
        ),
    }


def _latest_store_baselines(
    baselines: list[StoreOfferPoint],
) -> list[StoreOfferPoint]:
    latest: dict[tuple[str, str], StoreOfferPoint] = {}
    for row in sorted(
        baselines,
        key=lambda item: (item.display_date, item.captured_at, item.id),
    ):
        latest[(row.store_code, row.offer_id)] = row
    return sorted(latest.values(), key=lambda row: (row.store_code, row.offer_id))


def _store_offer_inventory_turnover_observations(
    history: list[StoreOfferPoint],
) -> list[_InventoryTurnoverObservation]:
    distinct_points: dict[tuple[date, datetime], StoreOfferPoint] = {}
    for row in sorted(history, key=lambda item: (item.display_date, item.captured_at, item.id)):
        distinct_points[(row.display_date, row.captured_at)] = row
    return [
        _InventoryTurnoverObservation(
            scope=(row.store_code, row.offer_id, row.productline_id, row.sku),
            stock_quantity=row.total_stock,
            stock_exact=row.total_stock is not None,
            price=(
                Decimal(str(row.selling_price))
                if row.selling_price is not None
                else None
            ),
            display_date=row.display_date,
        )
        for row in distinct_points.values()
    ]


def _store_offer_period_inventory_turnover(
    history: list[StoreOfferPoint],
) -> _PeriodInventoryTurnover:
    return _period_inventory_turnover(
        _store_offer_inventory_turnover_observations(history)
    )


def _store_period_inventory_turnover(
    baselines: list[StoreOfferPoint],
) -> _PeriodInventoryTurnover:
    by_identity: dict[tuple[str, str], list[StoreOfferPoint]] = {}
    for row in baselines:
        by_identity.setdefault((row.store_code, row.offer_id), []).append(row)
    turnovers = [
        _store_offer_period_inventory_turnover(history)
        for history in by_identity.values()
    ]
    if (
        not turnovers
        or any(
            item.sales_units is None or item.replenishment_units is None
            for item in turnovers
        )
    ):
        return _PeriodInventoryTurnover()

    sales_amount = (
        sum(item.sales_amount for item in turnovers if item.sales_amount is not None)
        if all(item.sales_amount is not None for item in turnovers)
        else None
    )
    replenishment_value = (
        sum(
            item.replenishment_value
            for item in turnovers
            if item.replenishment_value is not None
        )
        if all(item.replenishment_value is not None for item in turnovers)
        else None
    )
    return _PeriodInventoryTurnover(
        sales_units=sum(int(item.sales_units or 0) for item in turnovers),
        sales_amount=sales_amount,
        replenishment_units=sum(int(item.replenishment_units or 0) for item in turnovers),
        replenishment_value=replenishment_value,
        turnover_value=(
            sales_amount + replenishment_value
            if sales_amount is not None and replenishment_value is not None
            else None
        ),
    )


def _store_recent_observed_sales_units(
    baselines: list[StoreOfferPoint],
) -> tuple[dict[str, int | None], date | None]:
    by_identity: dict[tuple[str, str], list[StoreOfferPoint]] = {}
    for row in baselines:
        by_identity.setdefault((row.store_code, row.offer_id), []).append(row)
    observations = [
        observation
        for history in by_identity.values()
        for observation in _store_offer_inventory_turnover_observations(history)
    ]
    return _recent_observed_sales_units(observations)


def _seller_api_offer_rows(
    baselines: list[StoreOfferPoint],
    *,
    store_names_by_code: dict[str, str],
    raw_history: bool = False,
) -> list[dict[str, object]]:
    """Project every Seller API refresh into the same quote shape as public sellers."""
    by_identity: dict[tuple[str, str], list[StoreOfferPoint]] = {}
    for row in baselines:
        by_identity.setdefault((row.store_code, row.offer_id), []).append(row)

    rows: list[dict[str, object]] = []
    for (store_code, offer_id), history in sorted(by_identity.items()):
        history.sort(key=lambda row: (row.display_date, row.captured_at, row.id))
        oldest = history[0]
        latest = history[-1]
        has_interval_comparison = len(history) > 1
        oldest_price = (
            float(oldest.selling_price) if oldest.selling_price is not None else None
        )
        latest_price = (
            float(latest.selling_price) if latest.selling_price is not None else None
        )
        price_change = (
            latest_price - oldest_price
            if has_interval_comparison
            and oldest_price is not None
            and latest_price is not None
            else None
        )
        if raw_history or not has_interval_comparison:
            price_signal = "Seller API刷新"
        elif price_change is None:
            price_signal = "价格不可比"
        elif price_change < 0:
            price_signal = "降价"
        elif price_change > 0:
            price_signal = "涨价"
        else:
            price_signal = "价格不变"

        stock_comparable = (
            has_interval_comparison
            and oldest.total_stock is not None
            and latest.total_stock is not None
        )
        stock_change = (
            latest.total_stock - oldest.total_stock
            if stock_comparable
            and latest.total_stock is not None
            and oldest.total_stock is not None
            else None
        )
        if raw_history or not has_interval_comparison:
            stock_signal = "Seller API刷新"
        elif stock_change is None:
            stock_signal = "库存不可比"
        elif stock_change < 0:
            stock_signal = "库存减少"
        elif stock_change > 0:
            stock_signal = "库存增加"
        else:
            stock_signal = "库存数量不变"
        stock_state = (
            "未知"
            if latest.total_stock is None
            else "有货"
            if latest.total_stock > 0
            else "没货"
        )
        store_name = store_names_by_code.get(store_code, store_code)
        stock_note = (
            "Seller API最新刷新："
            f"总库存 {latest.total_stock if latest.total_stock is not None else '—'}，"
            "Takealot可售 "
            f"{latest.takealot_available_stock if latest.takealot_available_stock is not None else '—'}，"
            "卖家可售 "
            f"{latest.seller_available_stock if latest.seller_available_stock is not None else '—'}。"
        )
        rows.append(
            {
                "报价键": f"seller-api:{store_code}:{offer_id}",
                "报价来源": "seller_api",
                "offer_id": offer_id,
                "卖家ID": store_code,
                "卖家": store_name,
                "SKU": latest.sku,
                "图片": latest.image_url,
                "价格": latest_price,
                "库存状态": stock_state,
                "库存原始状态": latest.status or "未知",
                "库存数量": latest.total_stock,
                "库存精确": latest.total_stock is not None,
                "库存方式": "seller-api-refresh",
                "库存说明": stock_note,
                "条件": "自有 Offer",
                "变体键": f"seller-api:{store_code}:{offer_id}",
                "变体": f"SKU {latest.sku}" if latest.sku else f"Offer {offer_id}",
                "是否主报价": False,
                "是否变体主报价": False,
                "plid": str(latest.productline_id or ""),
                "链接": f"https://www.takealot.com/p/PLID{latest.productline_id}",
                "区间起始价格": oldest_price if has_interval_comparison else None,
                "价格变化": price_change,
                "价格信号": price_signal,
                "区间起始库存状态": (
                    "有货" if (oldest.total_stock or 0) > 0 else "没货"
                    if oldest.total_stock is not None
                    else None
                ),
                "区间起始库存数量": (
                    oldest.total_stock if has_interval_comparison else None
                ),
                "库存数量变化": stock_change,
                "库存可比": stock_comparable,
                "库存信号": stock_signal,
                "店铺": store_name,
                "Takealot可售库存": latest.takealot_available_stock,
                "卖家可售库存": latest.seller_available_stock,
            }
        )
    return rows


def _public_offer_is_own(
    offer: Mapping[str, object],
    *,
    own_offer_ids: set[str],
    own_skus: set[str],
) -> bool:
    offer_id = _normalized_offer_scope(offer.get("offer_id"))
    sku = _normalized_offer_scope(offer.get("SKU") or offer.get("sku"))
    own_scopes = own_offer_ids | own_skus
    return bool((offer_id and offer_id in own_scopes) or (sku and sku in own_scopes))


def _store_follower_offer_rows(
    oldest: CompetitorSnapshot,
    latest: CompetitorSnapshot,
    *,
    own_offer_ids: set[str],
    own_skus: set[str],
    raw_history: bool = False,
) -> list[dict[str, object]]:
    """Keep every non-own public seller, including a competitor in the Buy Box."""
    return [
        {**offer, "是否跟卖": True}
        for offer in _interval_offer_rows(
            oldest,
            latest,
            raw_history=raw_history,
        )
        if not _public_offer_is_own(
            offer,
            own_offer_ids=own_offer_ids,
            own_skus=own_skus,
        )
    ]


def _follower_seller_identity(
    offer: Mapping[str, object],
) -> tuple[str, str, str | None] | None:
    seller_name = str(offer.get("卖家") or "未知卖家").strip() or "未知卖家"
    normalized_name = " ".join(seller_name.casefold().split())
    seller_id = str(offer.get("卖家ID") or "").strip() or None
    known_name = (
        normalized_name not in {"", "未知卖家", "unknown seller"}
        and not normalized_name.startswith("卖家id ")
    )
    if known_name:
        return f"name:{normalized_name}", seller_name, seller_id
    if seller_id:
        return f"id:{seller_id.casefold()}", seller_name, seller_id
    offer_id = str(offer.get("offer_id") or "").strip()
    if offer_id:
        return f"offer:{offer_id.casefold()}", seller_name, None
    return None


def _follower_seller_timelines(
    snapshots: list[CompetitorSnapshot],
    *,
    selected_start_date: date | None,
    selected_end_date: date | None,
    own_offer_ids_by_plid: dict[str, set[str]] | None = None,
    own_skus_by_plid: dict[str, set[str]] | None = None,
    not_before_by_plid: dict[str, date] | None = None,
) -> dict[str, dict[str, object]]:
    """Rebuild first-observed seller events from immutable public snapshots."""
    first_seen: dict[tuple[str, str], date] = {}
    selected: dict[str, dict[str, dict[str, object]]] = {}
    own_store_mode = own_offer_ids_by_plid is not None or own_skus_by_plid is not None
    own_offer_ids_by_plid = own_offer_ids_by_plid or {}
    own_skus_by_plid = own_skus_by_plid or {}
    not_before_by_plid = not_before_by_plid or {}

    for snapshot in sorted(snapshots, key=lambda row: (row.collected_at, row.id)):
        observed_date = _competitor_display_date(snapshot.collected_at)
        not_before = not_before_by_plid.get(snapshot.plid)
        if not_before is not None and observed_date < not_before:
            continue
        if own_store_mode:
            offers = _store_follower_offer_rows(
                snapshot,
                snapshot,
                own_offer_ids=own_offer_ids_by_plid.get(snapshot.plid, set()),
                own_skus=own_skus_by_plid.get(snapshot.plid, set()),
                raw_history=True,
            )
        else:
            offers = [
                offer
                for offer in _interval_offer_rows(snapshot, snapshot, raw_history=True)
                if bool(offer.get("是否跟卖"))
            ]
        sellers_in_snapshot: dict[str, tuple[str, str | None]] = {}
        for offer in offers:
            identity = _follower_seller_identity(offer)
            if identity is None:
                continue
            key, seller_name, seller_id = identity
            sellers_in_snapshot.setdefault(key, (seller_name, seller_id))
            first_seen.setdefault((snapshot.plid, key), observed_date)

        in_selected_range = (
            (selected_start_date is None or observed_date >= selected_start_date)
            and (selected_end_date is None or observed_date <= selected_end_date)
        )
        if not in_selected_range:
            continue
        for key, (seller_name, seller_id) in sellers_in_snapshot.items():
            details_by_seller = selected.setdefault(snapshot.plid, {})
            detail = details_by_seller.setdefault(
                key,
                {
                    "卖家ID": seller_id,
                    "卖家": seller_name,
                    "首次发现日期": first_seen[(snapshot.plid, key)].isoformat(),
                    "区间发现日期": set(),
                    "区间观察次数": 0,
                },
            )
            dates = detail["区间发现日期"]
            assert isinstance(dates, set)
            dates.add(observed_date.isoformat())
            detail["区间观察次数"] = int(str(detail["区间观察次数"])) + 1

    result: dict[str, dict[str, object]] = {}
    for plid, seller_details in selected.items():
        details: list[dict[str, object]] = []
        observed_dates: set[str] = set()
        new_seller_names: list[str] = []
        for key, raw_detail in seller_details.items():
            raw_dates = raw_detail["区间发现日期"]
            assert isinstance(raw_dates, set)
            dates = sorted(cast(set[str], raw_dates))
            first_date = first_seen[(plid, key)]
            is_new = (
                (selected_start_date is None or first_date >= selected_start_date)
                and (selected_end_date is None or first_date <= selected_end_date)
            )
            detail = {
                **raw_detail,
                "区间发现日期": dates,
                "是否区间新增": is_new,
            }
            details.append(detail)
            observed_dates.update(dates)
            if is_new:
                new_seller_names.append(str(raw_detail["卖家"]))
        details.sort(key=lambda item: (str(item["首次发现日期"]), str(item["卖家"])))
        result[plid] = {
            "跟卖发现日期": sorted(observed_dates),
            "新增跟卖卖家数": len(new_seller_names),
            "新增跟卖卖家": sorted(set(new_seller_names)),
            "跟卖卖家明细": details,
        }
    return result


def _own_follower_event_rows(
    connected_offers: list[ConnectedStoreOffer],
    timelines: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    offers_by_plid: dict[str, list[ConnectedStoreOffer]] = {}
    for item in connected_offers:
        plid = str(item.offer.productline_id or "").strip()
        if plid:
            offers_by_plid.setdefault(plid, []).append(item)

    events: list[dict[str, object]] = []
    for plid, timeline in timelines.items():
        dates = list(cast(list[str], timeline.get("跟卖发现日期", [])))
        if not dates:
            continue
        own_offers = offers_by_plid.get(plid, [])
        representative = own_offers[0] if own_offers else None
        events.append(
            {
                "plid": plid,
                "链接": f"https://www.takealot.com/p/PLID{plid}",
                "商品": (
                    representative.offer.title
                    if representative is not None and representative.offer.title
                    else f"PLID{plid}"
                ),
                "图片": representative.offer.image_url if representative is not None else None,
                "店铺": sorted({item.store_name for item in own_offers}),
                **timeline,
            }
        )
    events.sort(
        key=lambda item: (
            str(cast(list[str], item["跟卖发现日期"])[-1]),
            str(item["plid"]),
        ),
        reverse=True,
    )
    return events


def _store_snapshot_rows(
    baselines: list[StoreOfferPoint],
    follower_snapshots: list[CompetitorSnapshot],
    *,
    all_follower_snapshots: list[CompetitorSnapshot],
    current_store_offers: list[ConnectedStoreOffer],
    selected_start_date: date | None,
    selected_end_date: date | None,
    store_names_by_code: dict[str, str],
    own_offer_ids_by_plid: dict[str, set[str]],
    own_skus_by_plid: dict[str, set[str]],
    follower_timelines: dict[str, dict[str, object]],
    store_tsin_by_offer: dict[tuple[str, str], str],
) -> list[dict[str, object]]:
    """Build own-store cards from every Seller API refresh plus follower offers."""
    current_offers_by_plid: dict[str, list[ConnectedStoreOffer]] = {}
    for current_offer in current_store_offers:
        plid = str(current_offer.offer.productline_id or "").strip()
        if plid:
            current_offers_by_plid.setdefault(plid, []).append(current_offer)

    all_baselines_by_plid: dict[str, list[StoreOfferPoint]] = {}
    for baseline_row in baselines:
        plid = str(baseline_row.productline_id or "").strip()
        if plid:
            all_baselines_by_plid.setdefault(plid, []).append(baseline_row)

    selected_baselines = [
        row
        for row in baselines
        if (selected_start_date is None or row.display_date >= selected_start_date)
        and (selected_end_date is None or row.display_date <= selected_end_date)
        and str(row.productline_id or "").strip()
    ]
    baselines_by_plid: dict[str, list[StoreOfferPoint]] = {}
    for baseline_row in selected_baselines:
        baselines_by_plid.setdefault(str(baseline_row.productline_id), []).append(
            baseline_row
        )

    followers_by_plid: dict[str, list[CompetitorSnapshot]] = {}
    for follower_snapshot in follower_snapshots:
        followers_by_plid.setdefault(follower_snapshot.plid, []).append(
            follower_snapshot
        )

    all_followers_by_plid: dict[str, list[CompetitorSnapshot]] = {}
    for follower_snapshot in all_follower_snapshots:
        all_followers_by_plid.setdefault(follower_snapshot.plid, []).append(
            follower_snapshot
        )

    result: list[dict[str, object]] = []
    for plid, plid_baselines in baselines_by_plid.items():
        current_offers = current_offers_by_plid.get(plid, [])
        current_offer_by_identity = {
            (item.store_code, str(item.offer.offer_id)): item.offer
            for item in current_offers
        }
        current_statuses = _ordered_current_offer_statuses(current_offers)
        current_status_updated_at = max(
            (item.offer.captured_at for item in current_offers),
            default=None,
        )
        own_offers = _latest_store_baselines(plid_baselines)
        own_offer_rows = _seller_api_offer_rows(
            plid_baselines,
            store_names_by_code=store_names_by_code,
        )
        for own_offer_row in own_offer_rows:
            current_offer_state = current_offer_by_identity.get(
                (
                    str(own_offer_row["卖家ID"]),
                    str(own_offer_row["offer_id"]),
                )
            )
            current_status = (
                str(current_offer_state.status or "").strip()
                if current_offer_state is not None
                else ""
            )
            own_offer_row["最新Offer状态"] = current_status or None
            own_offer_row["最新Offer状态更新时间"] = (
                current_offer_state.captured_at
                if current_offer_state is not None
                else None
            )
            current_stock = (
                current_offer_state.total_stock
                if current_offer_state is not None
                else None
            )
            own_offer_row["最新Offer库存数量"] = current_stock
            own_offer_row["最新Offer库存状态"] = (
                "未探测"
                if current_stock is None
                else "有货"
                if current_stock > 0
                else "没货"
            )
            own_offer_row["TSIN"] = store_tsin_by_offer.get(
                (
                    str(own_offer_row["卖家ID"]),
                    str(own_offer_row["offer_id"]),
                )
            )
        representative = next(
            (row for row in own_offers if row.selling_price is not None),
            own_offers[0],
        )
        prices = [float(row.selling_price) for row in own_offers if row.selling_price is not None]
        stock_values = [row.total_stock for row in own_offers]
        stock_exact = bool(stock_values) and all(value is not None for value in stock_values)
        total_stock = (
            sum(value for value in stock_values if value is not None)
            if stock_exact
            else None
        )
        priced_offer_rows = [row for row in own_offer_rows if row["价格"] is not None]
        price_comparable = bool(priced_offer_rows) and all(
            row["区间起始价格"] is not None for row in priced_offer_rows
        )
        start_price = (
            min(float(str(row["区间起始价格"])) for row in priced_offer_rows)
            if price_comparable
            else None
        )
        current_price = min(prices) if prices else None
        aggregate_price_change = (
            current_price - start_price
            if current_price is not None and start_price is not None
            else None
        )
        if aggregate_price_change is None:
            aggregate_price_signal = (
                "待建立价格基线"
                if not any(row["区间起始价格"] is not None for row in priced_offer_rows)
                else "价格不可比"
            )
        elif aggregate_price_change < 0:
            aggregate_price_signal = "降价"
        elif aggregate_price_change > 0:
            aggregate_price_signal = "涨价"
        else:
            aggregate_price_signal = "价格不变"

        aggregate_stock_comparable = bool(own_offer_rows) and all(
            bool(row["库存可比"]) for row in own_offer_rows
        )
        aggregate_stock_change = (
            sum(int(str(row["库存数量变化"])) for row in own_offer_rows)
            if aggregate_stock_comparable
            else None
        )
        inventory_turnover = _store_period_inventory_turnover(plid_baselines)
        recent_observed_sales, recent_observed_sales_through = (
            _store_recent_observed_sales_units(all_baselines_by_plid[plid])
        )

        observations = followers_by_plid.get(plid, [])
        observations.sort(key=lambda row: row.collected_at, reverse=True)
        latest_observation = observations[0] if observations else None
        oldest_observation = observations[-1] if observations else None
        all_observations = all_followers_by_plid.get(plid, [])
        latest_review_observation = max(
            all_observations,
            key=lambda row: (row.collected_at, row.id),
            default=None,
        )
        first_monitored_at = min(
            [
                *(row.captured_at for row in all_baselines_by_plid[plid]),
                *(row.collected_at for row in all_observations),
            ]
        )
        follower_rows = (
            _store_follower_offer_rows(
                oldest_observation,
                latest_observation,
                own_offer_ids=own_offer_ids_by_plid.get(plid, set()),
                own_skus=own_skus_by_plid.get(plid, set()),
            )
            if latest_observation is not None and oldest_observation is not None
            else []
        )
        has_followers = bool(follower_rows)
        follower_timeline = follower_timelines.get(plid, {})
        captured_at = max(row.captured_at for row in own_offers)
        public_collected_at = (
            latest_observation.collected_at if latest_observation is not None else None
        )
        if latest_observation is not None:
            assert latest_observation is not None
            assert oldest_observation is not None
            review_count = latest_observation.review_count
            positive_reviews = latest_observation.positive_reviews
            neutral_reviews = latest_observation.neutral_reviews
            negative_reviews = latest_observation.negative_reviews
            positive_review_delta, negative_review_delta = _interval_review_category_deltas(
                oldest_observation,
                latest_observation,
            )
        else:
            review_count = 0
            positive_reviews = 0
            neutral_reviews = 0
            negative_reviews = 0
            positive_review_delta = None
            negative_review_delta = None
        result.append(
            {
                "来源": "own_store",
                "快照ID": -representative.id,
                "plid": plid,
                "商品": (
                    latest_observation.title
                    if latest_observation is not None and latest_observation.title
                    else representative.title or f"PLID{plid}"
                ),
                "图片": (
                    latest_observation.image_url
                    if latest_observation is not None and latest_observation.image_url
                    else representative.image_url
                ),
                "采集时间": captured_at,
                "当前卖家": "自有店铺（Seller API）",
                "价格": current_price,
                "区间起始价格": start_price,
                "价格变化": aggregate_price_change,
                "价格信号": aggregate_price_signal,
                "库存上限": str(total_stock) if total_stock is not None else "接口未提供",
                "库存数量": total_stock,
                "库存精确": stock_exact,
                "库存说明": "Seller API 最近一次完整刷新，未执行公开页主报价库存探测。",
                "库存参考过期": False,
                "上次成功库存": None,
                "上次成功库存数量": None,
                "上次成功库存精确": False,
                "上次成功库存时间": None,
                "首次监控时间": first_monitored_at,
                "评论数": review_count,
                "评论数可用": latest_observation is not None,
                "最新评论数": (
                    latest_review_observation.review_count
                    if latest_review_observation is not None
                    else None
                ),
                "最新评论获取时间": (
                    latest_review_observation.collected_at
                    if latest_review_observation is not None
                    else None
                ),
                "评分": (
                    float(latest_observation.rating)
                    if latest_observation is not None
                    and latest_observation.rating is not None
                    else None
                ),
                "好评": positive_reviews,
                "中评": neutral_reviews,
                "差评": negative_reviews,
                "观察期销量信号": "只看跟卖报价",
                "观察期估算下限": None,
                "观察期估算上限": None,
                "库存净变化": aggregate_stock_change,
                "库存净流入": (
                    max(0, aggregate_stock_change)
                    if aggregate_stock_change is not None
                    else None
                ),
                "库存净流出": (
                    max(0, -aggregate_stock_change)
                    if aggregate_stock_change is not None
                    else None
                ),
                "周期销售件数": inventory_turnover.sales_units,
                "周期销售额": inventory_turnover.sales_amount,
                "周期补货量": inventory_turnover.replenishment_units,
                "周期补货货值": inventory_turnover.replenishment_value,
                "周期库存周转金额": inventory_turnover.turnover_value,
                "近期观察售出": recent_observed_sales,
                "近期观察售出截至": recent_observed_sales_through,
                "新增评论": (
                    max(0, latest_observation.review_count - oldest_observation.review_count)
                    if latest_observation is not None
                    and oldest_observation is not None
                    and latest_observation.id != oldest_observation.id
                    else None
                ),
                "新增好评": positive_review_delta,
                "新增差评": negative_review_delta,
                "趋势判断": (
                    "跟卖监控中"
                    if has_followers
                    else "暂未发现跟卖"
                    if latest_observation is not None
                    else "等待首次检查"
                ),
                "判断说明": (
                    f"已记录 {len(follower_rows)} 个跟卖报价；自有链接使用 Seller API 每次完整刷新。"
                    if has_followers
                    else "已检查公开商品全部报价并同步PLID共用评论，排除六店自有Offer后未发现其他卖家。"
                    if latest_observation is not None
                    else "已自动纳入跟卖目标，等待后台轮巡首次检查公开商品数据。"
                ),
                "信号区间开始": (
                    oldest_observation.collected_at if oldest_observation is not None else None
                ),
                "信号区间结束": public_collected_at,
                "区间快照数": len(observations),
                "库存可比": aggregate_stock_comparable,
                "链接": f"https://www.takealot.com/p/PLID{plid}",
                "跟卖报价": follower_rows,
                "对比报价": [*own_offer_rows, *follower_rows],
                # This projection deliberately comes from OfferCurrent and must not
                # change when the operator changes the historical observation range.
                "最新Offer状态": current_statuses,
                "最新Offer状态更新时间": current_status_updated_at,
                "自有报价": [
                    {
                        "offer_id": row.offer_id,
                        "店铺": store_names_by_code.get(row.store_code, row.store_code),
                        "SKU": row.sku,
                        "价格": float(row.selling_price) if row.selling_price is not None else None,
                        "库存": row.total_stock,
                        "Takealot可售库存": row.takealot_available_stock,
                        "卖家可售库存": row.seller_available_stock,
                        "状态": row.status,
                        "基准日": row.display_date,
                        "拉取时间": row.captured_at,
                    }
                    for row in own_offers
                ],
                "共享评论说明": (
                    "Takealot 评论属于整个 PLID 商品，不能归属到某个跟卖卖家；"
                    "私有链接首次检查或评论数变化时单独同步，并作为商品共享信号展示。"
                    if latest_observation is not None
                    else "该私有链接等待首次公开页检查，尚未同步PLID商品评论。"
                ),
                "跟卖发现日期": list(
                    cast(list[str], follower_timeline.get("跟卖发现日期", []))
                ),
                "新增跟卖卖家数": int(
                    cast(int, follower_timeline.get("新增跟卖卖家数", 0))
                ),
                "新增跟卖卖家": list(
                    cast(list[str], follower_timeline.get("新增跟卖卖家", []))
                ),
                "跟卖卖家明细": list(
                    cast(
                        list[dict[str, object]],
                        follower_timeline.get("跟卖卖家明细", []),
                    )
                ),
            }
        )
    result.sort(key=lambda item: (str(item["趋势判断"]), str(item["商品"])))
    return result


def _ordered_current_offer_statuses(
    offers: list[ConnectedStoreOffer],
) -> list[str]:
    """Return unique current Seller Offers statuses in the UI's canonical order."""
    canonical_order = {
        "not_buyable": 0,
        "buyable": 1,
        "disabled_by_takealot": 2,
        "disabled_by_seller": 3,
    }
    statuses = {
        str(item.offer.status or "").strip()
        for item in offers
        if str(item.offer.status or "").strip()
    }
    return sorted(
        statuses,
        key=lambda status: (canonical_order.get(status, len(canonical_order)), status),
    )


def _store_baseline_history_row(
    plid: str,
    baselines: list[StoreOfferPoint],
    *,
    store_names_by_code: dict[str, str],
) -> dict[str, object]:
    latest = max(baselines, key=lambda row: (row.captured_at, row.id))
    own_rows = _seller_api_offer_rows(
        baselines,
        store_names_by_code=store_names_by_code,
        raw_history=True,
    )
    return {
        "来源": "own_store",
        "快照ID": -latest.id,
        "plid": plid,
        "商品": latest.title or f"PLID{plid}",
        "图片": latest.image_url,
        "采集时间": latest.captured_at,
        "当前卖家": "自有店铺（Seller API）",
        "价格": (
            float(latest.selling_price) if latest.selling_price is not None else None
        ),
        "区间起始价格": None,
        "价格变化": None,
        "价格信号": "Seller API刷新",
        "库存上限": (
            str(latest.total_stock) if latest.total_stock is not None else "接口未提供"
        ),
        "库存数量": latest.total_stock,
        "库存精确": latest.total_stock is not None,
        "库存说明": "Seller API完整刷新历史点。",
        "库存参考过期": False,
        "上次成功库存": None,
        "上次成功库存数量": None,
        "上次成功库存精确": False,
        "上次成功库存时间": None,
        "首次监控时间": None,
        "评论数": 0,
        "评论数可用": False,
        "最新评论数": None,
        "最新评论获取时间": None,
        "评分": None,
        "好评": 0,
        "中评": 0,
        "差评": 0,
        "观察期销量信号": "Seller API历史",
        "观察期估算下限": None,
        "观察期估算上限": None,
        "库存净变化": None,
        "库存净流入": None,
        "库存净流出": None,
        "周期销售件数": None,
        "周期销售额": None,
        "周期补货量": None,
        "周期补货货值": None,
        "周期库存周转金额": None,
        "新增评论": None,
        "新增好评": None,
        "新增差评": None,
        "趋势判断": "Seller API刷新",
        "判断说明": "该时间点只包含Seller API报价与库存，评论曲线保持断点。",
        "信号区间开始": None,
        "信号区间结束": None,
        "区间快照数": None,
        "库存可比": None,
        "链接": f"https://www.takealot.com/p/PLID{plid}",
        "跟卖报价": [],
        "对比报价": own_rows,
        "自有报价": [],
        "共享评论说明": "该Seller API时间点未同时采集公开评论，评论曲线保持断点。",
        "跟卖发现日期": [],
        "新增跟卖卖家数": 0,
        "新增跟卖卖家": [],
        "跟卖卖家明细": [],
    }


def _store_history_rows(
    baselines: list[StoreOfferPoint],
    follower_snapshots: list[CompetitorSnapshot],
    *,
    selected_start_date: date | None,
    selected_end_date: date | None,
    store_names_by_code: dict[str, str],
    own_offer_ids_by_plid: dict[str, set[str]],
    own_skus_by_plid: dict[str, set[str]],
) -> list[dict[str, object]]:
    """Return separate real Seller API and public-observation history points."""
    baseline_groups: dict[tuple[str, datetime], list[StoreOfferPoint]] = {}
    for row in baselines:
        plid = str(row.productline_id or "").strip()
        if not plid:
            continue
        if selected_start_date is not None and row.display_date < selected_start_date:
            continue
        if selected_end_date is not None and row.display_date > selected_end_date:
            continue
        baseline_groups.setdefault((plid, row.captured_at), []).append(row)

    result = [
        _store_baseline_history_row(
            plid,
            rows,
            store_names_by_code=store_names_by_code,
        )
        for (plid, _), rows in baseline_groups.items()
    ]
    for snapshot in follower_snapshots:
        follower_rows = _store_follower_offer_rows(
            snapshot,
            snapshot,
            own_offer_ids=own_offer_ids_by_plid.get(snapshot.plid, set()),
            own_skus=own_skus_by_plid.get(snapshot.plid, set()),
            raw_history=True,
        )
        history_row = _snapshot_row(
            snapshot,
            offer_rows=follower_rows,
            raw_history=True,
        )
        history_row["来源"] = "own_store"
        history_row["跟卖报价"] = follower_rows
        history_row["对比报价"] = follower_rows
        history_row["自有报价"] = []
        result.append(history_row)
    result.sort(key=lambda item: str(item["采集时间"]), reverse=True)
    return result


def _variant_row(
    row: CompetitorVariantSnapshot,
    *,
    default_image_url: str | None = None,
) -> dict[str, object]:
    stock_text = "未探测"
    if row.stock_method in {"not-platform-stock", "out-of-stock"}:
        stock_text = "没货"
    elif row.stock_quantity is not None:
        stock_text = str(row.stock_quantity) if row.stock_exact else f"至少{row.stock_quantity}"
    display_label = _display_variant_label(row.variant_label)
    image_url = row.image_url
    if image_url is None and (
        row.variant_key == "default" or display_label in {"默认款", "默认变体"}
    ):
        image_url = default_image_url
    return {
        "plid": row.plid,
        "快照ID": row.snapshot_id,
        "图片": image_url,
        "采集时间": row.collected_at,
        "变体键": row.variant_key,
        "变体": display_label,
        "SKU": row.sku,
        "卖家": row.seller_name,
        "价格": float(row.price) if row.price is not None else None,
        "库存": stock_text,
        "库存数量": row.stock_quantity,
        "库存精确": row.stock_exact,
        "库存方式": row.stock_method,
        "库存说明": row.stock_note,
        "每位客户限购": row.customer_purchase_limit,
        "非平台仓": row.is_leadtime,
        "链接": row.url,
    }


def _display_variant_label(label: str) -> str:
    """Normalize legacy selector dictionaries into concise human labels."""
    normalized_parts: list[str] = []
    for part in re.split(r"\s+/\s+", label.strip()):
        separator = "：" if "：" in part else ":" if ":" in part else ""
        if not separator:
            normalized_parts.append(part.strip())
            continue
        title, raw_value = part.split(separator, 1)
        title = title.strip()
        raw_value = raw_value.strip()
        value: object = raw_value
        if raw_value.startswith("{") and raw_value.endswith("}"):
            try:
                value = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError):
                value = None
        display_value = _variant_scalar_value(value)
        normalized_parts.append(f"{title}：{display_value}" if display_value else title)
    return " / ".join(part for part in normalized_parts if part) or "默认变体"


def _variant_scalar_value(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key in ("name", "label", "value", "title"):
            candidate = _variant_scalar_value(value.get(key))
            if candidate:
                return candidate
        return None
    if value is None or isinstance(value, (list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None
