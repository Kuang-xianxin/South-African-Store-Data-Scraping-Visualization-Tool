"""Batch-friendly competitor collection and read-only dashboard loading."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
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
from takealot_ops.competitors.domain import (
    CompetitorProduct,
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
from takealot_ops.storage.models import (
    CompetitorLinkHealth,
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorVariantSnapshot,
)


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

    def _report_stage(self, stage: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(stage)

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
    ) -> CompetitorCollectionResult:
        plid = extract_plid(url)
        try:
            self._report_stage("正在读取商品与变体")
            product = await self._client.fetch_product(url)
            discovered_targets = _discovered_offer_targets(product, submitted_url=url)
            self._report_stage("正在读取全部评论")
            reviews = await self._client.fetch_all_reviews(product.plid)
            self._report_stage(
                "正在启动库存探测浏览器"
                if with_stock_probe
                else "本条未启用库存探测"
            )
            variant_stocks, offer_stocks = await self._collect_product_stocks(
                product,
                enabled=with_stock_probe,
                visible_browser=visible_browser,
            )
            stock = _aggregate_variant_stock(variant_stocks)
            self._report_stage("正在保存商品快照")
            collected_at = datetime.now(UTC)
            with Session(self._engine) as session:
                repository = CompetitorRepository(session)
                with session.begin():
                    previous = repository.latest_compatible_snapshot(product)
                    summary = summarize_reviews(reviews)
                    lifetime_sales = estimate_lifetime_sales(product.review_count)
                    signal = analyze_sales_signal(
                        previous,
                        current_stock_quantity=stock.quantity,
                        current_stock_exact=stock.exact,
                        current_review_count=product.review_count,
                    )
                    repository.save_observation(
                        product=product,
                        reviews=reviews,
                        review_summary=summary,
                        stock=stock,
                        variant_stocks=variant_stocks,
                        offer_stocks=offer_stocks,
                        lifetime_sales=lifetime_sales,
                        signal=signal,
                        collected_at=collected_at,
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
                stock_scope_count = len(variant_stocks) + len(offer_stocks)
                return CompetitorCollectionResult(
                    plid=plid,
                    title=product.title,
                    succeeded=False,
                    message=(
                        f"商品与评论快照已保存，但 {failed_stock_count}/"
                        f"{stock_scope_count} 个变体/卖家报价库存仍未探测；"
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
                message=_collection_message(
                    stock,
                    len(variant_stocks),
                    len(offer_stocks),
                ),
                discovered_targets=discovered_targets,
            )
        except CompetitorNotFoundError:
            self._report_stage("正在复核疑似失效链接")
            try:
                previously_confirmed = self._is_confirmed_invalid(plid)
                control = None if previously_confirmed else self._latest_control_product(plid)
                control_verified = False
                control_plid: str | None = None
                if control is not None:
                    control_plid, control_url = control
                    try:
                        await self._client.confirm_product_page_absent(
                            url,
                            control_url,
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

    def _latest_control_product(self, plid: str) -> tuple[str, str] | None:
        with Session(self._engine) as session:
            return CompetitorRepository(session).latest_control_product(exclude_plid=plid)

    def _is_confirmed_invalid(self, plid: str) -> bool:
        with Session(self._engine) as session:
            return CompetitorRepository(session).is_confirmed_invalid(plid)

    async def _collect_product_stocks(
        self,
        product: CompetitorProduct,
        *,
        enabled: bool,
        visible_browser: bool,
    ) -> tuple[list[VariantStockObservation], list[OfferStockObservation]]:
        if not enabled:
            variant_stocks = [
                VariantStockObservation(variant=variant, stock=skipped_stock_probe())
                for variant in product.variants
            ]
            offer_stocks = [
                OfferStockObservation(offer=offer, stock=skipped_stock_probe())
                for offer in product.offers
                if not offer.is_buybox
            ]
            return variant_stocks, offer_stocks
        try:
            return await probe_product_stocks(
                product,
                profile_dir=self._project_root / "data" / "competitor-browser-profile",
                visible=visible_browser,
            )
        except (OSError, RuntimeError) as exc:
            failed = StockProbeResult(quantity=None, exact=False, method="failed", note=str(exc))
            variant_stocks = [
                VariantStockObservation(variant=variant, stock=failed)
                for variant in product.variants
            ]
            offer_stocks = [
                OfferStockObservation(offer=offer, stock=failed)
                for offer in product.offers
                if not offer.is_buybox
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
            rows = list(
                session.scalars(
                    select(CompetitorLinkHealth)
                    .where(CompetitorLinkHealth.status != "healthy")
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


def load_competitor_dataset(
    engine: Engine,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CompetitorDataset:
    """Load competitor views and recompute signals across the selected interval."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    try:
        with Session(engine) as session:
            targets = list(
                session.scalars(
                    select(CompetitorTarget)
                    .where(CompetitorTarget.active.is_(True))
                    .order_by(CompetitorTarget.updated_at.desc())
                )
            )
            snapshots = list(
                session.scalars(
                    select(CompetitorSnapshot).order_by(CompetitorSnapshot.collected_at.desc())
                )
            )
            reviews = list(
                session.scalars(
                    select(CompetitorReview).order_by(CompetitorReview.review_date.desc())
                )
            )
            variants = list(
                session.scalars(
                    select(CompetitorVariantSnapshot).order_by(
                        CompetitorVariantSnapshot.collected_at.desc(),
                        CompetitorVariantSnapshot.id.asc(),
                    )
                )
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

    active_plids = {target.plid for target in targets}
    active_snapshots = [row for row in snapshots if row.plid in active_plids]
    snapshot_dates = [_competitor_display_date(row.collected_at) for row in active_snapshots]
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

    latest_by_plid: dict[str, CompetitorSnapshot] = {}
    snapshots_by_plid: dict[str, list[CompetitorSnapshot]] = {}
    for snapshot in interval_snapshots:
        latest_by_plid.setdefault(snapshot.plid, snapshot)
        snapshots_by_plid.setdefault(snapshot.plid, []).append(snapshot)
    variant_signatures: dict[int, frozenset[tuple[str, str, str]]] = {}
    for variant in variants:
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
        oldest = interval[-1]
        signal, stock_change, stock_comparable = _interval_sales_signal(
            oldest,
            latest,
            variant_signatures=variant_signatures,
        )
        price_start, price_change, price_signal = _interval_price_signal(oldest, latest)
        offer_rows = _interval_offer_rows(oldest, latest)
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
                price_start=price_start,
                price_change=price_change,
                price_signal=price_signal,
                offer_rows=offer_rows,
            )
        )
    current = pd.DataFrame(current_rows)
    history = pd.DataFrame(
        [
            _snapshot_row(
                row,
                offer_rows=_interval_offer_rows(row, row, raw_history=True),
                raw_history=True,
            )
            for row in interval_snapshots
        ]
    )
    review_frame = pd.DataFrame(
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
    interval_snapshot_ids = {snapshot.id for snapshot in interval_snapshots}
    snapshot_images = {snapshot.id: snapshot.image_url for snapshot in interval_snapshots}
    variant_frame = pd.DataFrame(
        [
            _variant_row(
                row,
                default_image_url=snapshot_images.get(row.snapshot_id),
            )
            for row in variants
            if row.snapshot_id in interval_snapshot_ids
        ]
    )
    return CompetitorDataset(
        current=current,
        history=history,
        reviews=review_frame,
        variants=variant_frame,
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
) -> tuple[list[tuple[str, Mapping[str, object]]], set[str]]:
    indexed: dict[str, Mapping[str, object]] = {}
    order: list[str] = []
    ambiguous: set[str] = set()
    for index, offer in enumerate(_snapshot_offers(row)):
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
    raw_history: bool = False,
) -> list[dict[str, object]]:
    """Compare each seller offer by offer_id, never by the shared product PLID."""

    oldest_items, oldest_ambiguous = _indexed_snapshot_offers(oldest)
    latest_items, latest_ambiguous = _indexed_snapshot_offers(latest)
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
                "offer_id": str(offer.get("offer_id") or "").strip() or None,
                "卖家ID": str(offer.get("seller_id") or "").strip() or None,
                "卖家": str(offer.get("seller_name") or "未知卖家"),
                "SKU": str(offer.get("sku") or "").strip() or None,
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
    price_start: float | None = None,
    price_change: float | None = None,
    price_signal: str | None = None,
    offer_rows: list[dict[str, object]] | None = None,
    raw_history: bool = False,
) -> dict[str, object]:
    stock_text = _stock_text(row)
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
    return {
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
        "评论数": row.review_count,
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
        "新增评论": review_delta,
        "趋势判断": trend_label,
        "判断说明": trend_note,
        "信号区间开始": signal_start,
        "信号区间结束": signal_end,
        "区间快照数": interval_snapshot_count,
        "库存可比": stock_comparable,
        "链接": row.url,
        "跟卖报价": offer_rows or [],
    }


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
