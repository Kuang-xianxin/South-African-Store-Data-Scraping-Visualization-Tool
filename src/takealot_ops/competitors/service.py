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
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.stock import (
    non_platform_stock_probe,
    probe_stock,
    skipped_stock_probe,
)
from takealot_ops.storage.models import (
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
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
            stock = self._collect_stock(
                product,
                enabled=with_stock_probe,
                visible_browser=visible_browser,
            )
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
                        lifetime_sales=lifetime_sales,
                        signal=signal,
                        collected_at=collected_at,
                    )
            return CompetitorCollectionResult(
                plid=plid,
                title=product.title,
                succeeded=True,
                message=_collection_message(stock),
            )
        except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=False,
                message=str(exc),
            )

    def _collect_stock(
        self,
        product: CompetitorProduct,
        *,
        enabled: bool,
        visible_browser: bool,
    ) -> StockProbeResult:
        if product.is_leadtime:
            return non_platform_stock_probe()
        if not enabled:
            return skipped_stock_probe()
        try:
            return probe_stock(
                product,
                profile_dir=self._project_root / "data" / "competitor-browser-profile",
                visible=visible_browser,
            )
        except (OSError, RuntimeError) as exc:
            return StockProbeResult(
                quantity=None,
                exact=False,
                method="failed",
                note=str(exc),
            )


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


def _collection_message(stock: StockProbeResult) -> str:
    if stock.method == "failed":
        return f"公开数据已保存；库存探测未取得：{stock.note}"
    if stock.method == "not-platform-stock":
        return "采集成功；供应商调货/长时效到货不计平台仓库存，已标记没货"
    return "采集成功"


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
    except SQLAlchemyError:
        return CompetitorDataset(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())

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
    return CompetitorDataset(current=current, history=history, reviews=review_frame)


def _snapshot_row(row: CompetitorSnapshot) -> dict[str, object]:
    stock_text = "未探测"
    if row.stock_method == "not-platform-stock":
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
