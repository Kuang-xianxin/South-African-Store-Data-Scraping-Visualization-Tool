"""Read-only ERP projections built from the canonical metric dataset."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.dashboard.labels import (
    ANOMALY_EXPLANATIONS,
    ANOMALY_LABELS,
    EVENT_LABELS,
    OFFER_STATUS_LABELS,
    SEVERITY_LABELS,
)
from takealot_ops.metrics.service import (
    DashboardDataset,
    MetricService,
    build_quadrant_window,
    classify_quadrants,
    latest_metric_anomalies,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_read_only_engine
from takealot_ops.storage.repository import Repository


EMPTY_DATASET = DashboardDataset(
    store_daily=pd.DataFrame(),
    product_daily=pd.DataFrame(),
    offer_current=pd.DataFrame(),
    anomalies=pd.DataFrame(),
    quality_events=pd.DataFrame(),
)
SAST = ZoneInfo("Africa/Johannesburg")
CHINA = ZoneInfo("Asia/Shanghai")


def create_read_only_erp_engine(database_url: str) -> Engine:
    """Create a read-only engine for the ERP's configured database."""
    return create_read_only_engine(database_url)


def load_erp_dataset(
    settings: DashboardSettings,
    as_of: date,
) -> DashboardDataset:
    """Load the canonical dashboard dataset without creating a database."""
    database_path = sqlite_database_path(settings.database_url)
    if database_path is not None and not database_path.exists():
        return EMPTY_DATASET
    engine = create_read_only_erp_engine(settings.database_url)
    try:
        with Session(engine) as session:
            service = MetricService(
                Repository(session),
                anomaly_rules_path=settings.project_root / "config" / "anomaly_rules.yaml",
                sale_status_rules_path=settings.project_root
                / "config"
                / "sale_status_rules.yaml",
            )
            return service.dashboard_dataset(as_of)
    except SQLAlchemyError:
        return EMPTY_DATASET
    finally:
        engine.dispose()


def build_summary_payload(dataset: DashboardDataset, as_of: date) -> dict[str, Any]:
    """Build store KPIs, 30-day trend, and leading products."""
    latest = latest_rows(dataset.product_daily, as_of)
    latest_date = latest_metric_date(latest)
    recent_start = as_of - timedelta(days=6)
    store = filter_as_of(dataset.store_daily, as_of, "metric_date")
    if "metric_date" in store.columns:
        store = store.sort_values("metric_date")
    recent_dates = (
        pd.to_datetime(store["metric_date"], errors="coerce").dt.date
        if "metric_date" in store.columns
        else pd.Series(dtype="object")
    )
    recent = store.loc[recent_dates >= recent_start] if not store.empty else store
    _, latest_anomalies = latest_metric_anomalies(dataset)
    sold = (
        latest.loc[pd.to_numeric(latest["ordered_units"], errors="coerce") > 0]
        if "ordered_units" in latest.columns
        else latest.iloc[0:0]
    )
    stock = (
        pd.to_numeric(latest["total_stock"], errors="coerce")
        if "total_stock" in latest.columns
        else pd.Series(dtype="float64")
    )
    products = _enrich_products(latest, dataset.offer_current)
    if "ordered_units" in products.columns:
        products = products.sort_values("ordered_units", ascending=False).head(12)
    return {
        "as_of": as_of.isoformat(),
        "latest_metric_date": latest_date,
        "kpis": {
            "latest_ordered_units": numeric_sum(latest, "ordered_units", integer=True),
            "latest_ordered_revenue": numeric_sum(latest, "ordered_revenue"),
            "seven_day_ordered_units": numeric_sum(recent, "ordered_units", integer=True),
            "latest_anomaly_products": unique_count(latest_anomalies, "offer_id"),
            "page_views_30_days": numeric_sum(
                latest, "page_views_30_days", integer=True
            ),
            "median_conversion": numeric_median(
                latest, "conversion_percentage_30_days"
            ),
            "selling_products": unique_count(sold, "offer_id"),
            "stockout_products": int((stock == 0).sum()),
        },
        "sales_series": frame_records(store.tail(30)),
        "top_products": frame_records(products),
    }


def build_products_payload(dataset: DashboardDataset, as_of: date) -> dict[str, Any]:
    """Return latest product rows enriched with searchable identities."""
    latest = latest_rows(dataset.product_daily, as_of)
    products = _enrich_products(latest, dataset.offer_current)
    if not products.empty and "ordered_units" in products.columns:
        products = products.sort_values(
            ["ordered_units", "page_views_30_days"],
            ascending=[False, False],
            na_position="last",
        )
    return {
        "latest_metric_date": latest_metric_date(latest),
        "items": frame_records(products),
    }


def build_product_detail_payload(
    dataset: DashboardDataset,
    as_of: date,
    offer_id: str,
) -> dict[str, Any]:
    """Return one product identity, KPIs, and metric history."""
    history = filter_as_of(dataset.product_daily, as_of, "metric_date")
    if history.empty or "offer_id" not in history.columns:
        return {"identity": {}, "kpis": {}, "history": []}
    history = history.loc[history["offer_id"].astype(str) == offer_id].sort_values(
        "metric_date"
    )
    identity_frame = dataset.offer_current
    identity: dict[str, Any] = {}
    if not identity_frame.empty and "offer_id" in identity_frame.columns:
        matched = identity_frame.loc[identity_frame["offer_id"].astype(str) == offer_id]
        if not matched.empty:
            identity = sanitize_record(matched.iloc[-1].to_dict())
    if history.empty:
        return {"identity": identity, "kpis": {}, "history": []}
    latest = history.iloc[-1]
    history_dates = pd.to_datetime(history["metric_date"], errors="coerce").dt.date
    window_start = history_dates.max() - timedelta(days=6)
    window = history.loc[history_dates >= window_start]
    return {
        "identity": identity,
        "kpis": {
            "latest_metric_date": sanitize_value(latest.get("metric_date")),
            "latest_ordered_units": sanitize_value(latest.get("ordered_units")),
            "seven_day_ordered_units": numeric_sum(
                window, "ordered_units", integer=True
            ),
            "page_views_30_days": sanitize_value(latest.get("page_views_30_days")),
            "conversion_percentage_30_days": sanitize_value(
                latest.get("conversion_percentage_30_days")
            ),
        },
        "history": frame_records(history),
    }


def build_quadrant_payload(
    dataset: DashboardDataset,
    as_of: date,
    percentile: int,
) -> dict[str, Any]:
    """Return canonical seven-day quadrant classification and boundaries."""
    window = build_quadrant_window(dataset.product_daily, as_of, days=7)
    classified = classify_quadrants(window, percentile=percentile)
    boundaries = dict(classified.attrs)
    classified = _enrich_products(classified, dataset.offer_current)
    operational_context = _quadrant_operational_context(
        dataset.product_daily,
        dataset.offer_current,
        as_of,
        dataset.offer_history,
    )
    if not operational_context.empty:
        classified = classified.merge(
            operational_context,
            on="offer_id",
            how="left",
        )
    counts = (
        classified["quadrant"].value_counts().to_dict()
        if "quadrant" in classified.columns
        else {}
    )
    return {
        "window_start": sanitize_value(window.attrs.get("window_start")),
        "window_end": sanitize_value(window.attrs.get("window_end")),
        "percentile": percentile,
        "boundaries": {
            "page_views": sanitize_value(
                boundaries.get("page_views_boundary")
            ),
            "ordered_units": sanitize_value(
                boundaries.get("ordered_units_boundary")
            ),
            "page_views_rank": sanitize_value(
                boundaries.get("page_views_rank_boundary")
            ),
            "ordered_units_rank": sanitize_value(
                boundaries.get("ordered_units_rank_boundary")
            ),
        },
        "counts": {
            "star": int(counts.get("star", 0)),
            "conversion_issue": int(counts.get("conversion_issue", 0)),
            "potential": int(counts.get("potential", 0)),
            "optimize": int(counts.get("optimize", 0)),
            "unclassified": int(counts.get("unclassified", 0)),
        },
        "items": frame_records(classified),
    }


def build_risk_payload(dataset: DashboardDataset, as_of: date) -> dict[str, Any]:
    """Return localized anomaly and quality-event records."""
    anomalies = filter_as_of(dataset.anomalies, as_of, "event_date").copy()
    quality = filter_as_of(dataset.quality_events, as_of, "event_date").copy()
    latest_source = DashboardDataset(
        store_daily=dataset.store_daily,
        product_daily=filter_as_of(dataset.product_daily, as_of, "metric_date"),
        offer_current=dataset.offer_current,
        anomalies=anomalies,
        quality_events=quality,
    )
    latest_date, latest = latest_metric_anomalies(latest_source)
    if not anomalies.empty:
        anomalies["anomaly_label"] = (
            anomalies["anomaly_type"].map(ANOMALY_LABELS).fillna("未知异常")
        )
        anomalies["explanation"] = (
            anomalies["anomaly_type"]
            .map(ANOMALY_EXPLANATIONS)
            .fillna("暂无中文说明")
        )
        anomalies["severity_label"] = (
            anomalies["severity"].map(SEVERITY_LABELS).fillna("未知")
        )
    if not latest.empty:
        latest = latest.copy()
        latest["anomaly_label"] = (
            latest["anomaly_type"].map(ANOMALY_LABELS).fillna("未知异常")
        )
        latest["explanation"] = (
            latest["anomaly_type"]
            .map(ANOMALY_EXPLANATIONS)
            .fillna("暂无中文说明")
        )
        latest["severity_label"] = (
            latest["severity"].map(SEVERITY_LABELS).fillna("未知")
        )
    product_context = _risk_product_context(anomalies, dataset, as_of)
    if not product_context.empty:
        anomalies = anomalies.merge(product_context, on="offer_id", how="left")
        latest = latest.merge(product_context, on="offer_id", how="left")
    if not quality.empty:
        quality["event_label"] = (
            quality["event_type"].map(EVENT_LABELS).fillna("未知质量事件")
        )
        quality["severity_label"] = (
            quality["severity"].map(SEVERITY_LABELS).fillna("未知")
        )
        quality["details_text"] = quality["details"].map(_details_text)
    return {
        "latest_metric_date": sanitize_value(latest_date),
        "latest_anomalies": frame_records(latest),
        "anomalies": frame_records(anomalies),
        "quality_events": frame_records(quality),
        "summary": {
            "latest_anomaly_products": unique_count(latest, "offer_id"),
            "latest_anomaly_records": len(latest),
            "quality_events": len(quality),
            "unknown_sale_status": (
                int((quality["event_type"] == "unknown_sale_status").sum())
                if "event_type" in quality.columns
                else 0
            ),
        },
    }


def frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a DataFrame into JSON-safe records."""
    return [sanitize_record(record) for record in frame.to_dict(orient="records")]


def sanitize_record(record: dict[Any, Any]) -> dict[str, Any]:
    return {str(key): sanitize_value(value) for key, value in record.items()}


def sanitize_value(value: object) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def filter_as_of(frame: pd.DataFrame, as_of: date, column: str) -> pd.DataFrame:
    if frame.empty or column not in frame.columns:
        return frame.copy()
    values = pd.to_datetime(frame[column], errors="coerce").dt.date
    return frame.loc[values <= as_of].copy()


def latest_rows(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    scoped = filter_as_of(frame, as_of, "metric_date")
    if scoped.empty or "metric_date" not in scoped.columns:
        return scoped
    dates = pd.to_datetime(scoped["metric_date"], errors="coerce").dt.date
    valid = dates.dropna()
    if valid.empty:
        return scoped.iloc[0:0].copy()
    return scoped.loc[dates == valid.max()].copy()


def latest_metric_date(frame: pd.DataFrame) -> str | None:
    if frame.empty or "metric_date" not in frame.columns:
        return None
    values = pd.to_datetime(frame["metric_date"], errors="coerce").dt.date.dropna()
    return values.max().isoformat() if not values.empty else None


def numeric_sum(
    frame: pd.DataFrame,
    column: str,
    *,
    integer: bool = False,
) -> int | float | None:
    if frame.empty or column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    total = float(values.sum())
    return int(total) if integer else total


def numeric_median(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.median()) if not values.empty else None


def unique_count(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].dropna().astype(str).nunique())


def sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


def _enrich_products(
    metrics: pd.DataFrame,
    offers: pd.DataFrame,
) -> pd.DataFrame:
    if metrics.empty or "offer_id" not in metrics.columns:
        return metrics.copy()
    identity_columns = [
        "offer_id",
        "title",
        "sku",
        "tsin_id",
        "barcode",
        "selling_price",
        "rrp",
        "status",
        "image_url",
    ]
    if offers.empty or "offer_id" not in offers.columns:
        identity = pd.DataFrame(columns=identity_columns)
    else:
        available = [column for column in identity_columns if column in offers.columns]
        identity = offers.loc[:, available].drop_duplicates("offer_id", keep="last")
    result = metrics.merge(identity, on="offer_id", how="left", suffixes=("", "_identity"))
    if "sku_identity" in result.columns:
        result["sku"] = result["sku"].combine_first(result["sku_identity"])
        result = result.drop(columns=["sku_identity"])
    if "status" in result.columns:
        result["status_label"] = result["status"].map(OFFER_STATUS_LABELS).fillna(
            result["status"]
        )
    return result


def _risk_product_context(
    anomalies: pd.DataFrame,
    dataset: DashboardDataset,
    as_of: date,
) -> pd.DataFrame:
    """Attach current product detail to anomaly records without changing rules."""
    columns = [
        "offer_id",
        "metric_date",
        "title",
        "sku",
        "tsin_id",
        "barcode",
        "image_url",
        "selling_price",
        "rrp",
        "status_label",
        "total_stock",
        "page_views_30_days",
        "ordered_units_7_days",
        "effective_units",
        "ordered_revenue",
        "conversion_percentage_30_days",
        "first_listed_at",
        "first_listed_source",
        "latest_restock_date",
        "latest_restock_increase",
    ]
    if anomalies.empty or "offer_id" not in anomalies.columns:
        return pd.DataFrame(columns=columns)

    context = anomalies.loc[:, ["offer_id"]].drop_duplicates().copy()
    window = build_quadrant_window(dataset.product_daily, as_of, days=7)
    if not window.empty and "offer_id" in window.columns:
        window = window.rename(columns={"ordered_units": "ordered_units_7_days"})
        available = [
            column
            for column in [
                "offer_id",
                "metric_date",
                "sku",
                "offer_status",
                "total_stock",
                "page_views_30_days",
                "ordered_units_7_days",
                "effective_units",
                "ordered_revenue",
                "conversion_percentage_30_days",
            ]
            if column in window.columns
        ]
        context = context.merge(
            window.loc[:, available].drop_duplicates("offer_id", keep="last"),
            on="offer_id",
            how="left",
        )

    context = _enrich_products(context, dataset.offer_current)
    offer_status = (
        context["offer_status"].map(OFFER_STATUS_LABELS).fillna(
            context["offer_status"]
        )
        if "offer_status" in context.columns
        else pd.Series(index=context.index, dtype="object")
    )
    if "status_label" in context.columns:
        context["status_label"] = context["status_label"].combine_first(
            offer_status
        )
    else:
        context["status_label"] = offer_status

    operational = _quadrant_operational_context(
        dataset.product_daily,
        dataset.offer_current,
        as_of,
        dataset.offer_history,
    )
    if not operational.empty:
        context = context.merge(operational, on="offer_id", how="left")

    for column in columns:
        if column not in context.columns:
            context[column] = None
    return context.loc[:, columns]


def _quadrant_operational_context(
    product_daily: pd.DataFrame,
    offers: pd.DataFrame,
    as_of: date,
    offer_history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Derive listing and observed replenishment context from durable history."""
    columns = [
        "offer_id",
        "first_listed_at",
        "first_listed_source",
        "latest_restock_date",
        "latest_restock_increase",
    ]
    required = {"metric_date", "offer_id", "total_stock"}
    if product_daily.empty or not required.issubset(product_daily.columns):
        return pd.DataFrame(columns=columns)

    history = filter_as_of(product_daily, as_of, "metric_date").copy()
    history["_metric_date"] = pd.to_datetime(
        history["metric_date"], errors="coerce"
    ).dt.date
    history = history.loc[
        history["offer_id"].notna() & history["_metric_date"].notna()
    ].copy()
    if history.empty:
        return pd.DataFrame(columns=columns)

    first_seen = (
        history.groupby("offer_id")["_metric_date"]
        .min()
        .rename("_first_seen_date")
        .reset_index()
    )

    restocks = _platform_restock_context(offer_history, as_of)

    context = first_seen.merge(restocks, on="offer_id", how="left")
    platform_listings = _platform_listing_context(offers)
    if not platform_listings.empty:
        context = context.merge(platform_listings, on="offer_id", how="left")
    else:
        context["_platform_listed_at"] = None
    context["first_listed_at"] = context.apply(
        lambda row: (
            row["_platform_listed_at"]
            if pd.notna(row["_platform_listed_at"])
            else row["_first_seen_date"].isoformat()
        ),
        axis=1,
    )
    context["first_listed_source"] = context["_platform_listed_at"].map(
        lambda value: "platform" if pd.notna(value) else "first_observed"
    )
    context["latest_restock_increase"] = pd.to_numeric(
        context["latest_restock_increase"], errors="coerce"
    ).map(lambda value: int(value) if pd.notna(value) else None)
    return pd.DataFrame(context.loc[:, columns])


def _platform_restock_context(
    offer_history: pd.DataFrame | None,
    as_of: date,
) -> pd.DataFrame:
    columns = ["offer_id", "latest_restock_date", "latest_restock_increase"]
    required = {"snapshot_date", "offer_id", "captured_at", "total_stock"}
    if (
        offer_history is None
        or offer_history.empty
        or not required.issubset(offer_history.columns)
    ):
        return pd.DataFrame(columns=columns)

    stock_history = filter_as_of(offer_history, as_of, "snapshot_date").loc[
        :, ["snapshot_date", "offer_id", "captured_at", "total_stock"]
    ].copy()
    stock_history["_snapshot_date"] = pd.to_datetime(
        stock_history["snapshot_date"], errors="coerce"
    ).dt.date
    stock_history["_captured_at"] = pd.to_datetime(
        stock_history["captured_at"], errors="coerce", utc=True
    )
    stock_history["total_stock"] = pd.to_numeric(
        stock_history["total_stock"], errors="coerce"
    )
    stock_history = stock_history.dropna(
        subset=["offer_id", "_snapshot_date", "total_stock"]
    ).sort_values(["offer_id", "_snapshot_date", "_captured_at"])
    stock_history["_previous_stock"] = stock_history.groupby("offer_id")[
        "total_stock"
    ].shift()
    stock_history["_stock_increase"] = (
        stock_history["total_stock"] - stock_history["_previous_stock"]
    )
    restocks = stock_history.loc[stock_history["_stock_increase"] > 0].copy()
    if restocks.empty:
        return pd.DataFrame(columns=columns)
    restocks = restocks.drop_duplicates("offer_id", keep="last")
    restocks["latest_restock_date"] = restocks.apply(
        lambda row: _format_observation_time(row["_captured_at"])
        or row["_snapshot_date"].isoformat(),
        axis=1,
    )
    restocks["latest_restock_increase"] = restocks["_stock_increase"].map(int)
    return restocks.loc[:, columns]


def _platform_listing_context(offers: pd.DataFrame) -> pd.DataFrame:
    if offers.empty or not {"offer_id", "created_at"}.issubset(offers.columns):
        return pd.DataFrame(columns=["offer_id", "_platform_listed_at"])
    result = offers.loc[:, ["offer_id", "created_at"]].drop_duplicates(
        "offer_id", keep="last"
    )
    result["_platform_listed_at"] = result["created_at"].map(
        _format_platform_listing_time
    )
    return result.loc[:, ["offer_id", "_platform_listed_at"]]


def _format_platform_listing_time(value: object) -> str | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed_value = pd.to_datetime(str(value), errors="coerce")
        if not isinstance(parsed_value, pd.Timestamp):
            return None
        parsed = parsed_value.to_pydatetime()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SAST).strftime("%Y-%m-%d %H:%M")


def _format_observation_time(value: object) -> str | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    parsed = pd.to_datetime(str(value), errors="coerce", utc=True)
    if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
        return None
    return parsed.to_pydatetime().astimezone(CHINA).strftime("%Y-%m-%d %H:%M")


def _details_text(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)
