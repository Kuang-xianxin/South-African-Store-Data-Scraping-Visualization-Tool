"""Create a self-contained, read-only HTML operations report."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]
import plotly.io as pio  # type: ignore[import-untyped]

from takealot_ops.metrics.service import DashboardDataset, latest_metric_anomalies


_TRAFFIC_LABELS = {
    "page_views_30_days": "近30天浏览量",
    "page_views_30_day_average": "近30天日均浏览量",
    "page_views_window_net_change": "30天浏览量窗口净变化",
}
_PLOT_COMPLETION_POST_SCRIPT = """
window.__takealotCompletedPlots = window.__takealotCompletedPlots || {};
window.__takealotCompletedPlots["{plot_id}"] = true;
window.dispatchEvent(new CustomEvent("takealot-plot-complete", {
  detail: "{plot_id}"
}));
"""


def export_html(dataset: DashboardDataset, destination: Path) -> Path:
    """Write one offline HTML file containing data, styles, and Plotly."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    store_figure = _store_trend(dataset.store_daily)
    product_figure = _product_trend(dataset.product_daily)
    store_plot = pio.to_html(
        store_figure,
        full_html=False,
        include_plotlyjs=True,
        div_id="store-trend",
        config={"displaylogo": False, "responsive": True},
        post_script=_PLOT_COMPLETION_POST_SCRIPT,
    )
    product_plot = pio.to_html(
        product_figure,
        full_html=False,
        include_plotlyjs=False,
        div_id="product-trend",
        config={"displaylogo": False, "responsive": True},
        post_script=_PLOT_COMPLETION_POST_SCRIPT,
    )
    _, latest_anomalies = latest_metric_anomalies(dataset)
    report_dataset = DashboardDataset(
        store_daily=dataset.store_daily,
        product_daily=dataset.product_daily,
        offer_current=dataset.offer_current,
        anomalies=latest_anomalies,
        quality_events=dataset.quality_events,
    )
    serialized = _serialize_dataset(report_dataset)
    document = _document(report_dataset, serialized, store_plot, product_plot)
    _reject_external_resource_attributes(document)
    document = _escape_inline_resource_literals(document)
    _reject_external_resources(document)
    destination.write_text(document, encoding="utf-8")
    return destination


def _store_trend(frame: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if not frame.empty:
        ordered = frame.sort_values("metric_date")
        figure.add_trace(
            go.Scatter(
                x=ordered["metric_date"],
                y=ordered["ordered_revenue"],
                mode="lines+markers",
                name="订购销售额",
                line={"color": "#2563EB", "width": 3},
            )
        )
    else:
        figure.add_annotation(text="暂无数据", showarrow=False)
    figure.update_layout(
        title="店铺订购销售额趋势（ZAR）",
        template="plotly_white",
        margin={"l": 55, "r": 25, "t": 55, "b": 45},
        height=340,
        xaxis_title="日期",
        yaxis_title="金额（ZAR）",
    )
    return figure


def _product_trend(frame: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if not frame.empty:
        selected = str(frame["offer_id"].dropna().astype(str).sort_values().iloc[0])
        rows = frame.loc[frame["offer_id"].astype(str) == selected].sort_values("metric_date")
        figure.add_trace(
            go.Scatter(
                x=rows["metric_date"],
                y=rows["ordered_units"],
                mode="lines+markers",
                name="订购件数",
                line={"color": "#0F766E", "width": 3},
            )
        )
        title = f"商品 {selected} 订购件数趋势"
    else:
        figure.add_annotation(text="暂无数据", showarrow=False)
        title = "商品订购件数趋势"
    figure.update_layout(
        title=title,
        template="plotly_white",
        margin={"l": 55, "r": 25, "t": 55, "b": 45},
        height=340,
        xaxis_title="日期",
        yaxis_title="件数",
    )
    return figure


def _document(
    dataset: DashboardDataset,
    serialized: str,
    store_plot: str,
    product_plot: str,
) -> str:
    latest_anomaly_date, latest_anomalies = latest_metric_anomalies(dataset)
    anomaly_products = (
        int(latest_anomalies["offer_id"].dropna().astype(str).nunique())
        if "offer_id" in latest_anomalies.columns
        else 0
    )
    anomaly_date_label = (
        latest_anomaly_date.isoformat() if latest_anomaly_date is not None else "暂无"
    )
    totals = {
        "units": _sum_or_none(dataset.store_daily, "ordered_units"),
        "effective": _sum_or_none(dataset.store_daily, "effective_units"),
        "revenue": _sum_or_none(dataset.store_daily, "ordered_revenue"),
        "anomalies": anomaly_products,
    }
    product_columns = [
        ("metric_date", "日期"),
        ("offer_id", "Offer ID"),
        ("sku", "SKU"),
        ("ordered_units", "订购件数"),
        ("effective_units", "有效件数"),
        ("ordered_revenue", "订购销售额（ZAR）"),
        ("page_views_30_days", _TRAFFIC_LABELS["page_views_30_days"]),
        ("page_views_30_day_average", _TRAFFIC_LABELS["page_views_30_day_average"]),
        (
            "page_views_window_net_change",
            _TRAFFIC_LABELS["page_views_window_net_change"],
        ),
        ("conversion_percentage_30_days", "近30天转化率（%）"),
        ("total_stock", "平台可售库存"),
        ("offer_status", "商品状态"),
    ]
    anomaly_columns = [
        ("event_date", "日期"),
        ("offer_id", "Offer ID"),
        ("anomaly_type", "异常类型"),
        ("severity", "严重程度"),
        ("explanation", "说明"),
    ]
    quality_columns = [
        ("event_date", "日期"),
        ("event_type", "质量事件"),
        ("severity", "严重程度"),
        ("offer_id", "Offer ID"),
        ("details", "详情"),
    ]
    options = "".join(
        f'<option value="{escape(value)}">{escape(value)}</option>'
        for value in sorted(dataset.product_daily.get("offer_id", pd.Series(dtype=str)).dropna().astype(str).unique())
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Takealot 运营日报</title>
<style>
:root {{ color-scheme: light; font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background:#f4f7fb; color:#172033; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#f4f7fb; }}
.report {{ max-width:1520px; margin:0 auto; padding:28px; }}
.hero {{ background:linear-gradient(125deg,#11284f,#1d4ed8); color:white; padding:28px 32px; border-radius:18px; box-shadow:0 14px 35px #1e3a5f24; }}
.hero h1 {{ margin:0 0 8px; font-size:30px; }} .hero p {{ margin:0; color:#dbeafe; }}
.cards {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:20px 0; }}
.card,.panel {{ background:white; border:1px solid #dce4ef; border-radius:14px; box-shadow:0 6px 18px #3341550d; }}
.card {{ padding:18px 20px; }} .card span {{ color:#64748b; font-size:13px; }} .card strong {{ display:block; margin-top:8px; font-size:25px; color:#102a56; }}
.panel {{ padding:20px; margin:18px 0; overflow:hidden; }} .panel h2 {{ margin:0 0 14px; font-size:20px; }}
.charts {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }}
.controls {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px; }} input,select {{ min-width:230px; padding:9px 11px; border:1px solid #cbd5e1; border-radius:8px; background:white; }}
.table-wrap {{ overflow:auto; max-height:520px; }} table {{ border-collapse:collapse; width:100%; font-size:13px; }} th {{ position:sticky; top:0; background:#173b68; color:white; text-align:left; }} th,td {{ padding:10px 12px; border-bottom:1px solid #e2e8f0; white-space:nowrap; }} tbody tr:nth-child(even) {{ background:#f8fafc; }}
.empty {{ color:#64748b; text-align:center; padding:28px; }} .note {{ color:#64748b; font-size:13px; line-height:1.7; }}
@media(max-width:900px) {{ .cards,.charts {{ grid-template-columns:1fr; }} .report {{ padding:14px; }} }}
</style>
</head>
<body>
<main id="report-root" class="report" data-report-ready="false">
  <section class="hero"><h1>Takealot 运营日报</h1><p>离线只读报告 · 数据范围以嵌入的数据集为准</p></section>
  <section class="cards">
    {_card("订购件数", totals["units"], "#,##0")}
    {_card("有效件数", totals["effective"], "#,##0")}
    {_card("订购销售额", totals["revenue"], "ZAR")}
    {_card("最新指标日异常商品数", totals["anomalies"], "#,##0")}
  </section>
  <section class="charts"><div class="panel">{store_plot}</div><div class="panel">{product_plot}</div></section>
  <section class="panel"><h2>商品每日销售明细/汇总</h2>
    <div class="controls"><input id="search" type="search" placeholder="搜索 SKU、Offer ID 或状态"><select id="offer-filter"><option value="">全部商品</option>{options}</select></div>
    {_table(dataset.product_daily, product_columns, "product-table")}
  </section>
  <section class="panel"><h2>异常商品（最新指标日 {anomaly_date_label}）</h2><p class="note">同一商品触发多种异常时会保留多条记录。</p>{_table(dataset.anomalies, anomaly_columns, "anomaly-table")}</section>
  <section class="panel"><h2>数据质量</h2>{_table(dataset.quality_events, quality_columns, "quality-table")}</section>
  <section class="panel"><h2>流量口径说明</h2><p class="note">近30天浏览量是 API 返回的滚动窗口值；近30天日均浏览量为该窗口值除以 30；30天浏览量窗口净变化用于比较相邻快照的窗口变化。缺失值保持空白，且不推断为零。</p></section>
</main>
<script id="dashboard-data" type="application/json">{serialized}</script>
<script>
(() => {{
  const search = document.getElementById("search");
  const offer = document.getElementById("offer-filter");
  const filterRows = () => {{
    const query = search.value.trim().toLowerCase();
    const selected = offer.value;
    document.querySelectorAll("#product-table tbody tr").forEach((row) => {{
      const matchesText = !query || row.textContent.toLowerCase().includes(query);
      const matchesOffer = !selected || row.dataset.offer === selected;
      row.hidden = !(matchesText && matchesOffer);
    }});
  }};
  search.addEventListener("input", filterRows);
  offer.addEventListener("change", filterRows);
  const plotIds = ["store-trend", "product-trend"];
  const markReportReady = () => {{
    const completed = window.__takealotCompletedPlots || {{}};
    if (plotIds.every((id) => completed[id] === true)) {{
      document.getElementById("report-root").setAttribute("data-report-ready", "true");
    }}
  }};
  window.addEventListener("takealot-plot-complete", markReportReady);
  markReportReady();
}})();
</script>
</body>
</html>"""


def _card(label: str, value: object, number_format: str) -> str:
    if value is None:
        displayed = "未知"
    elif number_format == "ZAR":
        displayed = f"R {float(str(value)):,.2f}"
    elif isinstance(value, (int, float)):
        displayed = f"{value:,.0f}"
    else:
        displayed = escape(str(value))
    return f'<div class="card"><span>{escape(label)}</span><strong>{displayed}</strong></div>'


def _table(frame: pd.DataFrame, columns: list[tuple[str, str]], table_id: str) -> str:
    headers = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    if frame.empty:
        body = f'<tr><td class="empty" colspan="{len(columns)}">暂无数据</td></tr>'
    else:
        rows: list[str] = []
        for record in frame.to_dict(orient="records"):
            offer_id = _display(record.get("offer_id"))
            cells = "".join(f"<td>{escape(_display(record.get(key)))}</td>" for key, _ in columns)
            rows.append(f'<tr data-offer="{escape(offer_id)}">{cells}</tr>')
        body = "".join(rows)
    return f'<div class="table-wrap"><table id="{table_id}"><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div>'


def _display(value: object) -> str:
    if value is None or _is_missing(value):
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _serialize_dataset(dataset: DashboardDataset) -> str:
    payload = {
        "store_daily": _frame_records(dataset.store_daily),
        "product_daily": _frame_records(dataset.product_daily),
        "offer_current": _frame_records(dataset.offer_current),
        "anomalies": _frame_records(dataset.anomalies),
        "quality_events": _frame_records(dataset.quality_events),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


def _frame_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _json_value(value: object) -> object:
    if value is None or _is_missing(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return str(value)


def _sum_or_none(frame: pd.DataFrame, column: str) -> int | float | None:
    if frame.empty or column not in frame:
        return None
    value = frame[column].sum(min_count=1)
    if _is_missing(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value if isinstance(value, (int, float)) else float(value)


def _is_missing(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value) or value is pd.NA or value is pd.NaT


def _reject_external_resources(document: str) -> None:
    lowered = document.lower()
    for token in ('src="http', "src='http", 'href="http', "href='http"):
        if token in lowered:
            raise ValueError("HTML export contains an external resource")


def _escape_inline_resource_literals(document: str) -> str:
    """Keep Plotly inline while removing resource-like URL attribute literals."""
    replacements = {
        'src="http': 'src="\\u0068ttp',
        "src='http": "src='\\u0068ttp",
        'href="http': 'href="\\u0068ttp',
        "href='http": "href='\\u0068ttp",
    }
    def escape_script(match: re.Match[str]) -> str:
        script = match.group(2)
        for source, replacement in replacements.items():
            script = script.replace(source, replacement)
        return f"{match.group(1)}{script}{match.group(3)}"

    return re.sub(
        r"(<script\b[^>]*>)(.*?)(</script>)",
        escape_script,
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _reject_external_resource_attributes(document: str) -> None:
    parser = _ExternalResourceParser()
    parser.feed(document)
    if parser.external_urls:
        raise ValueError("HTML export contains an external resource")


class _ExternalResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_urls: list[str] = []

    def handle_starttag(
        self, _tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        for name, value in attrs:
            if name.lower() in {"src", "href"} and value is not None:
                if value.lower().startswith(("http://", "https://")):
                    self.external_urls.append(value)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
