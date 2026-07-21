"""Streamlit entrypoint for the local read-only operations dashboard."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.dashboard.charts import (
    build_quadrant_figure,
    build_sales_figure,
    build_store_sales_figure,
    build_traffic_figure,
)
from takealot_ops.dashboard.labels import (
    ANOMALY_LABELS,
    FIELD_LABELS,
    PAGE_NAMES,
    QUADRANT_LABELS,
)
from takealot_ops.metrics.service import DashboardDataset, MetricService, classify_quadrants
from takealot_ops.settings import DashboardSettings, SettingsError
from takealot_ops.storage.repository import Repository


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
                sale_status_rules_path=settings.project_root
                / "config"
                / "sale_status_rules.yaml",
            )
            dataset = service.dashboard_dataset(as_of)
    except (OSError, ValueError, SQLAlchemyError) as exc:
        return None, f"本地数据暂不可用：{exc}"
    finally:
        if engine is not None:
            engine.dispose()
    return _dataset_as_of(dataset, as_of), None


def create_read_only_engine(database_url: str) -> Engine:
    """Create an engine whose dashboard connections reject write statements."""
    engine = create_engine(database_url)
    if engine.url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_query_only(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only=ON")
            cursor.close()

    else:

        @event.listens_for(engine, "begin")
        def _set_transaction_read_only(connection: Connection) -> None:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    return engine


def main() -> None:
    """Render the six-page local dashboard without collection credentials."""
    st.set_page_config(page_title="Takealot 运营看板", page_icon="📊", layout="wide")
    project_root = Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd())).resolve()
    try:
        settings = DashboardSettings.from_env(project_root)
    except SettingsError as exc:
        st.title("本地配置不可用")
        st.error(str(exc))
        st.info("请修正本地数据库、主机或端口环境变量；浏览看板无需设置 API Key。")
        return

    with st.sidebar:
        st.header("Takealot 运营看板")
        page_name = st.radio("页面", PAGE_NAMES)
        as_of = st.date_input("数据截止日期", value=date.today())
        st.caption("本地只读 · 不调用 Takealot API")

    dataset, load_error = load_dashboard_dataset(settings, as_of)
    renderers: dict[str, Callable[[DashboardDataset | None, str | None, DashboardSettings, date], None]] = {
        "店铺总览": _render_overview,
        "单品分析": _render_product,
        "经营四象限": _render_quadrants,
        "异常商品": _render_anomalies,
        "数据质量": _render_quality,
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
    metrics[1].metric("最新可用日下单金额", _currency_or_missing(_sum_numeric(latest, "ordered_revenue")))
    metrics[2].metric("近7日下单件数", _sum_value(recent, "ordered_units", integer=True))
    metrics[3].metric("异常商品数", _unique_count(dataset.anomalies, "offer_id"))
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
    st.plotly_chart(build_store_sales_figure(store.tail(30)), width="stretch")
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
        _empty_state("暂无单品指标", "采集并计算指标后，可按 SKU、Offer ID、TSIN、条码或名称搜索。")
        return
    query = st.text_input("搜索商品", placeholder="输入 SKU、Offer ID、TSIN、条码或商品名称")
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
                f"SKU: {_display(identity.get('sku', latest.get('sku')))}",
                f"Offer ID: {selected_offer}",
                f"TSIN: {_display(identity.get('tsin_id'))}",
                f"条码: {_display(identity.get('barcode'))}",
            ]
        )
    )
    identity_columns = st.columns(4)
    identity_columns[0].metric("当前售价", _currency_or_missing(identity.get("selling_price")))
    identity_columns[1].metric("RRP", _currency_or_missing(identity.get("rrp")))
    identity_columns[2].metric("总可见库存", _display(identity.get("total_stock")))
    identity_columns[3].metric("Offer 状态", _display(identity.get("status")))
    metric_columns = st.columns(4)
    latest_metric_date = _display(latest.get("metric_date"))
    st.caption(f"当前商品最新可用指标日：{latest_metric_date}")
    metric_columns[0].metric(
        "最新可用日下单件数", _number_or_missing(latest.get("ordered_units"))
    )
    metric_columns[1].metric(
        "近7日下单件数", _number_or_missing(_tail_sum(history, "ordered_units", 7))
    )
    metric_columns[2].metric(
        "近30天浏览量", _number_or_missing(latest.get("page_views_30_days"))
    )
    metric_columns[3].metric(
        "近30天转化率", _percentage_or_missing(latest.get("conversion_percentage_30_days"))
    )
    st.plotly_chart(build_traffic_figure(history), width="stretch")
    st.plotly_chart(build_sales_figure(history), width="stretch")
    _effective_units_notice(settings.project_root)


def _render_quadrants(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del settings
    st.title("经营四象限")
    st.caption("使用近30天浏览量与下单件数的分位数边界；缺失任一指标时明确列为未分类。")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    latest = _latest_rows(dataset.product_daily, as_of)
    if latest.empty:
        _empty_state("暂无可分类商品", "需要同一截止日期下的商品指标后才能计算四象限。")
        return
    percentile = st.select_slider("分位数边界", options=[25, 50, 75], value=50)
    classified = classify_quadrants(latest, percentile=percentile)
    classified["quadrant"] = classified["quadrant"].fillna("unclassified")
    counts = classified["quadrant"].value_counts()
    columns = st.columns(5)
    for column, (key, label) in zip(columns, QUADRANT_LABELS.items(), strict=True):
        column.metric(label, int(counts.get(key, 0)))
    st.plotly_chart(build_quadrant_figure(classified), width="stretch")
    display = classified.copy()
    display["quadrant"] = display["quadrant"].map(QUADRANT_LABELS).fillna("未分类")
    _dataframe(display, ["offer_id", "sku", "page_views_30_days", "ordered_units", "quadrant"])


def _render_anomalies(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del settings
    st.title("异常商品")
    st.caption(f"仅显示截至 {as_of.isoformat()} 的已计算异常，不在页面修改规则。")
    if not _require_dataset(dataset, load_error):
        return
    assert dataset is not None
    anomalies = filter_as_of(dataset.anomalies, as_of, "event_date")
    if anomalies.empty:
        _empty_state("当前没有异常记录", "可能尚未计算异常，或所选截止日期前没有触发规则。")
        return
    anomaly_types = sorted(anomalies["anomaly_type"].dropna().astype(str).unique())
    selected = st.multiselect(
        "异常类型",
        anomaly_types,
        default=anomaly_types,
        format_func=lambda value: ANOMALY_LABELS.get(value, value),
    )
    filtered = anomalies.loc[anomalies["anomaly_type"].isin(selected)].copy()
    filtered["anomaly_type"] = filtered["anomaly_type"].map(ANOMALY_LABELS).fillna(
        filtered["anomaly_type"]
    )
    st.metric("异常商品数", _unique_count(filtered, "offer_id"))
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
    _dataframe(events, ["event_date", "event_type", "severity", "offer_id", "details"])


def _render_exports(
    dataset: DashboardDataset | None,
    load_error: str | None,
    settings: DashboardSettings,
    as_of: date,
) -> None:
    del dataset, load_error
    st.title("导出中心")
    st.caption("这里只检查现有日报，不生成、不刷新，也不调用 API。")
    export_root = settings.project_root / "exports"
    partition = export_root / as_of.isoformat()
    basename = f"Takealot运营日报_{as_of.isoformat()}"
    rows = []
    for label, suffix in (("离线 HTML", ".html"), ("Excel", ".xlsx"), ("PNG", ".png")):
        path = partition / f"{basename}{suffix}"
        rows.append(
            {
                "格式": label,
                "状态": "已生成" if path.is_file() else "未生成",
                "路径": str(path.relative_to(settings.project_root)),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    if any(row["状态"] == "已生成" for row in rows):
        st.success("已找到部分或全部日报文件，可从上方本地路径打开。")
    else:
        st.info("所选日期暂无日报。请在命令行运行既有导出工作流；看板不会写入数据或生成文件。")


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
    st.info("请确认数据库已由采集/指标任务创建，且 config 下的规则文件完整。无需为浏览看板设置 API Key。")
    return False


def _empty_state(title: str, guidance: str) -> None:
    st.info(f"{title}。{guidance}")


def _dataframe(frame: pd.DataFrame, columns: list[str]) -> None:
    available = [column for column in columns if column in frame.columns]
    display = frame.loc[:, available].rename(columns=FIELD_LABELS)
    st.dataframe(display, width="stretch", hide_index=True)


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


def _tail_sum(frame: pd.DataFrame, column: str, days: int) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    values = pd.to_numeric(frame.tail(days)[column], errors="coerce")
    return None if values.notna().sum() == 0 else float(values.sum())


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
