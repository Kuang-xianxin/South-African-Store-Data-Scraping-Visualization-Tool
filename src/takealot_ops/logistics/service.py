"""Compose a safe logistics overview from Long Reach W8 and Takealot shipments."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import ApiResponseError
from takealot_ops.logistics.w8 import W8ApiError, W8Client
from takealot_ops.settings import Settings, SettingsError, W8Settings


SAST = ZoneInfo("Africa/Johannesburg")


class LogisticsOverviewService:
    """Load both providers once and cache the sanitized projection briefly."""

    def __init__(
        self,
        project_root: Path,
        *,
        cache_ttl_seconds: float = 60.0,
        force_refresh_min_interval_seconds: float = 10.0,
        w8_transport: httpx.BaseTransport | None = None,
        takealot_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._cache_ttl_seconds = cache_ttl_seconds
        self._force_refresh_min_interval_seconds = force_refresh_min_interval_seconds
        self._w8_transport = w8_transport
        self._takealot_transport = takealot_transport
        self._lock = Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None

    def load(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            age = time.monotonic() - self._cached_at
            if self._cached is not None:
                should_use_cache = (not force and age < self._cache_ttl_seconds) or (
                    force and age < self._force_refresh_min_interval_seconds
                )
                if should_use_cache:
                    return {**self._cached, "cache_age_seconds": round(age, 1)}
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="logistics-overview") as pool:
                w8_future = pool.submit(self._load_w8)
                takealot_future = pool.submit(self._load_takealot)
                w8 = w8_future.result()
                takealot = takealot_future.result()
            matching = _explicit_matches(
                w8.get("_raw_inbound", []),
                takealot.get("_raw_shipments", []),
            )
            w8.pop("_raw_inbound", None)
            takealot.pop("_raw_shipments", None)
            payload = {
                "generated_at": datetime.now(UTC).isoformat(),
                "cache_ttl_seconds": self._cache_ttl_seconds,
                "cache_age_seconds": 0,
                "w8": w8,
                "takealot": takealot,
                "matching": matching,
                "boundaries": [
                    "长睿运单号可以读取，但当前接口没有逐站扫描轨迹。",
                    "只把明确出现在两边编号字段中的值列为自动匹配，不按日期或数量猜测关系。",
                    "本页只读实时接口，不创建、取消、打印或修改任何物流单据。",
                ],
            }
            self._cached = payload
            self._cached_at = time.monotonic()
            return payload

    def _load_w8(self) -> dict[str, Any]:
        try:
            settings = W8Settings.from_env(self._project_root)
            if not settings.configured:
                return _w8_unavailable("长睿 W8 授权码尚未配置")
            client = W8Client(settings, transport=self._w8_transport)
            try:
                warehouses = client.warehouses()
                if not warehouses:
                    return _w8_unavailable("授权成功，但当前客户没有可读取仓库")
                warehouse = warehouses[0]
                house_id = _as_int(warehouse.get("houseId"))
                house_code = _text(warehouse.get("houseCode"))
                if house_id is None or not house_code:
                    raise W8ApiError("长睿仓库缺少 houseId 或 houseCode")
                request_id = str(uuid4())
                end = f"{datetime.now(SAST).date() + timedelta(days=1)} 00:00:00"
                with ThreadPoolExecutor(max_workers=6, thread_name_prefix="w8-read") as pool:
                    futures: dict[str, Future[Any]] = {
                        "channels": pool.submit(client.channels, house_code),
                        "products": pool.submit(client.products, request_id),
                        "stocks": pool.submit(client.stocks, house_id, house_code, request_id),
                        "inbound": pool.submit(
                            client.inbound_orders,
                            house_id,
                            house_code,
                            end,
                            request_id,
                        ),
                        "outbound": pool.submit(
                            client.outbound_orders,
                            house_id,
                            house_code,
                            request_id,
                        ),
                        "returns": pool.submit(client.returned_orders, house_code, request_id),
                    }
                    results = {name: future.result() for name, future in futures.items()}
            finally:
                client.close()
        except (SettingsError, W8ApiError, httpx.HTTPError) as exc:
            return _w8_unavailable(str(exc))

        stocks = _records(results["stocks"])
        inbound = _records(results["inbound"])
        outbound = _records(results["outbound"])
        returned = _records(results["returns"])
        return_total = _page_total(results["returns"], len(returned))
        warnings: list[str] = []
        if return_total != len(returned):
            warnings.append(
                f"退货接口本次返回 {len(returned)} 条记录，但分页 total={return_total}；"
                "雏形按实际返回记录展示，不把 total 当作已确认单量。"
            )
        channels = [
            {
                "code": _text(row.get("channelCode")),
                "name": _text(row.get("channelName")) or _text(row.get("channelNameCn")),
            }
            for row in results["channels"]
            if isinstance(row, Mapping)
        ]
        return {
            "connected": True,
            "provider": "长睿 Long Reach",
            "environment": "正式环境",
            "warehouse": {
                "id": house_id,
                "code": house_code,
                "name": _text(warehouse.get("houseCnname"))
                or _text(warehouse.get("houseEnname"))
                or house_code,
                "country": _text(warehouse.get("hrcountry")),
            },
            "channels": channels,
            "summary": {
                "products": _page_total(results["products"], len(_records(results["products"]))),
                "stock_records": len(stocks),
                "stock_total": _sum_field(stocks, "stockNum"),
                "usable_stock": _sum_field(stocks, "usableStockNum"),
                "locked_stock": _sum_field(stocks, "lockNum"),
                "outbound_allocated": _sum_field(stocks, "outboundNum"),
                "transit_stock": _sum_field(stocks, "transitNum"),
                "defective_stock": _sum_field(stocks, "defectiveNum"),
                "inbound_orders": _page_total(results["inbound"], len(inbound)),
                "outbound_orders": _page_total(results["outbound"], len(outbound)),
                "returned_records": len(returned),
            },
            "inbound_statuses": _status_counts(inbound),
            "outbound_statuses": _status_counts(outbound),
            "recent_inbound": [_inbound_projection(row) for row in _recent(inbound)[:10]],
            "recent_outbound": [_outbound_projection(row) for row in _recent(outbound)[:10]],
            "warnings": warnings,
            "_raw_inbound": inbound,
        }

    def _load_takealot(self) -> dict[str, Any]:
        try:
            settings = Settings.from_env(self._project_root)
            client = TakealotClient(settings, transport=self._takealot_transport)
            try:
                shipments = list(client.list_shipments())
            finally:
                client.close()
        except (SettingsError, ApiResponseError, httpx.HTTPError) as exc:
            return {
                "connected": False,
                "message": str(exc),
                "summary": _empty_takealot_summary(),
                "recent_shipments": [],
                "_raw_shipments": [],
            }
        recent = sorted(shipments, key=_shipment_sort_key, reverse=True)[:10]
        item_rows = [
            item
            for shipment in shipments
            for item in _mapping_list(shipment.get("shipment_items"))
        ]
        return {
            "connected": True,
            "summary": {
                "shipments": len(shipments),
                "replenishment": sum(
                    1 for row in shipments if row.get("shipment_type") == "replenishment"
                ),
                "shipped": sum(bool(row.get("shipped")) for row in shipments),
                "unloaded": sum(bool(row.get("date_unloaded")) for row in shipments),
                "cancelled": sum(bool(row.get("cancelled")) for row in shipments),
                "with_tracking_info": sum(bool(_text(row.get("tracking_info"))) for row in shipments),
                "quantity_sending": _sum_field(item_rows, "quantity_sending"),
                "quantity_received": _sum_field(
                    item_rows,
                    "purchase_order_quantity_received",
                ),
                "quantity_damaged": _sum_field(
                    item_rows,
                    "purchase_order_quantity_damaged",
                ),
            },
            "recent_shipments": [_shipment_projection(row) for row in recent],
            "_raw_shipments": shipments,
        }


def _w8_unavailable(message: str) -> dict[str, Any]:
    return {
        "connected": False,
        "provider": "长睿 Long Reach",
        "environment": "正式环境",
        "message": message,
        "warehouse": None,
        "channels": [],
        "summary": {
            "products": 0,
            "stock_records": 0,
            "stock_total": 0,
            "usable_stock": 0,
            "locked_stock": 0,
            "outbound_allocated": 0,
            "transit_stock": 0,
            "defective_stock": 0,
            "inbound_orders": 0,
            "outbound_orders": 0,
            "returned_records": 0,
        },
        "inbound_statuses": [],
        "outbound_statuses": [],
        "recent_inbound": [],
        "recent_outbound": [],
        "warnings": [],
        "_raw_inbound": [],
    }


def _empty_takealot_summary() -> dict[str, int]:
    return {
        "shipments": 0,
        "replenishment": 0,
        "shipped": 0,
        "unloaded": 0,
        "cancelled": 0,
        "with_tracking_info": 0,
        "quantity_sending": 0,
        "quantity_received": 0,
        "quantity_damaged": 0,
    }


def _records(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in page.get("records", []) if isinstance(row, Mapping)]


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _page_total(page: Mapping[str, Any], fallback: int) -> int:
    return _as_int(page.get("total")) or fallback


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _sum_field(rows: Sequence[Mapping[str, Any]], field: str) -> int:
    return sum(_as_int(row.get(field)) or 0 for row in rows)


def _status_counts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(_text(row.get("statusName")) or "未标记" for row in rows)
    return [{"status": status, "count": count} for status, count in counts.most_common()]


def _recent(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: _text(row.get("createDate")) or _text(row.get("createDateStr")),
        reverse=True,
    )


def _inbound_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_no": _text(row.get("orderNo")),
        "status": _text(row.get("statusName")) or "未标记",
        "created_at": _text(row.get("createDateStr")),
        "forecast_date": _text(row.get("forecastDateStr")),
        "inbound_date": _text(row.get("inboundDateStr")),
        "shelf_date": _text(row.get("shelfDateStr")),
        "headway_no": _text(row.get("headwayNo")),
        "shipping_mark": _text(row.get("shippingMark")),
        "sku_types": _as_int(row.get("skuTypeCount")) or 0,
        "forecast_quantity": _as_int(row.get("skuForecastTotalNum")) or 0,
    }


def _outbound_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_no": _text(row.get("orderNo")),
        "status": _text(row.get("statusName")) or "未标记",
        "created_at": _text(row.get("createDateStr")),
        "outbound_date": _text(row.get("outboundDateStr")),
        "waybill_no": _text(row.get("waybillNo")),
        "logistics_type": _text(row.get("logisticTypeName")),
        "sku_types": _as_int(row.get("skuTypeCount")) or 0,
        "total_quantity": _as_int(row.get("totalQty")) or 0,
        "has_document": bool(_text(row.get("podPath"))),
    }


def _shipment_sort_key(row: Mapping[str, Any]) -> tuple[str, int]:
    return (_text(row.get("created_at")), _as_int(row.get("shipment_id")) or 0)


def _shipment_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    items = _mapping_list(row.get("shipment_items"))
    return {
        "shipment_id": _as_int(row.get("shipment_id")),
        "reference": _text(row.get("reference")),
        "purchase_order_number": _text(row.get("purchase_order_number")),
        "destination_region": _text(row.get("destination_region")),
        "purchase_order_state": _text(row.get("purchase_order_state")),
        "shipment_type": _text(row.get("shipment_type")),
        "shipped": bool(row.get("shipped")),
        "cancelled": bool(row.get("cancelled")),
        "due_date": _text(row.get("due_date")),
        "date_unloaded": _text(row.get("date_unloaded")),
        "tracking_info": _text(row.get("tracking_info")),
        "sku_lines": len(items),
        "quantity_sending": _sum_field(items, "quantity_sending"),
        "quantity_received": _sum_field(items, "purchase_order_quantity_received"),
        "quantity_damaged": _sum_field(items, "purchase_order_quantity_damaged"),
    }


def _explicit_matches(
    inbound: Sequence[Mapping[str, Any]],
    shipments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    matched_inbound: set[str] = set()
    matched_shipments: set[int] = set()
    for inbound_row in inbound:
        inbound_no = _text(inbound_row.get("orderNo"))
        identifiers = [
            _text(inbound_row.get("headwayNo")),
            _text(inbound_row.get("shippingMark")),
            inbound_no,
        ]
        normalized = [value for value in map(_normalize_identifier, identifiers) if len(value) >= 8]
        if not normalized:
            continue
        for shipment in shipments:
            shipment_id = _as_int(shipment.get("shipment_id"))
            if shipment_id is None:
                continue
            searchable = _normalize_identifier(
                " ".join(
                    (
                        _text(shipment.get("reference")),
                        _text(shipment.get("tracking_info")),
                        _text(shipment.get("purchase_order_number")),
                    )
                )
            )
            if not searchable or not any(value in searchable for value in normalized):
                continue
            matched_inbound.add(inbound_no)
            matched_shipments.add(shipment_id)
            matches.append(
                {
                    "w8_order_no": inbound_no,
                    "w8_headway_no": _text(inbound_row.get("headwayNo")),
                    "takealot_shipment_id": shipment_id,
                    "takealot_reference": _text(shipment.get("reference")),
                }
            )
            break
    return {
        "method": "仅按头程号、箱唛或长睿单号在 Takealot Reference/Tracking 中的明确出现匹配",
        "direct_match_count": len(matches),
        "matched_w8_inbound": len(matched_inbound),
        "matched_takealot_shipments": len(matched_shipments),
        "unmatched_w8_inbound": max(0, len(inbound) - len(matched_inbound)),
        "unmatched_takealot_shipments": max(0, len(shipments) - len(matched_shipments)),
        "items": matches[:20],
    }


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())
