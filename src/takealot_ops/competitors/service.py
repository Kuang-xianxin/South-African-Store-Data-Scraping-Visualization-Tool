"""Batch-friendly competitor collection and read-only dashboard loading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorPublicClient, extract_plid
from takealot_ops.competitors.domain import (
    CompetitorProduct,
    StockProbeResult,
    VariantStockObservation,
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.stock import (
    probe_variant_stocks,
    skipped_stock_probe,
)
from takealot_ops.storage.models import (
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


@dataclass(frozen=True)
class CompetitorDataset:
    """Read-only competitor tables used by the Streamlit module."""

    current: pd.DataFrame
    history: pd.DataFrame
    reviews: pd.DataFrame
    variants: pd.DataFrame


class CompetitorCollector:
    """Collect public data and persist one snapshot per explicit target."""

    def __init__(
        self,
        *,
        engine: Engine,
        project_root: Path,
        client: CompetitorPublicClient | None = None,
    ) -> None:
        self._engine = engine
        self._project_root = project_root
        self._client = client or CompetitorPublicClient()
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CompetitorCollector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def collect(
        self,
        url: str,
        *,
        with_stock_probe: bool,
        visible_browser: bool = False,
    ) -> CompetitorCollectionResult:
        plid = extract_plid(url)
        try:
            product = self._client.fetch_product(url)
            reviews = self._client.fetch_all_reviews(product.plid)
            variant_stocks = self._collect_variant_stocks(
                product,
                enabled=with_stock_probe,
                visible_browser=visible_browser,
            )
            stock = _aggregate_variant_stock(variant_stocks)
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
                        lifetime_sales=lifetime_sales,
                        signal=signal,
                        collected_at=collected_at,
                    )
            return CompetitorCollectionResult(
                plid=plid,
                title=product.title,
                succeeded=True,
                message=_collection_message(stock, len(variant_stocks)),
            )
        except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=False,
                message=str(exc),
            )

    def _collect_variant_stocks(
        self,
        product: CompetitorProduct,
        *,
        enabled: bool,
        visible_browser: bool,
    ) -> list[VariantStockObservation]:
        if not enabled:
            return [
                VariantStockObservation(variant=variant, stock=skipped_stock_probe())
                for variant in product.variants
            ]
        try:
            return probe_variant_stocks(
                product,
                profile_dir=self._project_root / "data" / "competitor-browser-profile",
                visible=visible_browser,
            )
        except (OSError, RuntimeError) as exc:
            failed = StockProbeResult(
                quantity=None, exact=False, method="failed", note=str(exc)
            )
            return [
                VariantStockObservation(variant=variant, stock=failed)
                for variant in product.variants
            ]


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
    return StockProbeResult(
        quantity=quantity,
        exact=exact,
        method="all-variants-out-of-stock" if all_unavailable else "variant-aggregate",
        note=(
            f"汇总 {len(observations)} 个变体的平台仓有效库存；"
            "供应商调货与长时效到货按0计。"
        ),
    )


def _collection_message(stock: StockProbeResult, variant_count: int) -> str:
    if stock.method == "failed":
        return f"公开数据已保存；库存探测未取得：{stock.note}"
    return f"采集成功；已记录 {variant_count} 个变体，评论按商品共用一份"


def load_competitor_dataset(engine: Engine) -> CompetitorDataset:
    """Load all competitor views without changing database state."""
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
                    select(CompetitorSnapshot).order_by(
                        CompetitorSnapshot.collected_at.desc()
                    )
                )
            )
            reviews = list(
                session.scalars(
                    select(CompetitorReview).order_by(
                        CompetitorReview.review_date.desc()
                    )
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
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )

    latest_by_plid: dict[str, CompetitorSnapshot] = {}
    for snapshot in snapshots:
        latest_by_plid.setdefault(snapshot.plid, snapshot)
    active_plids = {target.plid for target in targets}
    current = pd.DataFrame(
        [_snapshot_row(row) for plid, row in latest_by_plid.items() if plid in active_plids]
    )
    history = pd.DataFrame([_snapshot_row(row) for row in snapshots])
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
    variant_frame = pd.DataFrame([_variant_row(row) for row in variants])
    return CompetitorDataset(
        current=current,
        history=history,
        reviews=review_frame,
        variants=variant_frame,
    )


def _snapshot_row(row: CompetitorSnapshot) -> dict[str, object]:
    stock_text = "未探测"
    if row.stock_method in {"not-platform-stock", "all-variants-out-of-stock"}:
        stock_text = "没货"
    elif row.stock_quantity is not None:
        stock_text = (
            str(row.stock_quantity)
            if row.stock_exact
            else f"至少{row.stock_quantity}"
        )
    period_range = "待积累"
    if row.period_sales_min is not None and row.period_sales_max is not None:
        period_range = (
            str(row.period_sales_min)
            if row.period_sales_min == row.period_sales_max
            else f"{row.period_sales_min}–{row.period_sales_max}"
        )
    return {
        "plid": row.plid,
        "商品": row.title,
        "采集时间": row.collected_at,
        "当前卖家": row.seller_name,
        "价格": float(row.price) if row.price is not None else None,
        "库存上限": stock_text,
        "库存数量": row.stock_quantity,
        "库存精确": row.stock_exact,
        "评论数": row.review_count,
        "评分": float(row.rating) if row.rating is not None else None,
        "好评": row.positive_reviews,
        "中评": row.neutral_reviews,
        "差评": row.negative_reviews,
        "累计销量估算": f"{row.lifetime_sales_min}–{row.lifetime_sales_max}",
        "观察期销量信号": period_range,
        "观察期估算下限": row.period_sales_min,
        "观察期估算上限": row.period_sales_max,
        "库存净流出": row.observed_stock_outflow,
        "新增评论": row.review_delta,
        "趋势判断": row.trend_label,
        "判断说明": row.trend_note,
        "链接": row.url,
    }


def _variant_row(row: CompetitorVariantSnapshot) -> dict[str, object]:
    stock_text = "未探测"
    if row.stock_method in {"not-platform-stock", "out-of-stock"}:
        stock_text = "没货"
    elif row.stock_quantity is not None:
        stock_text = (
            str(row.stock_quantity)
            if row.stock_exact
            else f"至少{row.stock_quantity}"
        )
    return {
        "plid": row.plid,
        "快照ID": row.snapshot_id,
        "采集时间": row.collected_at,
        "变体键": row.variant_key,
        "变体": row.variant_label,
        "SKU": row.sku,
        "卖家": row.seller_name,
        "价格": float(row.price) if row.price is not None else None,
        "库存": stock_text,
        "库存数量": row.stock_quantity,
        "库存精确": row.stock_exact,
        "库存方式": row.stock_method,
        "库存说明": row.stock_note,
        "非平台仓": row.is_leadtime,
        "链接": row.url,
    }
