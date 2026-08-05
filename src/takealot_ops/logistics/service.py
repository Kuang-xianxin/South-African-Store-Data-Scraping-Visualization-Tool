"""Compose a safe logistics overview from Long Reach W8 and Takealot shipments."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import SQLAlchemyError

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import ApiResponseError
from takealot_ops.logistics.links import (
    LogisticsLinkError,
    build_logistics_candidates,
    confirm_candidate_link,
    list_confirmed_links,
    load_offer_sku_map,
    revoke_confirmed_link,
)
from takealot_ops.logistics.snapshots import (
    load_provider_snapshot,
    save_provider_snapshot,
)
from takealot_ops.logistics.w8 import W8ApiError, W8Client
from takealot_ops.settings import DashboardSettings, Settings, SettingsError, W8Settings
from takealot_ops.storage.migrations import (
    create_engine_for_settings,
    create_read_only_engine,
    create_schema,
)
from takealot_ops.storage.store_context import current_store_code, store_scope


SAST = ZoneInfo("Africa/Johannesburg")


class LogisticsOverviewService:
    """Read durable logistics snapshots and refresh providers only on demand."""

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
        self._cached_by_store: dict[str, tuple[float, dict[str, Any]]] = {}
        self._w8_cached_at = 0.0
        self._w8_cached: dict[str, Any] | None = None
        self._schema_ready = False

    def load(self, *, force: bool = False) -> dict[str, Any]:
        """Load local snapshots, or explicitly refresh both upstream providers."""
        store_code = current_store_code()
        with self._lock:
            now = time.monotonic()
            cached_entry = self._cached_by_store.get(store_code)
            if force and cached_entry is not None:
                cached_at, cached_payload = cached_entry
                age = now - cached_at
                if age < self._force_refresh_min_interval_seconds:
                    return {**cached_payload, "cache_age_seconds": round(age, 1)}

            if not force:
                w8 = self._load_local_provider("w8")
                takealot = self._load_local_provider("takealot")
                return self._compose_payload(w8, takealot)

            w8_age = now - self._w8_cached_at
            use_w8_cache = (
                self._w8_cached is not None
                and w8_age < self._force_refresh_min_interval_seconds
            )
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="logistics-overview") as pool:
                w8_future = (
                    None
                    if use_w8_cache
                    else pool.submit(self._load_in_store_scope, store_code, self._load_w8)
                )
                takealot_future = pool.submit(
                    self._load_in_store_scope,
                    store_code,
                    self._load_takealot,
                )
                if w8_future is None:
                    assert self._w8_cached is not None
                    w8 = self._w8_cached
                else:
                    w8 = w8_future.result()
                    self._w8_cached = w8
                    self._w8_cached_at = time.monotonic()
                takealot = takealot_future.result()
            w8 = self._persist_or_restore_provider("w8", w8)
            takealot = self._persist_or_restore_provider("takealot", takealot)
            payload = self._compose_payload(w8, takealot)
            self._cached_by_store[store_code] = (time.monotonic(), payload)
            return payload

    def _load_local_provider(self, provider: str) -> dict[str, Any]:
        """Read one provider's latest successful snapshot without network access."""
        try:
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_read_only_engine(settings.database_url)
            try:
                snapshot = load_provider_snapshot(engine, provider)
            finally:
                engine.dispose()
        except (SettingsError, SQLAlchemyError, ValueError):
            snapshot = None
        if snapshot is None:
            payload = (
                _w8_unavailable("尚无长睿本地快照，请等待定时同步或手动刷新。")
                if provider == "w8"
                else {
                    "connected": False,
                    "message": "尚无 Takealot 物流本地快照，请等待定时同步或手动刷新。",
                    "summary": _empty_takealot_summary(),
                    "recent_shipments": [],
                    "_raw_shipments": [],
                }
            )
            return {
                **payload,
                "live_connected": False,
                "data_source": "unavailable",
                "synced_at": None,
                "snapshot_saved": False,
                "refresh_attempted": False,
            }
        payload = dict(snapshot["payload"])
        payload.update(
            {
                "connected": True,
                "live_connected": False,
                "data_source": "local_database",
                "synced_at": snapshot["fetched_at"],
                "snapshot_saved": True,
                "refresh_attempted": False,
                "message": "当前展示定时或手动同步到本地数据库的最近成功快照。",
            }
        )
        return payload

    def _compose_payload(
        self,
        w8: dict[str, Any],
        takealot: dict[str, Any],
    ) -> dict[str, Any]:
        """Build matching and display data from already-sanitized provider payloads."""
        raw_inbound = w8.get("_raw_inbound", [])
        raw_shipments = takealot.get("_raw_shipments", [])
        matching = _explicit_matches(raw_inbound, raw_shipments)
        matching_warnings: list[str] = []
        candidate_tiers: dict[str, list[dict[str, Any]]] = {
            "high": [],
            "medium": [],
            "low": [],
            "split_groups": [],
        }
        confirmed_links: list[dict[str, Any]] = []
        try:
            database_settings = DashboardSettings.from_env(self._project_root)
            engine = create_read_only_engine(database_settings.database_url)
            try:
                offer_skus = load_offer_sku_map(engine)
                candidate_tiers = build_logistics_candidates(
                    raw_inbound,
                    raw_shipments,
                    offer_skus,
                )
                confirmed_links = list_confirmed_links(engine)
            finally:
                engine.dispose()
        except (SettingsError, SQLAlchemyError, LogisticsLinkError):
            matching_warnings.append(
                "本地商品SKU映射或人工关联暂时不可读，分级候选未生成。"
            )
        direct_pairs = {
            (str(item.get("w8_order_no") or ""), _as_int(item.get("takealot_shipment_id")))
            for item in matching["items"]
        }
        confirmed_pairs = {
            (str(item.get("w8_order_no") or ""), _as_int(item.get("takealot_shipment_id")))
            for item in confirmed_links
        }
        for tier in ("high", "medium", "low"):
            candidate_tiers[tier] = [
                candidate
                for candidate in candidate_tiers[tier]
                if (
                    str(candidate.get("w8_order_no") or ""),
                    _as_int(candidate.get("takealot_shipment_id")),
                )
                not in direct_pairs | confirmed_pairs
            ]
        matching.update(
            {
                "confirmed_link_count": len(confirmed_links),
                "confirmed_links": confirmed_links,
                "high_confidence_candidate_count": len(candidate_tiers["high"]),
                "high_confidence_candidates": candidate_tiers["high"],
                "medium_confidence_candidate_count": len(candidate_tiers["medium"]),
                "medium_confidence_candidates": candidate_tiers["medium"],
                "low_confidence_candidate_count": len(candidate_tiers["low"]),
                "low_confidence_candidates": candidate_tiers["low"],
                "split_batch_group_count": len(candidate_tiers["split_groups"]),
                "split_batch_groups": candidate_tiers["split_groups"],
                "warnings": matching_warnings,
            }
        )
        w8.pop("_raw_inbound", None)
        takealot.pop("_raw_shipments", None)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "cache_ttl_seconds": 0,
            "cache_age_seconds": 0,
            "automatic_page_refresh": False,
            "w8": w8,
            "takealot": takealot,
            "matching": matching,
            "boundaries": [
                "打开物流页只读取本地数据库，不会自动访问长睿或 Takealot 接口。",
                "物流快照随既有店铺定时采集成功后同步，也可由有权限用户手动刷新。",
                "长睿运单号可以读取，但当前接口没有逐站扫描轨迹。",
                "只把明确出现在两边编号字段中的值列为自动匹配，不按日期或数量猜测关系。",
                "SKU、各SKU发送数量、双方唯一性及30天日期窗口只生成高置信候选，必须人工确认后才成为持久关联。",
                "整单SKU相同但数量不同列为中置信候选；至少一半SKU重合列为低置信候选，两档使用60天核对窗口且绝不自动确认。",
                "拆批组合只在2至3个Takealot Shipment的完整SKU数量合计等于一个长睿入库单时提示，仍需逐单人工核对。",
                "本页不创建、取消、打印或修改任何上游物流单据。",
            ],
        }

    @staticmethod
    def _load_in_store_scope(
        store_code: str,
        loader: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run one worker-thread provider read with the request's store context."""
        with store_scope(store_code):
            return loader()

    def _persist_or_restore_provider(
        self,
        provider: str,
        live_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist successful data, or serve the latest durable snapshot on failure."""
        live_connected = bool(live_payload.get("connected"))
        try:
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_engine_for_settings(settings)
            try:
                if not self._schema_ready:
                    create_schema(engine)
                    self._schema_ready = True
                if live_connected:
                    synced_at = save_provider_snapshot(engine, provider, live_payload)
                    return {
                        **live_payload,
                        "live_connected": True,
                        "data_source": "live_api",
                        "synced_at": synced_at,
                        "snapshot_saved": True,
                        "refresh_attempted": True,
                    }
                snapshot = load_provider_snapshot(engine, provider)
            finally:
                engine.dispose()
        except (SettingsError, SQLAlchemyError, ValueError):
            result = {
                **live_payload,
                "live_connected": live_connected,
                "data_source": "live_api" if live_connected else "unavailable",
                "synced_at": None,
                "snapshot_saved": False,
                "refresh_attempted": True,
            }
            _append_warning(result, "本地物流快照暂时不可写，当前数据未持久化。")
            return result

        if snapshot is None:
            return {
                **live_payload,
                "live_connected": False,
                "data_source": "unavailable",
                "synced_at": None,
                "snapshot_saved": False,
                "refresh_attempted": True,
            }

        cached_payload = dict(snapshot["payload"])
        live_error = _text(live_payload.get("message"))
        cached_payload.update(
            {
                "connected": True,
                "live_connected": False,
                "data_source": "local_database",
                "synced_at": snapshot["fetched_at"],
                "snapshot_saved": True,
                "refresh_attempted": True,
                "message": "实时接口本次不可用，当前展示本地数据库中最近一次成功快照。",
            }
        )
        if live_error:
            _append_warning(cached_payload, f"实时接口失败：{live_error}")
        return cached_payload

    def confirm_candidate(
        self,
        *,
        w8_order_no: str,
        takealot_shipment_id: int,
        actor_user_id: int,
        actor_username: str,
    ) -> dict[str, Any]:
        """Persist one candidate only if it is present in a current review tier."""
        payload = self.load()
        candidates = [
            *payload["matching"]["high_confidence_candidates"],
            *payload["matching"]["medium_confidence_candidates"],
            *payload["matching"]["low_confidence_candidates"],
        ]
        candidate = next(
            (
                item
                for item in candidates
                if item["w8_order_no"] == w8_order_no
                and item["takealot_shipment_id"] == takealot_shipment_id
            ),
            None,
        )
        if candidate is None:
            raise LogisticsLinkError("该关系当前不在可确认候选中，请重新读取后核对")
        try:
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_engine_for_settings(settings)
            try:
                create_schema(engine)
                link = confirm_candidate_link(
                    engine,
                    candidate,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            finally:
                engine.dispose()
        except (SettingsError, SQLAlchemyError):
            raise LogisticsLinkError("物流关联保存失败，请稍后重试") from None
        self._invalidate_cache()
        return link

    def revoke_link(
        self,
        link_id: int,
        *,
        actor_user_id: int,
        actor_username: str,
        note: str,
    ) -> dict[str, Any]:
        """Revoke one persisted relationship without deleting its audit history."""
        try:
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_engine_for_settings(settings)
            try:
                create_schema(engine)
                link = revoke_confirmed_link(
                    engine,
                    link_id,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=note,
                )
            finally:
                engine.dispose()
        except (SettingsError, SQLAlchemyError):
            raise LogisticsLinkError("物流关联撤销失败，请稍后重试") from None
        self._invalidate_cache()
        return link

    def _invalidate_cache(self) -> None:
        with self._lock:
            self._cached_by_store.clear()

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


def _append_warning(payload: dict[str, Any], message: str) -> None:
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        payload["warnings"] = warnings
    warnings.append(message)


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
        "method": "仅按头程号、箱唛或长睿单号在 Takealot Reference/Tracking/PO 中的明确出现匹配",
        "direct_match_count": len(matches),
        "matched_w8_inbound": len(matched_inbound),
        "matched_takealot_shipments": len(matched_shipments),
        "unmatched_w8_inbound": max(0, len(inbound) - len(matched_inbound)),
        "unmatched_takealot_shipments": max(0, len(shipments) - len(matched_shipments)),
        "items": matches[:20],
    }


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())
