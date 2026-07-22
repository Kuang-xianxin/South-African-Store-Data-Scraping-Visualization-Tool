"""Plotly figure construction for the Streamlit dashboard."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]

from takealot_ops.dashboard.labels import QUADRANT_LABELS, TRAFFIC_METRIC_LABELS


_DATE_COLUMN = "metric_date"


def build_sales_figure(product_daily: pd.DataFrame) -> go.Figure:
    """Build daily ordered-unit bars and a same-axis seven-day rolling average."""
    figure = go.Figure()
    if not _has_columns(product_daily, (_DATE_COLUMN, "ordered_units")):
        return _empty_figure(figure, "暂无销售数据")
    frame = product_daily.copy()
    frame[_DATE_COLUMN] = pd.to_datetime(frame[_DATE_COLUMN], errors="coerce")
    frame["ordered_units"] = pd.to_numeric(frame["ordered_units"], errors="coerce")
    frame = frame.sort_values(_DATE_COLUMN)
    rolling = frame["ordered_units"].rolling(window=7, min_periods=1).mean()
    rolling = rolling.where(frame["ordered_units"].notna())
    figure.add_bar(
        x=frame[_DATE_COLUMN],
        y=frame["ordered_units"],
        name="每日下单件数",
        marker_color="#2563EB",
        hovertemplate="%{x|%Y-%m-%d}<br>下单件数：%{y}<extra></extra>",
    )
    figure.add_scatter(
        x=frame[_DATE_COLUMN],
        y=rolling,
        name="7日移动平均",
        mode="lines+markers",
        connectgaps=False,
        line={"color": "#F59E0B", "width": 3},
        hovertemplate="%{x|%Y-%m-%d}<br>7日移动平均：%{y:.1f}<extra></extra>",
    )
    return _finish_figure(figure, "下单件数趋势", "下单件数")


def build_traffic_figure(product_daily: pd.DataFrame) -> go.Figure:
    """Build saved 30-day traffic snapshot trends without inventing daily traffic."""
    figure = go.Figure()
    columns = (_DATE_COLUMN, *TRAFFIC_METRIC_LABELS)
    if not _has_columns(product_daily, columns):
        return _empty_figure(figure, "暂无流量快照数据")
    frame = product_daily.copy()
    frame[_DATE_COLUMN] = pd.to_datetime(frame[_DATE_COLUMN], errors="coerce")
    frame = frame.sort_values(_DATE_COLUMN)
    colors = ("#0F766E", "#7C3AED", "#DC2626")
    for (field_name, label), color in zip(
        TRAFFIC_METRIC_LABELS.items(), colors, strict=True
    ):
        values = pd.to_numeric(frame[field_name], errors="coerce")
        figure.add_scatter(
            x=frame[_DATE_COLUMN],
            y=values,
            name=label,
            mode="lines+markers",
            connectgaps=False,
            line={"color": color, "width": 2},
            hovertemplate=f"%{{x|%Y-%m-%d}}<br>{label}：%{{y}}<extra></extra>",
        )
    return _finish_figure(figure, "保存的30天流量快照趋势", "快照值")


def build_store_sales_figure(store_daily: pd.DataFrame) -> go.Figure:
    """Build the store-level ordered-unit trend using the shared sales style."""
    return build_sales_figure(store_daily)


def build_quadrant_figure(classified: pd.DataFrame) -> go.Figure:
    """Build the traffic-versus-sales quadrant scatter from classified metrics."""
    figure = go.Figure()
    required = ("page_views_30_days", "ordered_units", "quadrant", "offer_id")
    if not _has_columns(classified, required):
        return _empty_figure(figure, "暂无可分类商品")
    colors: Mapping[str, str] = {
        "star": "#16A34A",
        "conversion_issue": "#DC2626",
        "potential": "#2563EB",
        "optimize": "#F59E0B",
        "unclassified": "#64748B",
    }
    for quadrant, label in QUADRANT_LABELS.items():
        rows = classified[classified["quadrant"] == quadrant]
        if rows.empty:
            continue
        figure.add_scatter(
            x=pd.to_numeric(rows["page_views_30_days"], errors="coerce"),
            y=pd.to_numeric(rows["ordered_units"], errors="coerce"),
            text=rows["offer_id"].astype(str),
            name=label,
            mode="markers",
            marker={"color": colors[quadrant], "size": 12, "opacity": 0.85},
            hovertemplate=(
                "商品编号：%{text}<br>近30天浏览量：%{x}<br>下单件数：%{y}<extra></extra>"
            ),
        )
    view_boundary = classified.attrs.get("page_views_boundary")
    unit_boundary = classified.attrs.get("ordered_units_boundary")
    if view_boundary is not None and pd.notna(view_boundary):
        figure.add_vline(x=float(view_boundary), line_dash="dash", line_color="#94A3B8")
    if unit_boundary is not None and pd.notna(unit_boundary):
        figure.add_hline(y=float(unit_boundary), line_dash="dash", line_color="#94A3B8")
    return _finish_figure(
        figure, "商品经营四象限", "下单件数", x_title="近30天浏览量"
    )


def _has_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> bool:
    return not frame.empty and all(column in frame.columns for column in columns)


def _empty_figure(figure: go.Figure, message: str) -> go.Figure:
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": "#64748B", "size": 16},
    )
    figure.update_layout(
        height=360,
        margin={"l": 30, "r": 20, "t": 30, "b": 30},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return figure


def _finish_figure(
    figure: go.Figure, title: str, y_title: str, *, x_title: str = "日期"
) -> go.Figure:
    figure.update_layout(
        title={"text": title, "font": {"size": 18}},
        height=410,
        margin={"l": 40, "r": 24, "t": 58, "b": 42},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 0},
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis={"title": x_title, "gridcolor": "#E2E8F0"},
        yaxis={"title": y_title, "gridcolor": "#E2E8F0", "rangemode": "tozero"},
    )
    return figure
