"""Confirmed, checkpointed, strictly serial multi-store search-ranking batches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import statistics
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.search_ranking.service import (
    PROMPT_VERSION,
    PRICING_SNAPSHOT_DATE,
    SearchRankingConfigurationError,
    SearchRankingInputError,
    SearchRankingProviderError,
    SearchRankingService,
    _analysis_cache_key,
    _variant_family_cache_material,
    _variant_family_profile,
)
from takealot_ops.storage.migrations import create_read_only_engine
from takealot_ops.storage.models import SearchRankingAnalysis
from takealot_ops.storage.store_context import store_scope


LOGGER = logging.getLogger(__name__)
BATCH_SCHEMA_VERSION = 3
ACTIVE_BATCH_STATUSES = frozenset({"queued", "running", "pausing", "stopping"})
PAUSED_BATCH_STATUSES = frozenset({"paused", "paused_after_error", "interrupted"})
RESUMABLE_BATCH_STATUSES = PAUSED_BATCH_STATUSES | {"stopped"}
DEFAULT_INPUT_TOKENS_PER_FRESH_IMAGE = 6_000
DEFAULT_OUTPUT_TOKENS_PER_FRESH_IMAGE = 1_400
DEFAULT_PUBLIC_REQUESTS_PER_OFFER = 42


class SearchRankingBatchInputError(ValueError):
    """Raised when a batch preview or confirmation is stale or invalid."""


class SearchRankingBatchConflictError(ValueError):
    """Raised when a second batch or invalid state transition is attempted."""


class SearchRankingBatchPermissionError(ValueError):
    """Raised when a different account attempts to control a batch."""


class _BatchSearchRankingService(Protocol):
    runtime: Any
    database_url: str

    def list_payload(self) -> dict[str, Any]: ...

    def detail_payload(self, offer_id: str) -> dict[str, Any] | None: ...

    async def analyze_offer(self, offer_id: str) -> dict[str, Any]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _store_value(store: Any, field: str, default: Any = None) -> Any:
    if isinstance(store, Mapping):
        return store.get(field, default)
    return getattr(store, field, default)


def _connected_store_rows(stores: Sequence[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for store in stores:
        code = str(_store_value(store, "code", "")).strip().casefold()
        if (
            not code
            or code in seen
            or not bool(_store_value(store, "active", False))
            or not bool(_store_value(store, "data_connected", False))
        ):
            continue
        seen.add(code)
        output.append(
            {
                "code": code,
                "display_name": str(_store_value(store, "display_name", code)).strip()
                or code,
            }
        )
    return output


def _product_family_key(item: Mapping[str, Any]) -> str:
    """Keep one analysis target per store-scoped PLID; missing PLIDs stay separate."""

    store_code = str(item.get("store_code") or "").strip().casefold()
    productline_id = str(item.get("productline_id") or "").strip().casefold()
    identity = (
        f"plid:{productline_id}"
        if productline_id
        else f"offer:{str(item.get('offer_id') or '').strip().casefold()}"
    )
    return f"{store_code}|{identity}"


def _family_representative(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Prefer the most recently analysed variant, then a stable lowest offer id."""

    for item in items:
        latest = item.get("latest_analysis")
        if not isinstance(latest, Mapping):
            continue
        source_offer_id = str(latest.get("source_offer_id") or "").strip()
        if source_offer_id and source_offer_id == str(item.get("offer_id") or "").strip():
            return item
    analysed = [item for item in items if isinstance(item.get("latest_analysis"), Mapping)]
    candidates = list(analysed or items)
    if analysed:
        newest = max(
            str((item.get("latest_analysis") or {}).get("created_at") or "")
            for item in analysed
        )
        candidates = [
            item
            for item in analysed
            if str((item.get("latest_analysis") or {}).get("created_at") or "") == newest
        ]
    return min(candidates, key=lambda item: str(item.get("offer_id") or ""))


def _target_variant_values(target: Mapping[str, Any], field: str) -> list[str]:
    raw = target.get(field)
    if isinstance(raw, list):
        values = [str(value).strip() for value in raw if str(value).strip()]
    else:
        values = []
    if field == "variant_offer_ids" and not values:
        offer_id = str(target.get("offer_id") or "").strip()
        if offer_id:
            values.append(offer_id)
    if field == "variant_titles" and not values:
        title = str(target.get("title") or "").strip()
        if title:
            values.append(title)
    return list(dict.fromkeys(values))


def _enrich_target_variant_metadata(target: dict[str, Any]) -> None:
    """Backfill title-derived variant parameters without changing queue identity or order."""

    offer_ids = _target_variant_values(target, "variant_offer_ids")
    titles = _target_variant_values(target, "variant_titles")
    if not offer_ids:
        return
    family_items = [
        {
            "offer_id": offer_id,
            "productline_id": target.get("productline_id"),
            "sku": target.get("sku") if index == 0 else None,
            "title": titles[index] if index < len(titles) else target.get("title"),
            "image_url": target.get("image_url") if index == 0 else None,
            "available_stock": 0,
        }
        for index, offer_id in enumerate(offer_ids)
    ]
    profile = _variant_family_profile(
        family_items,
        representative_offer_id=str(target.get("offer_id") or offer_ids[0]),
    )
    target["shared_family_title"] = profile.get("shared_title")
    target["variant_parameters"] = [
        {
            "offer_id": variant.get("offer_id"),
            "parameters": variant.get("parameters", []),
        }
        for variant in profile.get("variants", [])
        if isinstance(variant, Mapping)
    ]


def _compact_pending_variant_targets(state: dict[str, Any]) -> int:
    """Deduplicate only unfinished families so historical progress stays auditable."""

    raw_targets = state.get("targets")
    if not isinstance(raw_targets, list):
        return 0
    targets = [dict(item) for item in raw_targets if isinstance(item, Mapping)]
    next_index = max(0, min(_integer(state.get("next_index")), len(targets)))
    for target in targets:
        target["variant_offer_ids"] = _target_variant_values(
            target, "variant_offer_ids"
        )
        target["variant_titles"] = _target_variant_values(target, "variant_titles")
        target["variant_count"] = max(
            1,
            _integer(target.get("variant_count")),
            len(target["variant_offer_ids"]),
        )
        _enrich_target_variant_metadata(target)
    processed = targets[:next_index]
    seen = {_product_family_key(item) for item in processed}
    pending: list[dict[str, Any]] = []
    pending_by_key: dict[str, dict[str, Any]] = {}
    removed = 0

    for raw_target in targets[next_index:]:
        target = dict(raw_target)
        key = _product_family_key(target)
        if key in seen:
            removed += 1
            existing = pending_by_key.get(key)
            if existing is not None:
                existing_ids = _target_variant_values(existing, "variant_offer_ids")
                incoming_ids = _target_variant_values(target, "variant_offer_ids")
                existing_titles = _target_variant_values(existing, "variant_titles")
                incoming_titles = _target_variant_values(target, "variant_titles")
                existing["variant_offer_ids"] = list(
                    dict.fromkeys([*existing_ids, *incoming_ids])
                )
                existing["variant_titles"] = list(
                    dict.fromkeys([*existing_titles, *incoming_titles])
                )
                existing["variant_count"] = max(
                    len(existing["variant_offer_ids"]),
                    _integer(existing.get("variant_count"))
                    + max(1, _integer(target.get("variant_count"))),
                )
                _enrich_target_variant_metadata(existing)
            continue
        seen.add(key)
        target["variant_offer_ids"] = _target_variant_values(
            target, "variant_offer_ids"
        )
        target["variant_titles"] = _target_variant_values(target, "variant_titles")
        target["variant_count"] = max(
            1,
            _integer(target.get("variant_count")),
            len(target["variant_offer_ids"]),
        )
        _enrich_target_variant_metadata(target)
        pending.append(target)
        pending_by_key[key] = target

    if not removed and len(targets) == len(raw_targets):
        state["targets"] = targets
        state["next_index"] = len(processed)
        return 0

    state["targets"] = [*processed, *pending]
    state["next_index"] = len(processed)
    store_progress = state.get("store_progress")
    if isinstance(store_progress, dict):
        for code, progress in store_progress.items():
            if isinstance(progress, dict):
                progress["target_count"] = sum(
                    1
                    for target in state["targets"]
                    if str(target.get("store_code") or "") == str(code)
                )
    if removed:
        previous = state.get("variant_target_compaction")
        previous_removed = (
            _integer(previous.get("removed_pending_count"))
            if isinstance(previous, Mapping)
            else 0
        )
        state["variant_target_compaction"] = {
            "removed_pending_count": previous_removed + removed,
            "last_compacted_at": _iso_now(),
        }
    return removed


def _usage_from_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(detail, Mapping):
        return {
            "analysis_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0.0,
            "vision_reused": False,
        }
    analysis = detail.get("analysis")
    if not isinstance(analysis, Mapping):
        analysis = detail.get("latest_attempt")
    if not isinstance(analysis, Mapping):
        analysis = {}
    raw_usage = analysis.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    input_tokens = _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    return {
        "analysis_id": analysis.get("id"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _integer(usage.get("total_tokens"))
        or input_tokens + output_tokens,
        "estimated_cost_cny": round(_number(analysis.get("estimated_cost_cny")), 6),
        "vision_reused": bool(analysis.get("vision_reused")),
    }


class SearchRankingBatchController:
    """Own one global background batch and persist progress after every target."""

    def __init__(
        self,
        project_root: Path,
        *,
        service: _BatchSearchRankingService,
        analysis_lock: asyncio.Lock,
        state_path: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.service = service
        self.analysis_lock = analysis_lock
        self.state_path = state_path or (
            self.project_root / "logs" / "search-ranking-batch.json"
        )
        self._state_lock = RLock()
        self._task: asyncio.Task[None] | None = None
        self._checkpoint_needs_persist = False
        self._state = self._load_state()
        state_changed = self._checkpoint_needs_persist
        if self._state.get("status") in ACTIVE_BATCH_STATUSES:
            self._state["status"] = "interrupted"
            self._state["current_target"] = None
            self._state["pause_requested"] = False
            self._state["stop_requested"] = False
            self._state["last_error"] = (
                "ERP 在批次运行期间重新启动；系统没有自动续跑，也没有自动重试当前商品。"
            )
            self._state["updated_at"] = _iso_now()
            state_changed = True
        if state_changed:
            self._persist_state()

    def preview_payload(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> dict[str, Any]:
        preview = self._build_preview(stores)
        return {
            "policy": self._policy_payload(),
            "preview": {key: value for key, value in preview.items() if key != "_targets"},
            "batch": self.status_payload(
                stores,
                actor_username=actor_username,
                actor_is_admin=actor_is_admin,
            ),
        }

    def status_payload(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> dict[str, Any] | None:
        accessible_codes = {item["code"] for item in _connected_store_rows(stores)}
        with self._state_lock:
            if not self._state:
                return None
            state = json.loads(json.dumps(self._state, ensure_ascii=False))
        batch_codes = {str(item.get("code") or "") for item in state.get("stores", [])}
        authorized = bool(
            actor_is_admin
            or state.get("owner_username") == actor_username
            or (batch_codes and batch_codes.issubset(accessible_codes))
        )
        if not authorized:
            return {
                "batch_id": state.get("batch_id"),
                "status": state.get("status"),
                "owned_by_current_user": False,
                "details_available": False,
                "message": "另一个已授权账号正在运行搜索定位批次。",
            }
        targets = state.get("targets") if isinstance(state.get("targets"), list) else []
        next_index = max(0, min(_integer(state.get("next_index")), len(targets)))
        return {
            "batch_id": state.get("batch_id"),
            "status": state.get("status"),
            "owned_by_current_user": state.get("owner_username") == actor_username,
            "details_available": True,
            "owner_username": state.get("owner_username"),
            "owner_display_name": state.get("owner_display_name"),
            "snapshot_id": state.get("snapshot_id"),
            "created_at": state.get("created_at"),
            "started_at": state.get("started_at"),
            "updated_at": state.get("updated_at"),
            "finished_at": state.get("finished_at"),
            "store_count": len(state.get("stores", [])),
            "target_count": len(targets),
            "next_index": next_index,
            "processed_count": _integer(state.get("completed_count"))
            + _integer(state.get("skipped_count"))
            + _integer(state.get("failed_count")),
            "completed_count": _integer(state.get("completed_count")),
            "skipped_count": _integer(state.get("skipped_count")),
            "failed_count": _integer(state.get("failed_count")),
            "remaining_count": max(0, len(targets) - next_index),
            "current_target": state.get("current_target"),
            "usage": state.get("usage", {}),
            "store_progress": list((state.get("store_progress") or {}).values()),
            "last_error": state.get("last_error"),
            "deduplicated_pending_variant_count": _integer(
                (state.get("variant_target_compaction") or {}).get(
                    "removed_pending_count"
                )
            ),
            "recent_results": list(state.get("results", []))[-20:],
            "strict_serial": True,
            "max_concurrency": 1,
            "automatic_retry": False,
            "can_pause": state.get("status") in {"queued", "running"},
            "can_resume": bool(
                state.get("status") in RESUMABLE_BATCH_STATUSES
                and next_index < len(targets)
                and (self._task is None or self._task.done())
            ),
            "can_stop": state.get("status") in ACTIVE_BATCH_STATUSES
            or state.get("status") in PAUSED_BATCH_STATUSES,
            "can_restart": bool(
                state.get("status")
                and state.get("status") not in ACTIVE_BATCH_STATUSES
                and (self._task is None or self._task.done())
            ),
        }

    def start(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_display_name: str,
        actor_is_admin: bool,
        snapshot_id: str,
        _replace_resumable: bool = False,
    ) -> dict[str, Any]:
        with self._state_lock:
            replaceable = bool(
                _replace_resumable
                and self._state.get("status") in RESUMABLE_BATCH_STATUSES
                and (self._task is None or self._task.done())
            )
            if self._is_active_locked() and not replaceable:
                raise SearchRankingBatchConflictError(
                    "已有未结束的搜索定位批次；请先继续或停止该批次"
                )
        if not self.service.runtime.configured_providers:
            raise SearchRankingBatchInputError("当前没有配置可用的图片识别模型，未启动批次")
        preview = self._build_preview(stores)
        if preview["snapshot_id"] != snapshot_id:
            raise SearchRankingBatchInputError(
                "商品资格、主图或费用快照已经变化；请刷新预览后重新确认"
            )
        targets = list(preview.pop("_targets"))
        if not targets:
            raise SearchRankingBatchInputError("当前授权店铺没有符合条件的商品，未启动批次")
        now = _iso_now()
        store_progress = {
            store["code"]: {
                "code": store["code"],
                "display_name": store["display_name"],
                "target_count": sum(
                    1 for target in targets if target["store_code"] == store["code"]
                ),
                "completed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
            }
            for store in preview["stores"]
        }
        with self._state_lock:
            replaceable = bool(
                _replace_resumable
                and self._state.get("status") in RESUMABLE_BATCH_STATUSES
                and (self._task is None or self._task.done())
            )
            if self._is_active_locked() and not replaceable:
                raise SearchRankingBatchConflictError(
                    "已有未结束的搜索定位批次；请先继续或停止该批次"
                )
            self._state = {
                "schema_version": BATCH_SCHEMA_VERSION,
                "batch_id": str(uuid.uuid4()),
                "snapshot_id": snapshot_id,
                "owner_username": actor_username,
                "owner_display_name": actor_display_name or actor_username,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "updated_at": now,
                "finished_at": None,
                "stores": [
                    {"code": item["code"], "display_name": item["display_name"]}
                    for item in preview["stores"]
                ],
                "targets": targets,
                "next_index": 0,
                "completed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "current_target": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_cny": 0.0,
                    "cost_accounting_complete": True,
                },
                "store_progress": store_progress,
                "results": [],
                "last_error": None,
                "pause_requested": False,
                "stop_requested": False,
                "preview": preview,
            }
            self._persist_state()
            self._task = asyncio.create_task(
                self._run_batch(),
                name=f"search-ranking-batch-{self._state['batch_id']}",
            )
        return self.status_payload(
            stores,
            actor_username=actor_username,
            actor_is_admin=actor_is_admin,
        ) or {}

    def restart(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_display_name: str,
        actor_is_admin: bool,
        snapshot_id: str,
    ) -> dict[str, Any]:
        """Create a new batch from index zero after an explicit fresh snapshot check."""

        with self._state_lock:
            self._require_controller_locked(actor_username, actor_is_admin)
            if self._state.get("status") in ACTIVE_BATCH_STATUSES or (
                self._task is not None and not self._task.done()
            ):
                raise SearchRankingBatchConflictError(
                    "当前商品仍在处理；请先暂停或停止，等待状态稳定后再从头开始"
                )
        return self.start(
            stores,
            actor_username=actor_username,
            actor_display_name=actor_display_name,
            actor_is_admin=actor_is_admin,
            snapshot_id=snapshot_id,
            _replace_resumable=True,
        )

    def pause(
        self,
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> None:
        with self._state_lock:
            self._require_controller_locked(actor_username, actor_is_admin)
            if self._state.get("status") not in {"queued", "running"}:
                raise SearchRankingBatchConflictError("当前批次不在可暂停状态")
            self._state["pause_requested"] = True
            self._state["status"] = "pausing"
            self._state["updated_at"] = _iso_now()
            self._persist_state()

    def resume(
        self,
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> None:
        with self._state_lock:
            self._require_controller_locked(actor_username, actor_is_admin)
            if self._state.get("status") not in RESUMABLE_BATCH_STATUSES:
                raise SearchRankingBatchConflictError("当前批次不在可继续状态")
            if self._task is not None and not self._task.done():
                raise SearchRankingBatchConflictError("当前商品仍在收尾，请稍后再继续")
            if _integer(self._state.get("next_index")) >= len(self._state.get("targets", [])):
                raise SearchRankingBatchConflictError("当前批次已没有待处理商品")
            self._state["pause_requested"] = False
            self._state["stop_requested"] = False
            self._state["last_error"] = None
            self._state["status"] = "queued"
            self._state["finished_at"] = None
            self._state["updated_at"] = _iso_now()
            self._persist_state()
            self._task = asyncio.create_task(
                self._run_batch(),
                name=f"search-ranking-batch-{self._state['batch_id']}-resume",
            )

    def stop(
        self,
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> None:
        with self._state_lock:
            self._require_controller_locked(actor_username, actor_is_admin)
            status = self._state.get("status")
            if status in PAUSED_BATCH_STATUSES:
                self._finish_locked("stopped")
                self._persist_state()
                return
            if status not in ACTIVE_BATCH_STATUSES:
                raise SearchRankingBatchConflictError("当前批次不在可停止状态")
            self._state["stop_requested"] = True
            self._state["pause_requested"] = False
            self._state["status"] = "stopping"
            self._state["updated_at"] = _iso_now()
            self._persist_state()

    async def close(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        with self._state_lock:
            self._state["usage"]["cost_accounting_complete"] = False
            self._state["status"] = "interrupted"
            self._state["current_target"] = None
            self._state["last_error"] = (
                "ERP 已停止；系统没有自动续跑或重试正在处理的商品，请恢复服务后人工确认继续。"
            )
            self._state["updated_at"] = _iso_now()
            self._persist_state()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _build_preview(self, stores: Sequence[Any]) -> dict[str, Any]:
        connected_stores = _connected_store_rows(stores)
        if not connected_stores:
            raise SearchRankingBatchInputError("当前账号没有可访问的已接入店铺")

        engine = create_read_only_engine(self.service.database_url)
        targets: list[dict[str, Any]] = []
        store_rows: list[dict[str, Any]] = []
        usage_samples: list[dict[str, float]] = []
        request_samples: list[int] = []
        try:
            for store in connected_stores:
                with store_scope(store["code"]):
                    listing = self.service.list_payload()
                    with Session(engine) as session:
                        analyses = list(
                            session.scalars(
                                select(SearchRankingAnalysis).order_by(
                                    SearchRankingAnalysis.id
                                )
                            )
                        )
                    reusable_keys = {
                        item.cache_key
                        for item in analyses
                        if item.prompt_version == PROMPT_VERSION
                        if item.vision_payload is not None
                        and (
                            item.status == "completed"
                            or (
                                isinstance(item.vision_payload, Mapping)
                                and item.vision_payload.get("vision_stage_completed") is True
                            )
                        )
                    }
                    family_groups: dict[str, list[Mapping[str, Any]]] = {}
                    for item in sorted(listing["items"], key=lambda row: row["offer_id"]):
                        scoped_item = {**item, "store_code": store["code"]}
                        family_groups.setdefault(
                            _product_family_key(scoped_item),
                            [],
                        ).append(item)

                    existing_cache_hits = 0
                    duplicate_reuses = 0
                    fresh_keys: set[str] = set()
                    for family_items in family_groups.values():
                        item = _family_representative(family_items)
                        family_profile = _variant_family_profile(
                            family_items,
                            representative_offer_id=str(item.get("offer_id") or ""),
                        )
                        cache_key = _analysis_cache_key(
                            image_url=str(item.get("image_url") or ""),
                            provider_signature=self.service.runtime.provider_signature,
                            source_title=_variant_family_cache_material(family_profile),
                        )
                        if cache_key in reusable_keys:
                            cache_state = "existing_cache"
                            existing_cache_hits += 1
                        elif cache_key in fresh_keys:
                            cache_state = "same_batch_reuse"
                            duplicate_reuses += 1
                        else:
                            cache_state = "fresh_model"
                            fresh_keys.add(cache_key)
                        targets.append(
                            {
                                "store_code": store["code"],
                                "store_name": store["display_name"],
                                "offer_id": item["offer_id"],
                                "productline_id": item.get("productline_id"),
                                "sku": item.get("sku"),
                                "title": item.get("title"),
                                "image_url": item.get("image_url"),
                                "captured_at": item.get("captured_at"),
                                "preview_cache_state": cache_state,
                                "variant_count": len(family_items),
                                "shared_family_title": family_profile.get("shared_title"),
                                "variant_parameters": [
                                    {
                                        "offer_id": variant.get("offer_id"),
                                        "parameters": variant.get("parameters", []),
                                    }
                                    for variant in family_profile.get("variants", [])
                                    if isinstance(variant, Mapping)
                                ],
                                "variant_offer_ids": [
                                    str(variant.get("offer_id") or "")
                                    for variant in family_items
                                ],
                                "variant_titles": [
                                    str(variant.get("title") or "")
                                    for variant in family_items
                                ],
                            }
                        )
                    for analysis in analyses:
                        if (
                            analysis.prompt_version != PROMPT_VERSION
                            or analysis.vision_reused
                            or not isinstance(
                                analysis.vision_payload,
                                Mapping,
                            )
                        ):
                            continue
                        usage = analysis.vision_payload.get("usage")
                        if not isinstance(usage, Mapping) or _integer(
                            usage.get("total_tokens")
                        ) <= 0:
                            continue
                        usage_samples.append(
                            {
                                "input_tokens": _integer(usage.get("input_tokens")),
                                "output_tokens": _integer(usage.get("output_tokens")),
                                "total_tokens": _integer(usage.get("total_tokens")),
                                "cost_cny": _number(
                                    analysis.vision_payload.get("estimated_cost_cny")
                                ),
                            }
                        )
                        journey = analysis.vision_payload.get("shopper_journey")
                        request_count = (
                            _integer(journey.get("public_request_count"))
                            if isinstance(journey, Mapping)
                            else 0
                        )
                        if request_count > 0:
                            request_samples.append(request_count)
                    store_rows.append(
                        {
                            "code": store["code"],
                            "display_name": store["display_name"],
                            "current_offer_count": listing["eligibility"][
                                "current_offer_count"
                            ],
                            "eligible_offer_count": len(listing["items"]),
                            "eligible_count": len(family_groups),
                            "variant_family_count": sum(
                                1 for items in family_groups.values() if len(items) > 1
                            ),
                            "existing_vision_cache_hit_count": existing_cache_hits,
                            "same_batch_vision_reuse_count": duplicate_reuses,
                            "fresh_vision_count": len(fresh_keys),
                        }
                    )
        finally:
            engine.dispose()

        eligible_count = len(targets)
        fresh_count = sum(item["fresh_vision_count"] for item in store_rows)
        maximum_fresh_count = eligible_count - sum(
            item["existing_vision_cache_hit_count"] for item in store_rows
        )
        input_per_fresh = round(
            statistics.median(
                [item["input_tokens"] for item in usage_samples if item["input_tokens"] > 0]
            )
            if any(item["input_tokens"] > 0 for item in usage_samples)
            else DEFAULT_INPUT_TOKENS_PER_FRESH_IMAGE
        )
        output_per_fresh = round(
            statistics.median(
                [item["output_tokens"] for item in usage_samples if item["output_tokens"] > 0]
            )
            if any(item["output_tokens"] > 0 for item in usage_samples)
            else DEFAULT_OUTPUT_TOKENS_PER_FRESH_IMAGE
        )
        primary = self.service.runtime.primary_provider
        base_cost_per_fresh = (
            input_per_fresh * primary.input_price_cny_per_million
            + output_per_fresh * primary.output_price_cny_per_million
        ) / 1_000_000
        observed_costs = sorted(
            item["cost_cny"] for item in usage_samples if item["cost_cny"] > 0
        )
        if len(observed_costs) >= 2:
            lower_cost, _, upper_cost = statistics.quantiles(
                observed_costs,
                n=4,
                method="inclusive",
            )
        elif observed_costs:
            lower_cost = upper_cost = observed_costs[0]
        else:
            lower_cost = base_cost_per_fresh * 0.85
            upper_cost = base_cost_per_fresh * 1.5
        typical_low = min(base_cost_per_fresh, lower_cost) * fresh_count
        typical_high = max(base_cost_per_fresh, upper_cost) * fresh_count
        conservative_upper = max(
            typical_high,
            (max(observed_costs) if observed_costs else base_cost_per_fresh * 2.5)
            * maximum_fresh_count,
        )

        requests_per_offer = round(
            statistics.median(request_samples)
            if request_samples
            else DEFAULT_PUBLIC_REQUESTS_PER_OFFER,
            1,
        )
        average_interval = (
            self.service.runtime.page_delay_seconds
            + self.service.runtime.page_delay_jitter_seconds / 2
        )
        pacing_floor_hours = eligible_count * requests_per_offer * average_interval / 3_600
        max_observed_requests = max(request_samples, default=52)
        conservative_pacing_hours = (
            eligible_count * max_observed_requests * average_interval / 3_600
        )
        likely_min_hours = max(pacing_floor_hours * 1.15, pacing_floor_hours + 0.5)
        likely_max_hours = max(conservative_pacing_hours, pacing_floor_hours * 2)

        snapshot_material = {
            "provider_signature": self.service.runtime.provider_signature,
            "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
            "public_request_min_interval_seconds": self.service.runtime.page_delay_seconds,
            "public_request_jitter_seconds": self.service.runtime.page_delay_jitter_seconds,
            "targets": targets,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(
                snapshot_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "snapshot_id": snapshot_id,
            "generated_at": _iso_now(),
            "store_count": len(store_rows),
            "stores": store_rows,
            "current_offer_count": sum(
                item["current_offer_count"] for item in store_rows
            ),
            "eligible_offer_count": sum(
                item["eligible_offer_count"] for item in store_rows
            ),
            "eligible_count": eligible_count,
            "variant_family_count": sum(
                item["variant_family_count"] for item in store_rows
            ),
            "existing_vision_cache_hit_count": sum(
                item["existing_vision_cache_hit_count"] for item in store_rows
            ),
            "same_batch_vision_reuse_count": sum(
                item["same_batch_vision_reuse_count"] for item in store_rows
            ),
            "fresh_vision_count": fresh_count,
            "maximum_fresh_vision_count": maximum_fresh_count,
            "estimated_usage": {
                "historical_sample_count": len(usage_samples),
                "input_tokens_per_fresh_image": input_per_fresh,
                "output_tokens_per_fresh_image": output_per_fresh,
                "total_tokens_per_fresh_image": input_per_fresh + output_per_fresh,
                "input_tokens_total": input_per_fresh * fresh_count,
                "output_tokens_total": output_per_fresh * fresh_count,
                "total_tokens": (input_per_fresh + output_per_fresh) * fresh_count,
            },
            "estimated_cost": {
                "currency": "CNY",
                "base_cny": round(base_cost_per_fresh * fresh_count, 2),
                "typical_low_cny": round(typical_low, 2),
                "typical_high_cny": round(typical_high, 2),
                "conservative_upper_cny": round(conservative_upper, 2),
                "primary_provider": primary.name,
                "primary_model": primary.model,
                "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                "input_price_cny_per_million": primary.input_price_cny_per_million,
                "output_price_cny_per_million": primary.output_price_cny_per_million,
                "fallback_may_add_cost": self.service.runtime.fallback_provider is not None,
            },
            "estimated_duration": {
                "historical_request_sample_count": len(request_samples),
                "public_requests_per_offer_median": requests_per_offer,
                "average_interval_seconds": round(average_interval, 2),
                "pacing_floor_hours": round(pacing_floor_hours, 1),
                "likely_min_hours": round(likely_min_hours, 1),
                "likely_max_hours": round(likely_max_hours, 1),
                "note": "总时长区间含模型和网络余量；节流下限只计算公开请求起始间隔。",
            },
            "_targets": targets,
        }

    async def _run_batch(self) -> None:
        try:
            async with self.analysis_lock:
                with self._state_lock:
                    if self._state.get("status") == "interrupted":
                        return
                    self._state["status"] = "running"
                    self._state["started_at"] = self._state.get("started_at") or _iso_now()
                    self._state["updated_at"] = _iso_now()
                    self._persist_state()
                while True:
                    with self._state_lock:
                        if self._state.get("stop_requested"):
                            self._finish_locked("stopped")
                            self._persist_state()
                            return
                        if self._state.get("pause_requested"):
                            self._state["status"] = "paused"
                            self._state["current_target"] = None
                            self._state["updated_at"] = _iso_now()
                            self._persist_state()
                            return
                        targets = self._state.get("targets", [])
                        index = _integer(self._state.get("next_index"))
                        if index >= len(targets):
                            self._finish_locked("completed")
                            self._persist_state()
                            return
                        target = dict(targets[index])
                        self._state["current_target"] = {
                            "index": index + 1,
                            "store_code": target["store_code"],
                            "store_name": target["store_name"],
                            "offer_id": target["offer_id"],
                            "productline_id": target.get("productline_id"),
                            "title": target.get("title"),
                            "variant_count": max(1, _integer(target.get("variant_count"))),
                            "shared_family_title": target.get("shared_family_title"),
                            "variant_parameters": target.get("variant_parameters", []),
                        }
                        self._state["updated_at"] = _iso_now()
                        self._persist_state()

                    detail: Mapping[str, Any] | None = None
                    outcome = "completed"
                    message: str | None = None
                    pause_after_result = False
                    attempt_started_at = _utcnow().replace(tzinfo=None)
                    try:
                        with store_scope(target["store_code"]):
                            detail = await self.service.analyze_offer(target["offer_id"])
                        analysis_payload = (
                            detail.get("analysis") if isinstance(detail, Mapping) else None
                        )
                        recognition = (
                            analysis_payload.get("recognition")
                            if isinstance(analysis_payload, Mapping)
                            else None
                        )
                        if isinstance(recognition, Mapping) and recognition.get(
                            "manual_fact_required"
                        ):
                            outcome = "skipped"
                            message = str(
                                recognition.get("manual_fact_reason")
                                or "缺少关键商品事实，已跳过且未自动重试"
                            )
                    except SearchRankingInputError as exc:
                        outcome = "skipped"
                        message = str(exc)
                        detail = self._accounting_detail_for_failed_target(
                            target,
                            attempt_started_at=attempt_started_at,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (SearchRankingConfigurationError, SearchRankingProviderError) as exc:
                        outcome = "failed"
                        message = str(exc)
                        pause_after_result = True
                        detail = self._accounting_detail_for_failed_target(
                            target,
                            attempt_started_at=attempt_started_at,
                        )
                    except Exception:
                        LOGGER.exception(
                            "search-ranking batch target failed batch=%s store=%s offer=%s",
                            self._state.get("batch_id"),
                            target["store_code"],
                            target["offer_id"],
                        )
                        outcome = "failed"
                        message = "搜索定位批次运行失败，请查看服务日志后再决定是否继续"
                        pause_after_result = True
                        detail = self._accounting_detail_for_failed_target(
                            target,
                            attempt_started_at=attempt_started_at,
                        )

                    usage = _usage_from_detail(detail)
                    result = {
                        "index": index + 1,
                        "store_code": target["store_code"],
                        "store_name": target["store_name"],
                        "offer_id": target["offer_id"],
                        "productline_id": target.get("productline_id"),
                        "title": target.get("title"),
                        "variant_count": max(1, _integer(target.get("variant_count"))),
                        "outcome": outcome,
                        "message": message,
                        "analysis_id": usage["analysis_id"],
                        "vision_reused": usage["vision_reused"],
                        "usage": {
                            "input_tokens": usage["input_tokens"],
                            "output_tokens": usage["output_tokens"],
                            "total_tokens": usage["total_tokens"],
                        },
                        "estimated_cost_cny": usage["estimated_cost_cny"],
                        "cost_accounting_complete": not (
                            outcome == "failed" and detail is None
                        ),
                        "finished_at": _iso_now(),
                    }
                    with self._state_lock:
                        self._record_result_locked(result)
                        self._state["next_index"] = index + 1
                        self._state["current_target"] = None
                        self._state["updated_at"] = _iso_now()
                        if pause_after_result:
                            self._state["status"] = "paused_after_error"
                            self._state["last_error"] = message
                        self._persist_state()
                        if pause_after_result:
                            return
        except asyncio.CancelledError:
            with self._state_lock:
                if self._state.get("status") in ACTIVE_BATCH_STATUSES:
                    self._state["status"] = "interrupted"
                    self._state["current_target"] = None
                    self._state["usage"]["cost_accounting_complete"] = False
                    self._state["last_error"] = (
                        "批次在当前商品完成前被中断；系统没有自动重试，费用记录可能不完整。"
                    )
                    self._state["updated_at"] = _iso_now()
                    self._persist_state()
            raise
        finally:
            current = asyncio.current_task()
            if self._task is current:
                self._task = None

    def _record_result_locked(self, result: Mapping[str, Any]) -> None:
        outcome = str(result["outcome"])
        count_key = {
            "completed": "completed_count",
            "skipped": "skipped_count",
            "failed": "failed_count",
        }[outcome]
        self._state[count_key] = _integer(self._state.get(count_key)) + 1
        store_progress = self._state["store_progress"][result["store_code"]]
        store_progress[count_key] = _integer(store_progress.get(count_key)) + 1
        raw_usage = result.get("usage")
        usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
        totals = self._state["usage"]
        totals["cost_accounting_complete"] = bool(
            totals.get("cost_accounting_complete", True)
            and result.get("cost_accounting_complete", True)
        )
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            totals[key] = _integer(totals.get(key)) + _integer(usage.get(key))
        totals["estimated_cost_cny"] = round(
            _number(totals.get("estimated_cost_cny"))
            + _number(result.get("estimated_cost_cny")),
            6,
        )
        self._state.setdefault("results", []).append(dict(result))

    def _accounting_detail_for_failed_target(
        self,
        target: Mapping[str, Any],
        *,
        attempt_started_at: datetime,
    ) -> Mapping[str, Any] | None:
        """Read incurred usage even when the offer became ineligible after vision."""

        try:
            with store_scope(str(target["store_code"])):
                engine = create_read_only_engine(self.service.database_url)
                try:
                    with Session(engine) as session:
                        productline_id = str(
                            target.get("productline_id") or ""
                        ).strip()
                        target_scope = (
                            SearchRankingAnalysis.productline_id == productline_id
                            if productline_id
                            else SearchRankingAnalysis.offer_id
                            == str(target["offer_id"])
                        )
                        analysis = session.scalar(
                            select(SearchRankingAnalysis)
                            .where(
                                target_scope,
                                SearchRankingAnalysis.created_at >= attempt_started_at,
                            )
                            .order_by(SearchRankingAnalysis.id.desc())
                            .limit(1)
                        )
                finally:
                    engine.dispose()
        except Exception:
            if isinstance(self.service, SearchRankingService):
                LOGGER.exception(
                    "failed to read batch target cost accounting store=%s offer=%s",
                    target.get("store_code"),
                    target.get("offer_id"),
                )
                return None
            # Duck-typed unit services may not expose a real database. Production
            # never accepts an older detail as usage for the current failed attempt.
            with store_scope(str(target["store_code"])):
                return self.service.detail_payload(str(target["offer_id"]))
        if analysis is None:
            return None
        vision = analysis.vision_payload if isinstance(analysis.vision_payload, Mapping) else {}
        raw_usage = vision.get("usage")
        usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
        return {
            "latest_attempt": {
                "id": analysis.id,
                "vision_reused": analysis.vision_reused,
                "usage": dict(usage),
                "estimated_cost_cny": vision.get("estimated_cost_cny"),
            }
        }

    def _finish_locked(self, status: str) -> None:
        self._state["status"] = status
        self._state["current_target"] = None
        self._state["pause_requested"] = False
        self._state["stop_requested"] = False
        self._state["finished_at"] = _iso_now()
        self._state["updated_at"] = self._state["finished_at"]

    def _require_controller_locked(
        self,
        actor_username: str,
        actor_is_admin: bool,
    ) -> None:
        if not self._state:
            raise SearchRankingBatchConflictError("当前没有搜索定位批次")
        if not actor_is_admin and self._state.get("owner_username") != actor_username:
            raise SearchRankingBatchPermissionError("只有批次发起人或管理员可以控制该批次")

    def _is_active_locked(self) -> bool:
        return bool(
            self._state.get("status") in ACTIVE_BATCH_STATUSES
            or self._state.get("status") in RESUMABLE_BATCH_STATUSES
            or (self._task is not None and not self._task.done())
        )

    def _policy_payload(self) -> dict[str, Any]:
        return {
            "scope": "all_accessible_active_connected_stores",
            "target_scope": "one_representative_offer_per_store_productline_id",
            "strict_serial": True,
            "max_concurrency": 1,
            "automatic_retry": False,
            "pause_after_provider_or_network_error": True,
            "reverse_image_search": False,
            "requires_snapshot_confirmation": True,
            "public_request_min_interval_seconds": self.service.runtime.page_delay_seconds,
            "public_request_max_interval_seconds": round(
                self.service.runtime.page_delay_seconds
                + self.service.runtime.page_delay_jitter_seconds,
                2,
            ),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("failed to load search-ranking batch checkpoint")
            return {}
        if not isinstance(payload, dict) or _integer(payload.get("schema_version")) not in {
            1,
            2,
            BATCH_SCHEMA_VERSION,
        }:
            LOGGER.error("ignored unsupported search-ranking batch checkpoint")
            return {}
        previous_version = _integer(payload.get("schema_version"))
        targets_before_compaction = json.dumps(
            payload.get("targets"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        removed = _compact_pending_variant_targets(payload)
        targets_changed = targets_before_compaction != json.dumps(
            payload.get("targets"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload["schema_version"] = BATCH_SCHEMA_VERSION
        self._checkpoint_needs_persist = bool(
            previous_version != BATCH_SCHEMA_VERSION or removed or targets_changed
        )
        return payload

    def _persist_state(self) -> None:
        if not self._state:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


__all__ = [
    "SearchRankingBatchConflictError",
    "SearchRankingBatchController",
    "SearchRankingBatchInputError",
    "SearchRankingBatchPermissionError",
]
