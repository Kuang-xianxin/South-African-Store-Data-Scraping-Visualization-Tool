"""Streamlit entrypoint for the local operations dashboard."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yaml
from sqlalchemy import func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.dashboard.charts import (
    build_quadrant_figure,
    build_sales_figure,
    build_store_sales_figure,
    build_traffic_figure,
)
from takealot_ops.dashboard.labels import (
    ANOMALY_EXPLANATIONS,
    ANOMALY_LABELS,
    EVENT_LABELS,
    FIELD_LABELS,
    OFFER_STATUS_LABELS,
    PAGE_NAMES,
    QUADRANT_LABELS,
    SALE_STATUS_LABELS,
    SEVERITY_LABELS,
)
from takealot_ops.dashboard.refresh import run_dashboard_refresh
from takealot_ops.metrics.service import (
    DashboardDataset,
    MetricService,
    build_quadrant_window,
    classify_quadrants,
    latest_metric_anomalies,
)
from takealot_ops.nft102_portal import (
    Nft102GenerationResult,
    generate_nft102_from_baseline,
    inspect_nft102_upload,
    persist_nft102_baseline,
)
from takealot_ops.reporting import generate_daily_reports
from takealot_ops.scheduler import verify_database_integrity
from takealot_ops.settings import DashboardSettings, SettingsError
from takealot_ops.storage.models import CollectionRun, DailyProductMetric
from takealot_ops.storage.migrations import create_read_only_engine as _create_read_only_engine
from takealot_ops.storage.repository import Repository


PLOTLY_CHART_CONFIG = {
    "displayModeBar": False,
    "displaylogo": False,
    "responsive": True,
}

CHINESE_UI_STYLES = """
<style>
header[data-testid="stHeader"],
[data-testid="stHeaderActionElements"],
[data-testid="stElementToolbar"] {
    display: none !important;
}
[data-testid="stFileUploaderDropzone"] button p,
[data-testid="stFileUploaderDropzone"] button [data-testid="stIconMaterial"],
[data-testid="stFileUploaderDropzoneInstructions"] span {
    display: none !important;
}
[data-testid="stFileUploaderDropzone"] button::after {
    content: "选择文件";
    font-size: 0.875rem;
}
[data-testid="stFileUploaderDropzoneInstructions"]::after {
    content: "单个文件不超过100兆字节 · 电子表格";
    font-size: 0.875rem;
}
</style>
"""

@dataclass(frozen=True)
class DashboardFreshness:
    """Latest durable collection and metric timestamps shown to operators."""

    last_successful_collection_at: datetime | None
    latest_metric_date: date | None


def filter_as_of(frame: pd.DataFrame, as_of: date, date_column: str) -> pd.DataFrame:
    """Return rows on or before the selected date, preserving an empty schema."""
    if frame.empty or date_column not in frame.columns:
        return frame.copy()
    dates = pd.to_datetime(frame[date_column], errors="coerce").dt.date
    return frame.loc[dates <= as_of].copy()


def search_products(
    product_daily: pd.DataFrame, offer_current: pd.DataFrame, query: str
) -> pd.DataFrame:
    """Search daily rows via current offer identity without assuming metric columns."""
    if product_daily.empty or "offer_id" not in product_daily.columns:
        return product_daily.copy()
    normalized = query.strip().casefold()
    if not normalized:
        return product_daily.copy()
    identity_columns = ("offer_id", "sku", "tsin_id", "barcode", "title")
    if offer_current.empty or "offer_id" not in offer_current.columns:
        identity = pd.DataFrame(columns=identity_columns)
    else:
        available = [column for column in identity_columns if column in offer_current.columns]
        identity = offer_current.loc[:, available].copy()
        for column in identity_columns:
            if column not in identity.columns:
                identity[column] = None
        identity = identity.loc[:, list(identity_columns)].drop_duplicates(
            subset=["offer_id"], keep="last"
        )
    product_identity = product_daily.loc[:, ["offer_id"]].drop_duplicates()
    if "sku" in product_daily.columns:
        metric_sku = product_daily.loc[:, ["offer_id", "sku"]].drop_duplicates(
            "offer_id", keep="last"
        )
        product_identity = product_identity.merge(metric_sku, on="offer_id", how="left")
    merged = product_identity.merge(identity, on="offer_id", how="left", suffixes=("_metric", ""))
    if "sku_metric" in merged.columns:
        merged["sku"] = merged["sku"].combine_first(merged["sku_metric"])
    matches = pd.Series(False, index=merged.index)
    for column in identity_columns:
        values = merged[column].fillna("").astype(str).str.casefold()
        matches |= values.str.contains(normalized, regex=False, na=False)
    offer_ids = set(merged.loc[matches, "offer_id"].astype(str))
    return product_daily.loc[product_daily["offer_id"].astype(str).isin(offer_ids)].copy()


def load_dashboard_dataset(
    settings: DashboardSettings, as_of: date
) -> tuple[DashboardDataset | None, str | None]:
    """Load one read-only dataset, returning an operator-friendly error state."""
    engine: Engine | None = None
    try:
        database_path = _sqlite_database_path(settings.database_url)
        if database_path is not None and not database_path.exists():
            return None, f"尚未找到本地数据库：{database_path}"
        engine = create_read_only_engine(settings.database_url)
        with Session(engine) as session:
            service = MetricService(
                Repository(session),
                anomaly_rules_path=settings.project_root / "config" / "anomaly_rules.yaml",
                sale_status_rules_path=settings.project_root / "config" / "sale_status_rules.yaml",
            )
            dataset = service.dashboard_dataset(as_of)
    except (OSError, ValueError, SQLAlchemyError) as exc:
        return None, f"本地数据暂不可用：{exc}"
    finally:
        if engine is not None:
            engine.dispose()
    return _dataset_as_of(dataset, as_of), None


def load_dashboard_freshness(settings: DashboardSettings) -> DashboardFreshness:
    """Read freshness markers without creating or writing the local database."""
    database_path = _sqlite_database_path(settings.database_url)
    if database_path is not None and not database_path.exists():
        return DashboardFreshness(None, None)
    engine: Engine | None = None
    try:
        engine = create_read_only_engine(settings.database_url)
        with Session(engine) as session:
            last_collection = session.scalar(
                select(func.max(CollectionRun.finished_at)).where(
                    CollectionRun.status == "success",
                    CollectionRun.run_type.in_(("offers", "sales")),
                )
            )
            latest_metric = session.scalar(select(func.max(DailyProductMetric.metric_date)))
    except (OSError, ValueError, SQLAlchemyError):
        return DashboardFreshness(None, None)
    finally:
        if engine is not None:
            engine.dispose()
    return DashboardFreshness(last_collection, latest_metric)


def create_read_only_engine(database_url: str) -> Engine:
    """Create an engine whose dashboard connections reject write statements."""
    try:
        return _create_read_only_engine(database_url)
    except ValueError as exc:
        raise SettingsError("看板数据库必须使用受支持的同步驱动") from exc


def main() -> None:
    """Render the local dashboard; collection runs only after explicit actions."""
    st.set_page_config(page_title="南非店铺运营看板", page_icon="📊", layout="wide")
    st.markdown(CHINESE_UI_STYLES, unsafe_allow_html=True)
    project_root = Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd())).resolve()
    try:
        settings = DashboardSettings.from_env(project_root)
    except SettingsError as exc:
        st.title("本地配置不可用")
        st.error(str(exc))
        st.info("请修正本地数据库、主机或端口设置；浏览看板无需设置接口密钥。")
        return
    configured_address = st.get_option("server.address")
    if configured_address not in {None, "127.0.0.1", "localhost"}:
        st.title("本地安全设置冲突")
        st.error("当前页面服务不是本机回环地址，页面已停止加载。")
        st.info("请使用项目提供的本地看板启动入口重新启动。")
        return

    freshness = load_dashboard_freshness(settings)
    with st.sidebar:
        st.header("南非店铺运营看板")
        page_name = st.radio("页面", PAGE_NAMES)
        as_of = st.date_input("数据截止日期", value=date.today())
        st.caption("本地运行 · 数据刷新和日报更新只调用平台只读接口")
        st.divider()
        st.caption(
            f"最近成功采集：{_format_china_datetime(freshness.last_successful_collection_at)}"
        )
        st.caption(
            "最新指标日期："
            + (
                freshness.latest_metric_date.isoformat()
                if freshness.latest_metric_date is not None
                else "暂无"
            )
        )
        refresh_notice = st.session_state.pop("dashboard_refresh_notice", None)
        if isinstance(refresh_notice, str):
            st.success(refresh_notice)
        refresh_clicked = st.button(
            "立即刷新看板数据",
            type="primary",
            width="stretch",
        )
        st.caption(
            "平台每日切日后使用；通常需要1至3分钟。刷新包含完整采集、指标重建、"
            "日报导出、完整性检查和备份。"
        )
        if refresh_clicked:
            with st.spinner("正在采集并重建指标，请勿关闭页面……"):
                refresh_result = run_dashboard_refresh(settings.project_root)
            if refresh_result.succeeded:
                st.session_state["dashboard_refresh_notice"] = refresh_result.message
                st.rerun()
            else:
                st.error(refresh_result.message)

    dataset, load_error = load_dashboard_dataset(settings, as_of)
    renderers: dict[
        str, Callable[[DashboardDataset | None, str | None, DashboardSettings, date], None]
    ] = {
        "店铺总览": _render_overview,
        "单品分析": _render_product,
        "经营四象限": _render_quadrants,
        "异常商品": _render_anomalies,
        "数据质量": _render_quality,
        "竞品观察": _render_competitors,
        "NFT102 日报更新": _render_nft102_update,
        "导出中心": _render_exports,
    }
    renderers[page_name](dataset, load_error, settings, as_of)


def _render_overview(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    st.title("店铺总览")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    if dataset.product_daily.empty:
        _empty_state("暂无指标数据", "请先运行采集与指标计算任务，再回来查看经营概况。")
        return
    latest = _latest_rows(dataset.product_daily, as_of)
    latest_date = _latest_metric_date(latest)
    st.caption(
        f"页面截止 {as_of.isoformat()} · 最新可用指标日 {latest_date} · 下单件数为主销售口径"
    )
    store = dataset.store_daily.sort_values("metric_date")
    recent_start = as_of - timedelta(days=6)
    recent = filter_as_of(store, as_of, "metric_date")
    recent_dates = pd.to_datetime(recent["metric_date"], errors="coerce").dt.date
    recent = recent.loc[recent_dates >= recent_start]
    metrics = st.columns(4)
    metrics[0].metric("最新可用日下单件数", _sum_value(latest, "ordered_units", integer=True))
    metrics[1].metric(
        "最新可用日下单金额", _currency_or_missing(_sum_numeric(latest, "ordered_revenue"))
    )
    metrics[2].metric("近7日下单件数", _sum_value(recent, "ordered_units", integer=True))
    _, latest_anomalies = latest_metric_anomalies(dataset)
    metrics[3].metric(
        "最新指标日异常商品数", _unique_count(latest_anomalies, "offer_id")
    )
    second = st.columns(4)
    second[0].metric("近30天浏览量合计", _sum_value(latest, "page_views_30_days", integer=True))
    second[1].metric(
        "近30天转化率中位数",
        _percentage_median(latest, "conversion_percentage_30_days"),
    )
    sold = latest.loc[pd.to_numeric(latest["ordered_units"], errors="coerce") > 0]
    second[2].metric("有销量商品数", _unique_count(sold, "offer_id"))
    stock = (
        pd.to_numeric(latest["total_stock"], errors="coerce")
        if "total_stock" in latest.columns
        else pd.Series(dtype="float64")
    )
    second[3].metric("缺货商品数", int((stock == 0).sum()))
    st.plotly_chart(
        build_store_sales_figure(store.tail(30)),
        width="stretch",
        config=PLOTLY_CHART_CONFIG,
    )
    _effective_units_notice(settings.project_root)


def _render_product(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    st.title("单品分析")
    st.caption("销售趋势与保存的30天流量快照分开展示，避免把快照变化误读为当天浏览量。")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    if dataset.product_daily.empty:
        _empty_state("暂无单品指标", "采集并计算指标后，可按库存编码、商品编号、条码或名称搜索。")
        return
    query = st.text_input("搜索商品", placeholder="输入库存编码、商品编号、条码或商品名称")
    matches = search_products(dataset.product_daily, dataset.offer_current, query)
    if matches.empty:
        st.info("没有找到匹配商品，请检查搜索词。")
        return
    offer_ids = sorted(matches["offer_id"].dropna().astype(str).unique())
    selected_offer = st.selectbox("选择商品", offer_ids)
    history = matches.loc[matches["offer_id"].astype(str) == selected_offer].copy()
    history = filter_as_of(history, as_of, "metric_date").sort_values("metric_date")
    identity = _offer_identity(dataset.offer_current, selected_offer)
    latest = history.iloc[-1]
    title = _display(identity.get("title"), "未命名商品")
    st.subheader(title)
    st.caption(
        " · ".join(
            [
                f"库存编码：{_display(identity.get('sku', latest.get('sku')))}",
                f"商品编号：{selected_offer}",
                f"平台商品编号：{_display(identity.get('tsin_id'))}",
                f"条码：{_display(identity.get('barcode'))}",
            ]
        )
    )
    identity_columns = st.columns(4)
    identity_columns[0].metric("当前售价", _currency_or_missing(identity.get("selling_price")))
    identity_columns[1].metric("建议零售价", _currency_or_missing(identity.get("rrp")))
    identity_columns[2].metric("平台可售库存", _display(identity.get("total_stock")))
    identity_columns[3].metric(
        "商品状态", _localized_value(identity.get("status"), OFFER_STATUS_LABELS)
    )
    metric_columns = st.columns(4)
    latest_metric_date = _display(latest.get("metric_date"))
    st.caption(f"当前商品最新可用指标日：{latest_metric_date}")
    metric_columns[0].metric("最新可用日下单件数", _number_or_missing(latest.get("ordered_units")))
    metric_columns[1].metric(
        "近7日下单件数",
        _number_or_missing(_calendar_window_sum(history, "ordered_units", days=7)),
    )
    metric_columns[2].metric("近30天浏览量", _number_or_missing(latest.get("page_views_30_days")))
    metric_columns[3].metric(
        "近30天转化率", _percentage_or_missing(latest.get("conversion_percentage_30_days"))
    )
    st.plotly_chart(build_traffic_figure(history), width="stretch", config=PLOTLY_CHART_CONFIG)
    st.plotly_chart(build_sales_figure(history), width="stretch", config=PLOTLY_CHART_CONFIG)
    _effective_units_notice(settings.project_root)


def _render_quadrants(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del settings
    st.title("经营四象限")
    st.caption(
        "使用最新近30天浏览量与近7日下单件数进行相对排名；销量为0始终归入低销量侧，"
        "缺失任一指标时明确列为未分类。"
    )
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    latest = build_quadrant_window(dataset.product_daily, as_of, days=7)
    if latest.empty:
        _empty_state("暂无可分类商品", "需要同一截止日期下的商品指标后才能计算四象限。")
        return
    percentile = st.select_slider("分组严格程度（分位数）", options=[25, 50, 75], value=50)
    classified = classify_quadrants(latest, percentile=percentile)
    classified["quadrant"] = classified["quadrant"].fillna("unclassified")
    counts = classified["quadrant"].value_counts()
    columns = st.columns(5)
    for column, (key, label) in zip(columns, QUADRANT_LABELS.items(), strict=True):
        column.metric(label, int(counts.get(key, 0)))
    view_boundary = classified.attrs["page_views_boundary"]
    unit_boundary = classified.attrs["ordered_units_boundary"]
    view_boundary_label = "暂无" if pd.isna(view_boundary) else str(int(view_boundary))
    unit_boundary_label = "暂无" if pd.isna(unit_boundary) else str(int(unit_boundary))
    st.caption(
        "当前分界：近30天浏览量 ≥ "
        f"{view_boundary_label}；近7日下单件数 ≥ {unit_boundary_label}。"
        "图中位置是相对排名，"
        "鼠标移到商品点上可查看真实数值。"
    )
    st.plotly_chart(build_quadrant_figure(classified), width="stretch", config=PLOTLY_CHART_CONFIG)
    display = classified.copy()
    display["quadrant"] = display["quadrant"].map(QUADRANT_LABELS).fillna("未分类")
    display = display.rename(columns={"ordered_units": "ordered_units_7_days"})
    _dataframe(
        display,
        [
            "offer_id",
            "sku",
            "page_views_30_days",
            "ordered_units_7_days",
            "quadrant",
        ],
    )


def _render_anomalies(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del settings
    st.title("异常商品")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    history = filter_as_of(dataset.anomalies, as_of, "event_date")
    latest_date, latest = latest_metric_anomalies(dataset)
    scope = st.radio(
        "查看范围",
        ("最新指标日", "全部历史"),
        horizontal=True,
        key="anomaly_scope",
    )
    if scope == "最新指标日":
        anomalies = latest
        date_label = latest_date.isoformat() if latest_date is not None else "暂无"
        st.caption(
            f"默认仅显示最新指标日 {date_label} 的异常；同一商品触发多种异常时会保留多条记录。"
        )
        metric_label = "最新指标日异常商品数"
    else:
        anomalies = history
        st.caption(
            f"显示截至 {as_of.isoformat()} 的全部历史异常；同一商品跨日期或触发多种异常时会有多条记录。"
        )
        metric_label = "历史异常商品数"
    if anomalies.empty:
        _empty_state("当前范围没有异常记录", "可切换查看范围，或确认所选截止日期已有指标。")
        return
    anomaly_types = sorted(anomalies["anomaly_type"].dropna().astype(str).unique())
    selected = st.multiselect(
        "异常类型",
        anomaly_types,
        default=anomaly_types,
        format_func=lambda value: ANOMALY_LABELS.get(value, value),
    )
    filtered = anomalies.loc[anomalies["anomaly_type"].isin(selected)].copy()
    filtered["explanation"] = (
        filtered["anomaly_type"].map(ANOMALY_EXPLANATIONS).fillna("暂无中文说明")
    )
    st.metric(metric_label, _unique_count(filtered, "offer_id"))
    st.caption(
        f"当前共 {len(filtered)} 条异常记录，涉及 {_unique_count(filtered, 'offer_id')} 个去重商品。"
    )
    _dataframe(
        filtered,
        ["event_date", "offer_id", "anomaly_type", "severity", "explanation"],
    )


def _render_quality(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    st.title("数据质量")
    st.caption("只读展示质量事件；采集、重试与规则调整需在命令行工作流完成。")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    events = filter_as_of(dataset.quality_events, as_of, "event_date")
    configured = _effective_units_configured(settings.project_root)
    if configured is True:
        st.success("有效销售状态规则已包含计入状态；下单件数仍为页面主口径。")
    elif configured is False:
        st.warning("有效销售状态规则尚未包含计入状态；暂不提升有效销售件数。")
    else:
        st.warning("无法确认有效销售状态规则；下单件数保持为主口径。")
    if events.empty:
        _empty_state("暂无质量事件", "这可能表示尚未完成采集，也可能表示当前没有已记录问题。")
        return
    metrics = st.columns(3)
    metrics[0].metric("质量事件数", len(events))
    metrics[1].metric("涉及商品数", _unique_count(events, "offer_id"))
    unknown = events.loc[events["event_type"] == "unknown_sale_status"]
    metrics[2].metric("未知销售状态事件", len(unknown))
    events = events.copy()
    events["details"] = events.apply(_quality_detail, axis=1)
    _dataframe(events, ["event_date", "event_type", "severity", "offer_id", "details"])


def _render_competitors(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del dataset, load_error, as_of
    st.title("竞品观察")
    st.caption(
        "竞品模块使用 Vue + TypeScript 构建，并通过本机接口与当前 MySQL 共用数据。"
    )
    st.caption(
        "页面只连接 127.0.0.1；采集必须在 Vue 页面中明确点击后才会执行。"
    )
    competitor_port = (
        settings.dashboard_port + 1 if settings.dashboard_port < 65535 else 8502
    )
    competitor_url = f"http://127.0.0.1:{competitor_port}"
    st.link_button("在新窗口打开竞品中心", competitor_url, type="primary")
    st.components.v1.iframe(competitor_url, height=920, scrolling=True)


def _render_exports(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    st.title("导出中心")
    st.caption(
        "按当前截止日期一键生成离线网页、电子表格和图片；只读取本地数据库，"
        "不会重新采集，也不会调用平台接口。"
    )
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    export_root = settings.project_root / "exports"
    partition = export_root / as_of.isoformat()
    basename = f"Takealot运营日报_{as_of.isoformat()}"
    export_clicked = st.button(
        "一键导出全部报表",
        type="primary",
        width="stretch",
        key=f"export_all_{as_of.isoformat()}",
    )
    if export_clicked:
        try:
            verify_database_integrity(settings)
        except (OSError, RuntimeError, ValueError):
            st.error("导出前的本地数据库完整性检查未通过，请先检查数据库。")
        else:
            try:
                with st.spinner("正在生成离线网页、电子表格和图片……"):
                    generated = generate_daily_reports(dataset, export_root, as_of)
            except (OSError, RuntimeError, ValueError):
                st.error("报表生成失败，请检查本地文件权限和运行环境后重试。")
            else:
                st.success(f"{as_of.isoformat()} 的日报已生成，可在下方直接下载。")
                if generated.png_error is not None:
                    st.warning("离线网页和电子表格已生成，但图片生成失败，请检查浏览器运行环境。")

    file_specs = (
        ("离线网页", ".html", "text/html"),
        (
            "电子表格",
            ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("图片", ".png", "image/png"),
    )
    rows = []
    paths: list[tuple[str, Path, str]] = []
    for label, suffix, mime in file_specs:
        path = partition / f"{basename}{suffix}"
        paths.append((label, path, mime))
        rows.append(
            {
                "格式": label,
                "状态": "已生成" if path.is_file() else "未生成",
                "保存位置": f"项目日报导出目录 / {as_of.isoformat()}",
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    available_paths = [(label, path, mime) for label, path, mime in paths if path.is_file()]
    if not available_paths:
        st.info("所选日期暂无日报，点击上方按钮即可从现有本地数据生成。")
        return
    st.success("已找到日报文件，可直接下载或在项目日报导出目录中打开。")
    download_columns = st.columns(len(available_paths))
    for column, (label, path, mime) in zip(
        download_columns, available_paths, strict=True
    ):
        with column:
            st.download_button(
                f"下载{label}",
                data=path.read_bytes(),
                file_name=path.name,
                mime=mime,
                width="stretch",
                key=f"download_{path.suffix}_{as_of.isoformat()}_{label}",
            )


def _render_nft102_update(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del dataset, load_error, as_of
    st.title("NFT102 日报更新")
    st.caption(
        "上传运营同事当天修改完成的电子表格，系统以它为唯一基准生成下一日副本；上传文件不会被覆盖。"
    )
    st.info("使用顺序：运营完成备注并保存 → 上传最终版 → 核对识别日期 → 点击生成 → 下载新表格。")
    uploaded = st.file_uploader(
        "上传运营回传的电子表格",
        type=["xlsx"],
        key="nft102_operator_baseline",
    )
    if uploaded is None:
        st.warning("请先上传运营同事修改后的最终版电子表格。")
        return

    content = uploaded.getvalue()
    try:
        inspection = inspect_nft102_upload(uploaded.name, content)
    except ValueError as exc:
        st.error(str(exc))
        return

    metrics = st.columns(4)
    metrics[0].metric("文件大小", f"{inspection.size_bytes / 1024 / 1024:.2f} 兆字节")
    metrics[1].metric("识别商品列", inspection.product_columns)
    metrics[2].metric("表内最新日期", inspection.latest_report_date.isoformat())
    metrics[3].metric("建议新增日期", inspection.suggested_report_date.isoformat())

    report_date = st.date_input(
        "本次新增的表格日期",
        value=inspection.suggested_report_date,
        key=f"nft102_report_date_{inspection.sha256}",
        help="当天订单数会读取该日期前一天的完整销售件数。",
    )
    now_china = datetime.now(ZoneInfo("Asia/Shanghai"))
    date_is_valid = report_date > inspection.latest_report_date and report_date <= now_china.date()
    if report_date <= inspection.latest_report_date:
        st.error("新增日期必须晚于表内最新日期，不能覆盖或重复已有日期。")
    elif report_date > now_china.date():
        st.error("不能提前生成未来日期；请在该日期中国时间 10:05 后再操作。")
    elif report_date != inspection.suggested_report_date:
        st.warning("所选日期不是连续下一天，请确认中间日期确实不需要补录。")

    if now_china.time() < time(10, 5):
        st.warning("建议中国时间 10:05 后生成，避开平台每日销量切日刷新窗口。")

    st.caption("生成时会读取平台只读接口，并在项目内存档本次上传基准、输出新表格和核对报告。")
    generate_clicked = st.button(
        "保存基准并生成下一日表格",
        type="primary",
        disabled=not date_is_valid,
        key=f"nft102_generate_{inspection.sha256}",
    )
    result_key = f"nft102_generation_result_{inspection.sha256}"
    baseline_key = f"nft102_baseline_path_{inspection.sha256}"
    if generate_clicked:
        try:
            saved_baseline = Path(str(st.session_state.get(baseline_key, "")))
            if not saved_baseline.is_file():
                saved_baseline = persist_nft102_baseline(settings.project_root, inspection, content)
                st.session_state[baseline_key] = str(saved_baseline)
            with st.spinner("正在拉取数据并生成新表格，请勿关闭页面……"):
                result = generate_nft102_from_baseline(
                    settings.project_root, saved_baseline, report_date
                )
            st.session_state[result_key] = result
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(str(exc))

    stored_result = st.session_state.get(result_key)
    if (
        isinstance(stored_result, Nft102GenerationResult)
        and stored_result.report_date == report_date
        and stored_result.workbook_path.is_file()
    ):
        _show_nft102_generation_result(stored_result)


def _show_nft102_generation_result(result: Nft102GenerationResult) -> None:
    st.success(f"{result.report_date.isoformat()} 的 NFT102 新表格已生成。")
    st.caption("本次上传的运营最终版已原样存档，新表格不会覆盖原文件。")
    st.download_button(
        "下载新表格",
        data=result.workbook_path.read_bytes(),
        file_name=result.workbook_path.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key=f"nft102_download_{result.workbook_path.name}",
    )
    st.download_button(
        "下载运营核对说明",
        data=result.audit_text_path.read_bytes(),
        file_name=result.audit_text_path.name,
        mime="text/plain",
        key=f"nft102_audit_{result.audit_text_path.name}",
    )


def _dataset_as_of(dataset: DashboardDataset, as_of: date) -> DashboardDataset:
    return DashboardDataset(
        store_daily=filter_as_of(dataset.store_daily, as_of, "metric_date"),
        product_daily=filter_as_of(dataset.product_daily, as_of, "metric_date"),
        offer_current=dataset.offer_current.copy(),
        anomalies=filter_as_of(dataset.anomalies, as_of, "event_date"),
        quality_events=filter_as_of(dataset.quality_events, as_of, "event_date"),
    )


def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def _latest_rows(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    scoped = filter_as_of(frame, as_of, "metric_date")
    if scoped.empty:
        return scoped
    dates = pd.to_datetime(scoped["metric_date"], errors="coerce").dt.date
    return scoped.loc[dates == dates.max()].copy()


def _latest_metric_date(frame: pd.DataFrame) -> str:
    if frame.empty or "metric_date" not in frame.columns:
        return "—"
    values = pd.to_datetime(frame["metric_date"], errors="coerce").dropna()
    return "—" if values.empty else values.max().date().isoformat()


def _offer_identity(frame: pd.DataFrame, offer_id: str) -> dict[str, Any]:
    if frame.empty or "offer_id" not in frame.columns:
        return {}
    rows = frame.loc[frame["offer_id"].astype(str) == offer_id]
    if rows.empty:
        return {}
    return {str(key): value for key, value in rows.iloc[-1].to_dict().items()}


def _effective_units_configured(project_root: Path) -> bool | None:
    path = project_root / "config" / "sale_status_rules.yaml"
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    included = loaded.get("included")
    if not isinstance(included, list) or any(not isinstance(item, str) for item in included):
        return None
    return bool(included)


def _effective_units_notice(project_root: Path) -> None:
    configured = _effective_units_configured(project_root)
    if configured is not True:
        st.warning("有效销售状态规则尚未确认；当前以“下单件数”为主销售口径。")


def _require_dataset(dataset: DashboardDataset | None, load_error: str | None) -> bool:
    if dataset is not None:
        return True
    st.warning(load_error or "本地数据暂不可用。")
    st.info(
        "请确认数据库已由采集和指标任务创建，且配置目录中的规则文件完整。浏览看板无需设置接口密钥。"
    )
    return False


def _empty_state(title: str, guidance: str) -> None:
    st.info(f"{title}。{guidance}")


def _dataframe(frame: pd.DataFrame, columns: list[str]) -> None:
    available = [column for column in columns if column in frame.columns]
    display = frame.loc[:, available].copy()
    translations = {
        "anomaly_type": ANOMALY_LABELS,
        "event_type": EVENT_LABELS,
        "severity": SEVERITY_LABELS,
        "offer_status": OFFER_STATUS_LABELS,
        "status": OFFER_STATUS_LABELS,
    }
    for column, labels in translations.items():
        if column in display.columns:
            display[column] = display[column].map(
                lambda value, mapping=labels: _localized_value(value, mapping)
            )
    display = display.rename(columns=FIELD_LABELS)
    st.dataframe(display, width="stretch", hide_index=True)


def _localized_value(value: object, labels: dict[str, str]) -> str:
    if value is None or str(value).strip().casefold() in {"nan", "nat", "<na>", "none"}:
        return "—"
    return labels.get(str(value), "未识别状态")


def _format_china_datetime(value: datetime | None) -> str:
    if value is None:
        return "暂无"
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    china_time = normalized.astimezone(ZoneInfo("Asia/Shanghai"))
    return china_time.strftime("%Y-%m-%d %H:%M")


def _quality_detail(row: pd.Series) -> str:
    if row.get("event_type") == "unknown_sale_status":
        details = row.get("details")
        statuses = details.get("sale_statuses") if isinstance(details, dict) else None
        if isinstance(statuses, list):
            rendered = "、".join(
                SALE_STATUS_LABELS.get(str(value), "未识别状态") for value in statuses
            )
            if rendered:
                return f"涉及销售状态：{rendered}；有效件数暂不计算。"
        return "销售状态尚未配置，有效件数暂不计算。"
    return "暂无中文说明"


def _sum_numeric(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    total = pd.to_numeric(frame[column], errors="coerce").sum(min_count=1)
    return None if pd.isna(total) else float(total)


def _sum_value(frame: pd.DataFrame, column: str, *, integer: bool = False) -> str:
    value = _sum_numeric(frame, column)
    if value is None:
        return "—"
    return f"{int(value):,}" if integer else f"{value:,.2f}"


def _unique_count(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].dropna().nunique())


def _calendar_window_sum(
    frame: pd.DataFrame,
    column: str,
    *,
    days: int,
    date_column: str = "metric_date",
) -> float | None:
    if days < 1:
        raise ValueError("days must be positive")
    if frame.empty or column not in frame.columns or date_column not in frame.columns:
        return None
    metric_dates = pd.to_datetime(frame[date_column], errors="coerce").dt.date
    valid_dates = metric_dates.dropna()
    if valid_dates.empty:
        return None
    latest_date = valid_dates.max()
    window_start = latest_date - timedelta(days=days - 1)
    values = pd.to_numeric(
        frame.loc[(metric_dates >= window_start) & (metric_dates <= latest_date), column],
        errors="coerce",
    )
    total = values.sum(min_count=1)
    return None if pd.isna(total) else float(total)


def _percentage_median(frame: pd.DataFrame, column: str) -> str:
    if frame.empty or column not in frame.columns:
        return "—"
    value = pd.to_numeric(frame[column], errors="coerce").median()
    return _percentage_or_missing(value)


def _currency(value: float) -> str:
    return f"R {value:,.2f}"


def _currency_or_missing(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return "—" if pd.isna(number) else _currency(float(number))


def _number_or_missing(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return "—" if pd.isna(number) else f"{float(number):,.0f}"


def _percentage_or_missing(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return "—" if pd.isna(number) else f"{float(number):.2f}%"


def _display(value: object, fallback: str = "—") -> str:
    missing = bool(pd.Series([value], dtype="object").isna().iloc[0])
    return fallback if missing else str(value)


if __name__ == "__main__":
    main()
