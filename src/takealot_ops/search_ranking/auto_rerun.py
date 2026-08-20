"""Consume one local, auditable request to start a fresh Terra full rerun."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from takealot_ops.erp.permissions import SEARCH_RANKING_RUN
from takealot_ops.search_ranking.codex_cli import (
    CODEX_TERRA_MODEL,
    CODEX_WEEKLY_BUDGET_PERCENT,
)


AUTO_RERUN_SCHEMA_VERSION = 1
AUTO_RERUN_FILENAME = "search-ranking-terra-auto-rerun.json"


class SearchRankingAutoRerunError(RuntimeError):
    """The one-time local rerun request is malformed or unauthorized."""


class _BatchController(Protocol):
    def preview_payload(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_is_admin: bool,
    ) -> dict[str, Any]: ...

    def start(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_display_name: str,
        actor_is_admin: bool,
        snapshot_id: str,
    ) -> dict[str, Any]: ...

    def restart(
        self,
        stores: Sequence[Any],
        *,
        actor_username: str,
        actor_display_name: str,
        actor_is_admin: bool,
        snapshot_id: str,
    ) -> dict[str, Any]: ...


def consume_auto_rerun_request(
    request_path: Path,
    *,
    controller: _BatchController,
    users: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Start one new index-zero batch when a valid pending marker exists."""

    if not request_path.exists():
        return None
    request = _load_request(request_path)
    if request.get("status") != "pending":
        return None
    try:
        _validate_request_contract(request)
        actor = _select_actor(request, users)
        actor_username = str(actor["username"])
        actor_display_name = str(actor.get("display_name") or actor_username)
        stores = actor.get("accessible_stores")
        if not isinstance(stores, Sequence) or isinstance(stores, (str, bytes)) or not stores:
            raise SearchRankingAutoRerunError("自动全量重跑账号没有可访问店铺")

        preview_payload = controller.preview_payload(
            stores,
            actor_username=actor_username,
            actor_is_admin=True,
        )
        preview = preview_payload.get("preview")
        if not isinstance(preview, Mapping):
            raise SearchRankingAutoRerunError("自动全量重跑没有取得有效批次预览")
        snapshot_id = str(preview.get("snapshot_id") or "")
        if len(snapshot_id) != 64:
            raise SearchRankingAutoRerunError("自动全量重跑快照ID无效")

        current_batch = preview_payload.get("batch")
        operation = "restart" if isinstance(current_batch, Mapping) else "start"
        starter = controller.restart if operation == "restart" else controller.start
        batch = starter(
            stores,
            actor_username=actor_username,
            actor_display_name=actor_display_name,
            actor_is_admin=True,
            snapshot_id=snapshot_id,
        )
        completed_request = {
            **request,
            "status": "started",
            "operation": operation,
            "started_at": _iso_now(),
            "batch_id": str(batch.get("batch_id") or ""),
            "target_count": int(batch.get("target_count") or 0),
            "snapshot_id": snapshot_id,
        }
        completed_request.pop("error", None)
        _persist_request(request_path, completed_request)
        return batch
    except Exception as exc:
        failed_request = {
            **request,
            "status": "failed",
            "failed_at": _iso_now(),
            "error": str(exc)[:500] or exc.__class__.__name__,
        }
        _persist_request(request_path, failed_request)
        raise


def _validate_request_contract(request: Mapping[str, Any]) -> None:
    if request.get("schema_version") != AUTO_RERUN_SCHEMA_VERSION:
        raise SearchRankingAutoRerunError("自动全量重跑请求版本无效")
    if request.get("model") != CODEX_TERRA_MODEL:
        raise SearchRankingAutoRerunError("自动全量重跑只允许 gpt-5.6-terra")
    if request.get("weekly_budget_percent") != CODEX_WEEKLY_BUDGET_PERCENT:
        raise SearchRankingAutoRerunError("自动全量重跑只允许10个百分点周额度")
    if not str(request.get("actor_username") or "").strip():
        raise SearchRankingAutoRerunError("自动全量重跑缺少发起账号")


def _select_actor(
    request: Mapping[str, Any],
    users: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    requested_username = str(request.get("actor_username") or "").strip().casefold()
    actor = next(
        (
            user
            for user in users
            if str(user.get("username") or "").strip().casefold() == requested_username
        ),
        None,
    )
    if actor is None or not bool(actor.get("active")):
        raise SearchRankingAutoRerunError("自动全量重跑发起账号不存在或已停用")
    permissions = actor.get("permissions")
    if actor.get("role") != "admin" or not isinstance(permissions, Sequence):
        raise SearchRankingAutoRerunError("自动全量重跑必须由管理员账号发起")
    if SEARCH_RANKING_RUN not in permissions:
        raise SearchRankingAutoRerunError("自动全量重跑账号没有搜索定位运行权限")
    return actor


def _load_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SearchRankingAutoRerunError("自动全量重跑请求无法安全读取") from exc
    if not isinstance(payload, dict):
        raise SearchRankingAutoRerunError("自动全量重跑请求格式无效")
    return payload


def _persist_request(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AUTO_RERUN_FILENAME",
    "AUTO_RERUN_SCHEMA_VERSION",
    "SearchRankingAutoRerunError",
    "consume_auto_rerun_request",
]
