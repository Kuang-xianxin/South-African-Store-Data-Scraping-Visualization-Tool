"""Audited platform-warehouse workflow with guarded Takealot Seller BFF writes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.platform_warehouse.portal import (
    PortalAction,
    PortalAmbiguousWriteError,
    PortalAuthenticationError,
    PortalDisabledError,
    PortalError,
    PortalSessionRegistry,
    TakealotPortalClient,
)
from takealot_ops.platform_warehouse.credentials import (
    PortalCredentialStore,
    WindowsPortalCredentialStore,
    masked_email,
)
from takealot_ops.settings import DashboardSettings, TakealotPortalSettings
from takealot_ops.storage.migrations import (
    create_engine_for_settings,
    create_read_only_engine,
)
from takealot_ops.storage.models import (
    LogisticsProviderSnapshot,
    OfferCurrent,
    PlatformWarehouseDraft,
    PlatformWarehouseDraftAudit,
    PlatformWarehouseDraftLine,
    PlatformWarehouseShipment,
)
from takealot_ops.storage.store_context import current_store_code


DraftAction = Literal["confirm_po", "confirm_shipped", "archive"]


class PlatformWarehouseInputError(ValueError):
    """Raised when a draft or explicit confirmation payload is invalid."""


class PlatformWarehouseConflictError(RuntimeError):
    """Raised when a requested lifecycle action is unsafe or out of order."""


class PlatformWarehouseNotFoundError(LookupError):
    """Raised when a local draft/shipment is absent from the current store."""


class PlatformWarehouseService:
    """Create audited platform drafts and guard later shipment lifecycle writes."""

    def __init__(
        self,
        project_root: Path,
        *,
        portal_registry: PortalSessionRegistry | None = None,
        credential_store: PortalCredentialStore | None = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._portal_settings = TakealotPortalSettings.from_env(self._project_root)
        self._portal = portal_registry or PortalSessionRegistry(
            TakealotPortalClient(self._portal_settings)
        )
        self._credentials = credential_store or WindowsPortalCredentialStore()
        self._write_locks: dict[str, threading.Lock] = {}
        self._write_locks_guard = threading.Lock()

    @property
    def portal_settings(self) -> TakealotPortalSettings:
        return self._portal_settings

    def portal_status(self) -> dict[str, Any]:
        store_code = current_store_code()
        status = self._portal.status(store_code)
        credential_error: str | None = None
        try:
            credential = self._credentials.get(store_code)
        except (OSError, RuntimeError):
            credential = None
            credential_error = "服务器无法读取 Windows 凭据管理器"
        return {
            **status,
            "enabled": self._portal_settings.is_store_enabled(store_code),
            "globally_enabled": self._portal_settings.enabled,
            "base_url": self._portal_settings.base_url,
            "max_total_quantity": self._portal_settings.max_total_quantity,
            "shipped_write_enabled": _env_flag("TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED"),
            "credential_configured": credential is not None,
            "credential_email": masked_email(credential),
            "credential_error": credential_error,
            "credentials_persisted": credential is not None,
        }

    def portal_login(self, email: str, password: str) -> dict[str, Any]:
        self._assert_portal_enabled()
        return self._portal.login(current_store_code(), email, password)

    def portal_verify_otp(self, otp: str) -> dict[str, Any]:
        self._assert_portal_enabled()
        return self._portal.verify_otp(current_store_code(), otp)

    def portal_logout(self) -> dict[str, Any]:
        return self._portal.logout(current_store_code())

    def create_platform_draft_direct(
        self,
        lines: Sequence[Mapping[str, Any]],
        *,
        client_request_id: str,
        actor_user_id: int | None,
        actor_username: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Create a Takealot shipment draft in one explicit click, pausing only for 2FA."""
        self._assert_portal_enabled()
        request_id = _client_request_id(client_request_id)
        normalized = _normalize_lines(lines)
        self._assert_total_quantity(normalized)
        existing = self._draft_for_request(
            request_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        if existing and existing["status"] not in {"awaiting_2fa", "draft", "reviewed"}:
            if existing["status"] in {
                "platform_draft",
                "platform_partial",
                "po_confirmed",
                "shipped",
                "archived",
            }:
                return {
                    "state": "created",
                    "draft": existing,
                    "portal": self.portal_status(),
                }
            raise PlatformWarehouseConflictError(
                "该创建请求已有失败、处理中或结果未知记录；为避免重复Shipment，"
                "禁止用同一请求自动重试，请先到Seller Portal人工核对"
            )

        store_code = current_store_code()
        try:
            self._portal.validated_token(store_code)
        except PortalAuthenticationError:
            session_status = self._portal.status(store_code)
            if session_status["requires_otp"]:
                if existing is None or existing["status"] != "awaiting_2fa":
                    raise PlatformWarehouseConflictError(
                        "当前店铺已有另一笔创建请求等待 2FA，请先完成该验证码"
                    )
                return self._need_2fa_payload(existing)
            try:
                credential = self._credentials.get(store_code)
            except (OSError, RuntimeError) as exc:
                raise PortalAuthenticationError(
                    "服务器无法读取 Windows 凭据管理器中的 Seller Portal 凭据"
                ) from exc
            if credential is None:
                raise PortalAuthenticationError(
                    "当前店铺尚未在服务器 Windows 凭据管理器配置 Seller Portal 凭据"
                )
            session_status = self._portal.login(
                store_code,
                credential.email,
                credential.password,
            )
            if session_status["requires_otp"]:
                draft = existing or self.create_draft(
                    normalized,
                    client_request_id=request_id,
                    initial_status="awaiting_2fa",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=note,
                )
                if draft["status"] != "awaiting_2fa":
                    draft = self._set_draft_status(
                        draft["id"],
                        "awaiting_2fa",
                        action="awaiting_2fa",
                        actor_user_id=actor_user_id,
                        actor_username=actor_username,
                    )
                return self._need_2fa_payload(draft)

        draft = existing or self.create_draft(
            normalized,
            client_request_id=request_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            note=note,
        )
        if draft["status"] == "awaiting_2fa":
            draft = self._set_draft_status(
                draft["id"],
                "draft",
                action="session_reused",
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
        return self._continue_direct_create(
            draft["id"],
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )

    def verify_otp_and_continue_create(
        self,
        draft_id: int,
        otp: str,
        *,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        """Verify the pending login OTP and automatically resume the same draft creation."""
        self._assert_portal_enabled()
        draft = self._load_owned_draft(draft_id, actor_user_id, actor_username)
        if draft["status"] != "awaiting_2fa":
            raise PlatformWarehouseConflictError("该创建请求当前不在等待 2FA 状态")
        self._portal.verify_otp(current_store_code(), otp)
        self._set_draft_status(
            draft_id,
            "draft",
            action="otp_verified",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return self._continue_direct_create(
            draft_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )

    def load(self) -> dict[str, Any]:
        """Return current offers, audited drafts, linked shipments and read-only snapshot."""
        portal_enabled = self._portal_settings.is_store_enabled()
        if portal_enabled:
            capability_message = (
                "当前店铺已启用 Seller Portal BFF。点击一次即校验会话、执行 Takealot "
                "服务端分仓预审并创建平台草稿；只有 Takealot 登录响应要求 2FA 时才暂停并"
                "弹出验证码，验证后自动续接同一请求。不会绕过容量或补货限制，任何不完整"
                "分配都会拒绝建单。"
            )
        elif self._portal_settings.enabled:
            capability_message = (
                "当前店铺不在约平台仓允许列表中，登录、验证码、预审、创建及后续平台动作"
                "均已由服务端禁用。"
            )
        else:
            capability_message = (
                "Seller Portal BFF 接入已安装但总开关默认关闭；开启后仍须把店铺代码加入"
                "允许列表，当前店铺才可执行真实平台动作。"
            )
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_read_only_engine(settings.database_url)
        try:
            with Session(engine) as session:
                offers = session.scalars(
                    select(OfferCurrent).order_by(OfferCurrent.title, OfferCurrent.offer_id)
                ).all()
                drafts = session.scalars(
                    select(PlatformWarehouseDraft).order_by(
                        PlatformWarehouseDraft.created_at.desc(),
                        PlatformWarehouseDraft.id.desc(),
                    )
                ).all()
                shipment_snapshot = session.scalar(
                    select(LogisticsProviderSnapshot).where(
                        LogisticsProviderSnapshot.provider == "takealot"
                    )
                )
                return {
                    "generated_at": _iso(datetime.now(UTC)),
                    "capability": {
                        "write_mode": (
                            "guarded_seller_portal_bff"
                            if portal_enabled
                            else "disabled_by_default"
                        ),
                        "official_shipment_write_supported": portal_enabled,
                        "message": capability_message,
                    },
                    "portal": self.portal_status(),
                    "offers": [_offer_payload(offer) for offer in offers],
                    "drafts": [self._draft_payload(session, draft) for draft in drafts],
                    "platform_shipments": _platform_shipments(shipment_snapshot),
                    "platform_snapshot_synced_at": (
                        _iso(shipment_snapshot.fetched_at) if shipment_snapshot else None
                    ),
                }
        finally:
            engine.dispose()

    def create_draft(
        self,
        lines: Sequence[Mapping[str, Any]],
        *,
        client_request_id: str | None = None,
        initial_status: str = "draft",
        actor_user_id: int | None,
        actor_username: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Persist an auditable, idempotent request before any upstream operation."""
        normalized = _normalize_lines(lines)
        self._assert_total_quantity(normalized)
        request_id = _client_request_id(client_request_id) if client_request_id else None
        if initial_status not in {"draft", "awaiting_2fa"}:
            raise PlatformWarehouseInputError("草稿初始状态无效")
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                if request_id:
                    existing = session.scalar(
                        select(PlatformWarehouseDraft).where(
                            PlatformWarehouseDraft.client_request_id == request_id
                        )
                    )
                    if existing is not None:
                        return self._draft_payload(session, existing)
                offer_ids = [line["offer_id"] for line in normalized]
                offers = session.scalars(
                    select(OfferCurrent).where(OfferCurrent.offer_id.in_(offer_ids))
                ).all()
                offers_by_id = {offer.offer_id: offer for offer in offers}
                missing = [offer_id for offer_id in offer_ids if offer_id not in offers_by_id]
                if missing:
                    raise PlatformWarehouseInputError(
                        f"当前店铺找不到商品：{'、'.join(missing[:5])}"
                    )
                now = datetime.utcnow()
                draft = PlatformWarehouseDraft(
                    draft_number=f"PW-{now:%Y%m%d}-{uuid4().hex[:8].upper()}",
                    client_request_id=request_id,
                    status=initial_status,
                    upstream_mode=(
                        "guarded_bff" if self._portal_settings.enabled else "local_only"
                    ),
                    note=_clean_optional(note, 2000),
                    created_by_user_id=actor_user_id,
                    created_by_username=actor_username,
                    created_at=now,
                    updated_at=now,
                )
                session.add(draft)
                session.flush()
                for line in normalized:
                    offer = offers_by_id[line["offer_id"]]
                    session.add(
                        PlatformWarehouseDraftLine(
                            draft_id=draft.id,
                            offer_id=offer.offer_id,
                            sku=offer.sku,
                            tsin_id=offer.tsin_id,
                            title=offer.title,
                            image_url=offer.image_url,
                            cpt_quantity=line["cpt_quantity"],
                            jhb_quantity=line["jhb_quantity"],
                            dbn_quantity=line["dbn_quantity"],
                        )
                    )
                self._audit(
                    session,
                    draft,
                    action="created",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=draft.note,
                    details={
                        "line_count": len(normalized),
                        "quantities": _quantity_totals(normalized),
                        "upstream_write": False,
                        "client_request_id": request_id,
                        "direct_create": request_id is not None,
                        "safety_max_total": self._portal_settings.max_total_quantity,
                    },
                    now=now,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _assert_total_quantity(self, normalized: Sequence[Mapping[str, Any]]) -> None:
        total_quantity = sum(sum(_line_quantities(line).values()) for line in normalized)
        if total_quantity > self._portal_settings.max_total_quantity:
            raise PlatformWarehouseInputError(
                "本次总数量超过安全上限 "
                f"{self._portal_settings.max_total_quantity}；请拆单并逐单核对"
            )

    def _draft_for_request(
        self,
        request_id: str,
        *,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any] | None:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_read_only_engine(settings.database_url)
        try:
            with Session(engine) as session:
                draft = session.scalar(
                    select(PlatformWarehouseDraft).where(
                        PlatformWarehouseDraft.client_request_id == request_id
                    )
                )
                if draft is None:
                    return None
                same_user = (
                    draft.created_by_user_id == actor_user_id
                    if draft.created_by_user_id is not None
                    else draft.created_by_username == actor_username
                )
                if not same_user:
                    raise PlatformWarehouseConflictError(
                        "创建请求编号已被其他操作者使用"
                    )
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _load_owned_draft(
        self,
        draft_id: int,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_read_only_engine(settings.database_url)
        try:
            with Session(engine) as session:
                draft = session.scalar(
                    select(PlatformWarehouseDraft).where(
                        PlatformWarehouseDraft.id == draft_id
                    )
                )
                if draft is None:
                    raise PlatformWarehouseNotFoundError("当前店铺找不到补货草稿")
                same_user = (
                    draft.created_by_user_id == actor_user_id
                    if draft.created_by_user_id is not None
                    else draft.created_by_username == actor_username
                )
                if not same_user:
                    raise PlatformWarehouseConflictError(
                        "2FA 只能续接当前操作者发起的创建请求"
                    )
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _set_draft_status(
        self,
        draft_id: int,
        status: str,
        *,
        action: str,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                draft = self._locked_draft(session, draft_id)
                draft.status = status
                draft.last_error = None
                draft.updated_at = datetime.utcnow()
                self._audit(
                    session,
                    draft,
                    action=action,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=None,
                    details={"upstream_write": False},
                    now=draft.updated_at,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _need_2fa_payload(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        portal = self.portal_status()
        return {
            "state": "need_2fa",
            "draft": dict(draft),
            "portal": portal,
            "otp_destination": portal.get("otp_destination")
            or portal.get("credential_email"),
        }

    def _continue_direct_create(
        self,
        draft_id: int,
        *,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        reviewed = self.review_draft(
            draft_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        created = self.create_upstream(
            draft_id,
            approval_token=reviewed["approval_token"],
            confirmation_text=reviewed["confirmation_text"],
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return {
            "state": "created",
            "draft": created["draft"],
            "portal": self.portal_status(),
        }

    def review_draft(
        self,
        draft_id: int,
        *,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        """Run Takealot's own allocation review and issue a short-lived create approval."""
        self._assert_portal_enabled()
        store_code = current_store_code()
        token = self._portal.token(store_code)
        with self._store_write_lock(store_code):
            draft_data = self._load_draft_for_upstream(draft_id, expected={"draft", "reviewed"})
            shipment_items = _review_items(draft_data["lines"])
            review_task_id, review_result = self._portal.client.review_shipments(
                token, shipment_items
            )
            facilities = self._portal.client.facilities(token)
            request_params, review_summary = self._validated_review_payload(
                token,
                draft_data["draft_number"],
                draft_data["lines"],
                review_result,
                facilities,
            )
            raw_approval = os.urandom(32).hex()
            canonical_hash = _payload_hash(request_params)
            now = datetime.utcnow()
            expires_at = now + timedelta(minutes=5)
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_engine_for_settings(settings)
            try:
                with Session(engine, expire_on_commit=False) as session, session.begin():
                    draft = self._locked_draft(session, draft_id)
                    if draft.status not in {"draft", "reviewed"}:
                        raise PlatformWarehouseConflictError(
                            f"当前状态为{_status_label(draft.status)}，不能写入新的预审结果"
                        )
                    draft.status = "reviewed"
                    draft.review_task_id = review_task_id
                    draft.review_payload = request_params
                    draft.review_payload_hash = canonical_hash
                    draft.review_approval_hash = _secret_hash(raw_approval)
                    draft.reviewed_at = now
                    draft.review_expires_at = expires_at
                    draft.last_error = None
                    draft.updated_at = now
                    self._audit(
                        session,
                        draft,
                        action="upstream_reviewed",
                        actor_user_id=actor_user_id,
                        actor_username=actor_username,
                        note=None,
                        details={
                            "upstream_write": False,
                            "review_task_id": review_task_id,
                            "payload_hash": canonical_hash,
                            "allocation": review_summary,
                            "expires_at": _iso(expires_at),
                        },
                        now=now,
                    )
                    session.flush()
                    payload = self._draft_payload(session, draft)
            finally:
                engine.dispose()
        return {
            "draft": payload,
            "approval_token": raw_approval,
            "expires_at": _iso(expires_at),
            "allocation": review_summary,
            "confirmation_text": draft_data["draft_number"],
        }

    def create_upstream(
        self,
        draft_id: int,
        *,
        approval_token: str,
        confirmation_text: str,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        """Create Takealot draft shipment(s) once; ambiguous writes are never retried."""
        self._assert_portal_enabled()
        store_code = current_store_code()
        token = self._portal.token(store_code)
        with self._store_write_lock(store_code):
            request_params, draft_number = self._consume_create_approval(
                draft_id,
                approval_token=approval_token,
                confirmation_text=confirmation_text,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
            task_id: int | None = None
            try:
                task_id, result = self._portal.client.create_replenishment(
                    token, request_params
                )
            except PortalAmbiguousWriteError as exc:
                task_id = exc.task_id or task_id
                self._record_upstream_failure(
                    draft_id,
                    status="create_unknown",
                    action="upstream_create_unknown",
                    error=str(exc),
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    task_id=task_id,
                )
                raise PlatformWarehouseConflictError(str(exc)) from exc
            except PortalError as exc:
                self._record_upstream_failure(
                    draft_id,
                    status="create_failed",
                    action="upstream_create_failed",
                    error=str(exc),
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    task_id=task_id,
                )
                raise PlatformWarehouseConflictError(str(exc)) from exc
            assert task_id is not None
            try:
                payload = self._store_create_result(
                    draft_id,
                    task_id=task_id,
                    result=result,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            except PlatformWarehouseConflictError as exc:
                self._record_upstream_failure(
                    draft_id,
                    status="create_unknown",
                    action="upstream_create_unknown",
                    error=str(exc),
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    task_id=task_id,
                )
                raise
        return {"draft": payload, "draft_number": draft_number}

    def prepare_shipment_action(
        self,
        shipment_id: int,
        action: PortalAction,
    ) -> dict[str, Any]:
        self._assert_portal_enabled()
        self._assert_shipment_action_allowed(shipment_id, action)
        return self._portal.prepare_action(current_store_code(), action, shipment_id)

    def execute_shipment_action(
        self,
        shipment_id: int,
        action: PortalAction,
        *,
        approval_token: str,
        confirmation_text: str,
        tracking_reference: str = "",
        my_soh_decrease_warehouse_id: int | None = None,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        self._assert_portal_enabled()
        if confirmation_text.strip() != str(shipment_id):
            raise PlatformWarehouseInputError("二次确认必须完整输入 Shipment ID")
        store_code = current_store_code()
        self._assert_shipment_action_allowed(shipment_id, action)
        self._portal.consume_action_approval(
            store_code, action, shipment_id, approval_token
        )
        token = self._portal.token(store_code)
        with self._store_write_lock(store_code):
            task_id: int | None = None
            result: Mapping[str, Any] | None = None
            clean_tracking = ""
            try:
                if action == "confirm_po":
                    task_id, result = self._portal.client.confirm_po(
                        token,
                        shipment_id,
                        my_soh_decrease_warehouse_id=my_soh_decrease_warehouse_id,
                    )
                elif action == "confirm_shipped":
                    clean_tracking = _clean_required(
                        tracking_reference, "物流单号或发货凭据", 200
                    )
                    self._portal.client.update_tracking(token, shipment_id, clean_tracking)
                    self._portal.client.mark_shipped(token, shipment_id)
                else:
                    self._portal.client.archive(token, shipment_id)
            except PortalAmbiguousWriteError as exc:
                return self._mark_shipment_unknown(
                    shipment_id,
                    action,
                    str(exc),
                    task_id=exc.task_id,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            except PortalError as exc:
                raise PlatformWarehouseConflictError(str(exc)) from exc
            return self._mark_shipment_action_complete(
                shipment_id,
                action,
                task_id=task_id,
                result=result,
                tracking_reference=clean_tracking,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )

    def _assert_portal_enabled(self) -> None:
        store_code = current_store_code()
        if not self._portal_settings.enabled:
            raise PortalDisabledError("约平台仓真实写入总开关当前关闭")
        if not self._portal_settings.is_store_enabled(store_code):
            raise PortalDisabledError(
                f"当前店铺 {store_code} 未启用约平台仓；仅允许已配置的店铺"
            )

    # Retained only for drafts created while the integration is disabled. These methods
    # cannot mutate guarded BFF drafts and are not used by the new upstream UI.
    def confirm_po(
        self,
        draft_id: int,
        *,
        po_number: str,
        platform_shipment_id: int | None,
        actor_user_id: int | None,
        actor_username: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self._local_transition(
            draft_id,
            action="confirm_po",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            note=note,
            po_number=po_number,
            platform_shipment_id=platform_shipment_id,
        )

    def confirm_shipped(
        self,
        draft_id: int,
        *,
        tracking_reference: str,
        actor_user_id: int | None,
        actor_username: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self._local_transition(
            draft_id,
            action="confirm_shipped",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            note=note,
            tracking_reference=tracking_reference,
        )

    def archive(
        self,
        draft_id: int,
        *,
        actor_user_id: int | None,
        actor_username: str,
        note: str,
    ) -> dict[str, Any]:
        return self._local_transition(
            draft_id,
            action="archive",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            note=note,
        )

    def _validated_review_payload(
        self,
        token: str,
        draft_number: str,
        lines: Sequence[Mapping[str, Any]],
        review_result: Mapping[str, Any],
        facilities: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        unallocated = review_result.get("UNALLOCATED")
        if isinstance(unallocated, list) and unallocated:
            offer_ids = sorted(
                {str(row.get("offer_id")) for row in unallocated if isinstance(row, Mapping)}
            )
            raise PlatformWarehouseConflictError(
                "Takealot 服务端未能分配以下商品，已拒绝建单："
                + "、".join(offer_ids[:10])
            )
        facility_map = {
            str(row.get("code") or "").upper(): row
            for row in facilities
            if isinstance(row, Mapping) and row.get("enabled") is not False
        }
        expected = _expected_quantities(lines)
        actual: dict[tuple[int, str], int] = {}
        shipment_summaries: list[dict[str, Any]] = []
        replenishment_by_offer: dict[int, list[dict[str, int]]] = {}
        review_summary: list[dict[str, Any]] = []
        for raw_facility_code, raw_rows in review_result.items():
            facility_code = str(raw_facility_code).upper()
            if facility_code == "UNALLOCATED":
                continue
            facility = facility_map.get(facility_code)
            if facility is None or not isinstance(raw_rows, list) or not raw_rows:
                raise PlatformWarehouseConflictError(
                    f"Takealot 预审返回了未知或空仓库分配：{facility_code}"
                )
            region = facility.get("region")
            if not isinstance(region, Mapping):
                raise PlatformWarehouseConflictError("Takealot 仓库响应缺少 region")
            region_code = str(region.get("code") or "").upper()
            if region_code not in {"CPT", "JHB", "DBN"}:
                raise PlatformWarehouseConflictError(
                    f"Takealot 返回了不支持的区域：{region_code or 'UNKNOWN'}"
                )
            facility_id = _positive_int(facility.get("facility_id"), "facility_id")
            warehouse_id = _positive_int(region.get("region_id"), "warehouse_id")
            quantity_total = 0
            offer_count = 0
            for row in raw_rows:
                if not isinstance(row, Mapping):
                    raise PlatformWarehouseConflictError("Takealot 预审商品行格式无效")
                offer_id = _positive_int(row.get("offer_id"), "offer_id")
                quantity = _positive_int(row.get("quantity"), "quantity")
                key = (offer_id, region_code)
                actual[key] = actual.get(key, 0) + quantity
                replenishment_by_offer.setdefault(offer_id, []).append(
                    {
                        "facility_id": facility_id,
                        "quantity": quantity,
                        "warehouse_id": warehouse_id,
                    }
                )
                quantity_total += quantity
                offer_count += 1
            reference = self._portal.client.default_reference(token, facility_code)
            shipment_summaries.append(
                {
                    "reference": reference,
                    "destination_warehouse": {
                        "name": region_code,
                        "warehouse_id": warehouse_id,
                    },
                    "destination_facility": {
                        "facility_code": facility_code,
                        "facility_id": facility_id,
                    },
                }
            )
            review_summary.append(
                {
                    "facility_code": facility_code,
                    "region": region_code,
                    "reference": reference,
                    "offer_count": offer_count,
                    "quantity": quantity_total,
                }
            )
        if actual != expected:
            raise PlatformWarehouseConflictError(
                "Takealot 服务端预审数量与草稿不完全一致，已拒绝建单；请重新查询商品后再试"
            )
        if not shipment_summaries:
            raise PlatformWarehouseConflictError("Takealot 服务端没有返回可创建的仓库分配")
        request_params = {
            "shipment_summaries": shipment_summaries,
            "replenishment_list": [
                {
                    "seller_listing_id": offer_id,
                    "quantities_sending": quantities,
                }
                for offer_id, quantities in sorted(replenishment_by_offer.items())
            ],
            "erp_reference": draft_number,
        }
        # erp_reference is audit-only and must not be sent to the undocumented BFF.
        request_params.pop("erp_reference")
        return request_params, review_summary

    def _consume_create_approval(
        self,
        draft_id: int,
        *,
        approval_token: str,
        confirmation_text: str,
        actor_user_id: int | None,
        actor_username: str,
    ) -> tuple[dict[str, Any], str]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                draft = self._locked_draft(session, draft_id)
                if draft.status != "reviewed":
                    raise PlatformWarehouseConflictError(
                        f"当前状态为{_status_label(draft.status)}，必须重新预审"
                    )
                if confirmation_text.strip() != draft.draft_number:
                    raise PlatformWarehouseInputError("二次确认必须完整输入草稿号")
                if (
                    draft.review_expires_at is None
                    or draft.review_expires_at < datetime.utcnow()
                ):
                    draft.status = "draft"
                    draft.review_approval_hash = None
                    raise PlatformWarehouseConflictError("预审已过期，请重新预审")
                if not draft.review_approval_hash or not hmac.compare_digest(
                    draft.review_approval_hash, _secret_hash(approval_token)
                ):
                    raise PlatformWarehouseConflictError("预审确认令牌无效")
                request_params = draft.review_payload
                if not isinstance(request_params, dict):
                    raise PlatformWarehouseConflictError("预审载荷缺失，请重新预审")
                if draft.review_payload_hash != _payload_hash(request_params):
                    raise PlatformWarehouseConflictError("预审载荷校验失败，已拒绝建单")
                now = datetime.utcnow()
                draft.status = "creating"
                draft.review_approval_hash = None
                draft.updated_at = now
                self._audit(
                    session,
                    draft,
                    action="upstream_create_confirmed",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=None,
                    details={
                        "upstream_write": True,
                        "payload_hash": draft.review_payload_hash,
                        "automatic_retry": False,
                    },
                    now=now,
                )
                return dict(request_params), draft.draft_number
        finally:
            engine.dispose()

    def _store_create_result(
        self,
        draft_id: int,
        *,
        task_id: int,
        result: Mapping[str, Any],
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        success = result.get("success") is True
        raw_result = result.get("result")
        body = raw_result if isinstance(raw_result, Mapping) else {}
        summaries = body.get("shipment_summaries")
        rows = [row for row in summaries if isinstance(row, Mapping)] if isinstance(summaries, list) else []
        created_rows = [
            row for row in rows if (_optional_positive_int(row.get("quantity_added")) or 0) > 0
        ]
        if not success or not created_rows:
            raise PlatformWarehouseConflictError(
                f"Takealot task {task_id} 未返回已创建的 Shipment；禁止自动重试"
            )
        products_in_draft = body.get("products_in_draft")
        partial = isinstance(products_in_draft, list) and bool(products_in_draft)
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                draft = self._locked_draft(session, draft_id)
                if draft.status != "creating":
                    raise PlatformWarehouseConflictError("本地建单状态已变化，停止写入结果")
                now = datetime.utcnow()
                draft.status = "platform_partial" if partial else "platform_draft"
                draft.create_task_id = task_id
                draft.upstream_result = dict(result)
                draft.last_error = (
                    "Takealot 仅创建了部分商品，请人工核对 products_in_draft"
                    if partial
                    else None
                )
                draft.updated_at = now
                shipment_ids: list[int] = []
                for summary in created_rows:
                    shipment_id = _positive_int(
                        summary.get("shipment_id"), "shipment_id"
                    )
                    shipment_ids.append(shipment_id)
                    destination = summary.get("destination_warehouse")
                    facility = summary.get("destination_facility")
                    region = destination if isinstance(destination, Mapping) else {}
                    facility_row = facility if isinstance(facility, Mapping) else {}
                    existing = session.scalar(
                        select(PlatformWarehouseShipment).where(
                            PlatformWarehouseShipment.platform_shipment_id == shipment_id
                        )
                    )
                    row = existing or PlatformWarehouseShipment(
                        draft_id=draft.id,
                        platform_shipment_id=shipment_id,
                        created_at=now,
                    )
                    row.region = _text(region.get("name"))
                    row.facility_code = _text(facility_row.get("facility_code"))
                    row.facility_id = _optional_positive_int(
                        facility_row.get("facility_id")
                    )
                    row.reference = _text(summary.get("reference"))
                    row.status = "platform_draft"
                    row.raw_summary = dict(summary)
                    row.updated_at = now
                    session.add(row)
                draft.platform_shipment_id = shipment_ids[0]
                self._audit(
                    session,
                    draft,
                    action="upstream_created_partial" if partial else "upstream_created",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=draft.last_error,
                    details={
                        "upstream_write": True,
                        "task_id": task_id,
                        "shipment_ids": shipment_ids,
                        "partial": partial,
                        "automatic_retry": False,
                    },
                    now=now,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _assert_shipment_action_allowed(
        self, shipment_id: int, action: PortalAction
    ) -> None:
        if action == "confirm_shipped" and not _env_flag(
            "TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED"
        ):
            raise PlatformWarehouseConflictError(
                "确认已发货端点仍需人工抓包核验，当前独立安全开关关闭"
            )
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_read_only_engine(settings.database_url)
        try:
            with Session(engine) as session:
                shipment = session.scalar(
                    select(PlatformWarehouseShipment).where(
                        PlatformWarehouseShipment.platform_shipment_id == shipment_id
                    )
                )
                if shipment is None:
                    raise PlatformWarehouseNotFoundError(
                        "当前店铺找不到由本模块创建的 Shipment"
                    )
                expected = {
                    "confirm_po": "platform_draft",
                    "confirm_shipped": "po_confirmed",
                    "archive": "shipped",
                }[action]
                if shipment.status != expected:
                    raise PlatformWarehouseConflictError(
                        f"Shipment 当前状态为{_status_label(shipment.status)}，"
                        f"不能执行{_action_label(action)}"
                    )
        finally:
            engine.dispose()

    def _mark_shipment_action_complete(
        self,
        shipment_id: int,
        action: PortalAction,
        *,
        task_id: int | None,
        result: Mapping[str, Any] | None,
        tracking_reference: str,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                shipment = self._locked_shipment(session, shipment_id)
                expected = {
                    "confirm_po": "platform_draft",
                    "confirm_shipped": "po_confirmed",
                    "archive": "shipped",
                }[action]
                if shipment.status != expected:
                    raise PlatformWarehouseConflictError("Shipment 状态已变化，请刷新后重试")
                now = datetime.utcnow()
                shipment.last_task_id = task_id
                if action == "confirm_po":
                    shipment.status = "po_confirmed"
                    shipment.po_confirmed_at = now
                    shipment.po_number = _find_text_value(
                        result or {}, "purchase_order_number", "po_number"
                    )
                elif action == "confirm_shipped":
                    shipment.status = "shipped"
                    shipment.tracking_reference = tracking_reference
                    shipment.shipped_at = now
                else:
                    shipment.status = "archived"
                    shipment.archived_at = now
                shipment.updated_at = now
                draft = self._locked_draft(session, shipment.draft_id)
                self._update_aggregate_draft_status(session, draft, now)
                self._audit(
                    session,
                    draft,
                    action=f"upstream_{action}",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=None,
                    details={
                        "upstream_write": True,
                        "shipment_id": shipment_id,
                        "task_id": task_id,
                        "automatic_retry": False,
                    },
                    now=now,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _mark_shipment_unknown(
        self,
        shipment_id: int,
        action: PortalAction,
        error: str,
        *,
        task_id: int | None,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                shipment = self._locked_shipment(session, shipment_id)
                now = datetime.utcnow()
                shipment.status = f"{action}_unknown"
                shipment.last_task_id = task_id
                shipment.updated_at = now
                draft = self._locked_draft(session, shipment.draft_id)
                draft.status = "action_unknown"
                draft.last_error = error
                draft.updated_at = now
                self._audit(
                    session,
                    draft,
                    action=f"upstream_{action}_unknown",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=error,
                    details={
                        "upstream_write": True,
                        "shipment_id": shipment_id,
                        "task_id": task_id,
                        "automatic_retry": False,
                    },
                    now=now,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    def _record_upstream_failure(
        self,
        draft_id: int,
        *,
        status: str,
        action: str,
        error: str,
        actor_user_id: int | None,
        actor_username: str,
        task_id: int | None = None,
    ) -> None:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine) as session, session.begin():
                draft = self._locked_draft(session, draft_id)
                now = datetime.utcnow()
                draft.status = status
                if task_id is not None:
                    draft.create_task_id = task_id
                draft.last_error = error
                draft.updated_at = now
                self._audit(
                    session,
                    draft,
                    action=action,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=error,
                    details={"upstream_write": True, "automatic_retry": False},
                    now=now,
                )
        finally:
            engine.dispose()

    def _load_draft_for_upstream(
        self, draft_id: int, *, expected: set[str]
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_read_only_engine(settings.database_url)
        try:
            with Session(engine) as session:
                draft = session.scalar(
                    select(PlatformWarehouseDraft).where(
                        PlatformWarehouseDraft.id == draft_id
                    )
                )
                if draft is None:
                    raise PlatformWarehouseNotFoundError("当前店铺找不到补货草稿")
                if draft.upstream_mode != "guarded_bff":
                    raise PlatformWarehouseConflictError(
                        "该草稿创建时真实接口未启用；请启用后重新创建草稿"
                    )
                if draft.status not in expected:
                    raise PlatformWarehouseConflictError(
                        f"当前状态为{_status_label(draft.status)}，不能执行平台预审"
                    )
                lines = session.scalars(
                    select(PlatformWarehouseDraftLine)
                    .where(PlatformWarehouseDraftLine.draft_id == draft.id)
                    .order_by(PlatformWarehouseDraftLine.id)
                ).all()
                return {
                    "draft_number": draft.draft_number,
                    "lines": [
                        {
                            "offer_id": line.offer_id,
                            "cpt_quantity": line.cpt_quantity,
                            "jhb_quantity": line.jhb_quantity,
                            "dbn_quantity": line.dbn_quantity,
                        }
                        for line in lines
                    ],
                }
        finally:
            engine.dispose()

    def _local_transition(
        self,
        draft_id: int,
        *,
        action: DraftAction,
        actor_user_id: int | None,
        actor_username: str,
        note: str,
        po_number: str = "",
        platform_shipment_id: int | None = None,
        tracking_reference: str = "",
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(self._project_root)
        engine = create_engine_for_settings(settings)
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                draft = self._locked_draft(session, draft_id)
                if draft.upstream_mode != "local_only":
                    raise PlatformWarehouseConflictError(
                        "真实接口草稿不能使用本地状态登记，请执行平台操作"
                    )
                expected = {
                    "confirm_po": "draft",
                    "confirm_shipped": "po_confirmed",
                    "archive": "shipped",
                }[action]
                if draft.status != expected:
                    raise PlatformWarehouseConflictError(
                        f"当前状态为{_status_label(draft.status)}，不能执行{_action_label(action)}"
                    )
                now = datetime.utcnow()
                clean_note = _clean_optional(note, 2000)
                details: dict[str, Any] = {"upstream_write": False}
                if action == "confirm_po":
                    draft.po_number = _clean_required(po_number, "PO Number", 80)
                    if platform_shipment_id is not None and platform_shipment_id < 1:
                        raise PlatformWarehouseInputError("平台 Shipment ID 必须为正整数")
                    draft.platform_shipment_id = platform_shipment_id
                    draft.po_confirmed_at = now
                    draft.status = "po_confirmed"
                elif action == "confirm_shipped":
                    draft.tracking_reference = _clean_required(
                        tracking_reference, "物流单号或发货凭据", 200
                    )
                    draft.shipped_at = now
                    draft.status = "shipped"
                else:
                    if not clean_note:
                        raise PlatformWarehouseInputError("归档时必须填写归档说明")
                    draft.archived_at = now
                    draft.status = "archived"
                draft.note = clean_note or draft.note
                draft.updated_at = now
                self._audit(
                    session,
                    draft,
                    action=action,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    note=clean_note,
                    details=details,
                    now=now,
                )
                session.flush()
                return self._draft_payload(session, draft)
        finally:
            engine.dispose()

    @staticmethod
    def _locked_draft(session: Session, draft_id: int) -> PlatformWarehouseDraft:
        draft = session.scalar(
            select(PlatformWarehouseDraft)
            .where(PlatformWarehouseDraft.id == draft_id)
            .with_for_update()
        )
        if draft is None:
            raise PlatformWarehouseNotFoundError("当前店铺找不到补货草稿")
        return draft

    @staticmethod
    def _locked_shipment(session: Session, shipment_id: int) -> PlatformWarehouseShipment:
        shipment = session.scalar(
            select(PlatformWarehouseShipment)
            .where(PlatformWarehouseShipment.platform_shipment_id == shipment_id)
            .with_for_update()
        )
        if shipment is None:
            raise PlatformWarehouseNotFoundError("当前店铺找不到平台 Shipment")
        return shipment

    def _store_write_lock(self, store_code: str) -> threading.Lock:
        with self._write_locks_guard:
            return self._write_locks.setdefault(store_code, threading.Lock())

    @staticmethod
    def _update_aggregate_draft_status(
        session: Session, draft: PlatformWarehouseDraft, now: datetime
    ) -> None:
        shipments = session.scalars(
            select(PlatformWarehouseShipment).where(
                PlatformWarehouseShipment.draft_id == draft.id
            )
        ).all()
        statuses = {shipment.status for shipment in shipments}
        if statuses and statuses <= {"archived"}:
            draft.status = "archived"
            draft.archived_at = now
        elif statuses and statuses <= {"shipped", "archived"}:
            draft.status = "shipped"
            draft.shipped_at = now
        elif statuses and statuses <= {"po_confirmed", "shipped", "archived"}:
            draft.status = "po_confirmed"
            draft.po_confirmed_at = now
        else:
            draft.status = "platform_draft"
        draft.updated_at = now

    @staticmethod
    def _audit(
        session: Session,
        draft: PlatformWarehouseDraft,
        *,
        action: str,
        actor_user_id: int | None,
        actor_username: str,
        note: str | None,
        details: Mapping[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            PlatformWarehouseDraftAudit(
                draft_id=draft.id,
                action=action,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                note=note,
                details=dict(details),
                created_at=now,
            )
        )

    @staticmethod
    def _draft_payload(session: Session, draft: PlatformWarehouseDraft) -> dict[str, Any]:
        lines = session.scalars(
            select(PlatformWarehouseDraftLine)
            .where(PlatformWarehouseDraftLine.draft_id == draft.id)
            .order_by(PlatformWarehouseDraftLine.id)
        ).all()
        audits = session.scalars(
            select(PlatformWarehouseDraftAudit)
            .where(PlatformWarehouseDraftAudit.draft_id == draft.id)
            .order_by(PlatformWarehouseDraftAudit.created_at.desc())
        ).all()
        shipments = session.scalars(
            select(PlatformWarehouseShipment)
            .where(PlatformWarehouseShipment.draft_id == draft.id)
            .order_by(PlatformWarehouseShipment.id)
        ).all()
        line_payloads = [
            {
                "id": line.id,
                "offer_id": line.offer_id,
                "sku": line.sku,
                "tsin_id": line.tsin_id,
                "title": line.title,
                "image_url": line.image_url,
                "cpt_quantity": line.cpt_quantity,
                "jhb_quantity": line.jhb_quantity,
                "dbn_quantity": line.dbn_quantity,
                "total_quantity": sum(_line_quantities(line).values()),
            }
            for line in lines
        ]
        return {
            "id": draft.id,
            "draft_number": draft.draft_number,
            "client_request_id": draft.client_request_id,
            "status": draft.status,
            "status_label": _status_label(draft.status),
            "upstream_mode": draft.upstream_mode,
            "po_number": draft.po_number,
            "platform_shipment_id": draft.platform_shipment_id,
            "tracking_reference": draft.tracking_reference,
            "review_task_id": draft.review_task_id,
            "reviewed_at": _iso(draft.reviewed_at),
            "review_expires_at": _iso(draft.review_expires_at),
            "create_task_id": draft.create_task_id,
            "last_error": draft.last_error,
            "note": draft.note,
            "created_by": draft.created_by_username,
            "created_at": _iso(draft.created_at),
            "updated_at": _iso(draft.updated_at),
            "po_confirmed_at": _iso(draft.po_confirmed_at),
            "shipped_at": _iso(draft.shipped_at),
            "archived_at": _iso(draft.archived_at),
            "line_count": len(line_payloads),
            "quantity_totals": _quantity_totals(line_payloads),
            "lines": line_payloads,
            "shipments": [
                {
                    "shipment_id": shipment.platform_shipment_id,
                    "region": shipment.region,
                    "facility_code": shipment.facility_code,
                    "facility_id": shipment.facility_id,
                    "reference": shipment.reference,
                    "status": shipment.status,
                    "status_label": _status_label(shipment.status),
                    "po_number": shipment.po_number,
                    "tracking_reference": shipment.tracking_reference,
                    "last_task_id": shipment.last_task_id,
                    "updated_at": _iso(shipment.updated_at),
                }
                for shipment in shipments
            ],
            "audits": [
                {
                    "id": audit.id,
                    "action": audit.action,
                    "action_label": _action_label(audit.action),
                    "actor_username": audit.actor_username,
                    "note": audit.note,
                    "details": audit.details,
                    "created_at": _iso(audit.created_at),
                }
                for audit in audits
            ],
        }


def _normalize_lines(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not lines:
        raise PlatformWarehouseInputError("补货清单至少需要一个商品")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines, start=1):
        offer_id = str(line.get("offer_id") or "").strip()
        if not offer_id:
            raise PlatformWarehouseInputError(f"第 {index} 行缺少 Offer ID")
        if offer_id in seen:
            raise PlatformWarehouseInputError(f"商品 {offer_id} 重复出现")
        seen.add(offer_id)
        quantities = {
            region: _quantity(line.get(region), index, region)
            for region in ("cpt_quantity", "jhb_quantity", "dbn_quantity")
        }
        if sum(quantities.values()) < 1:
            raise PlatformWarehouseInputError(
                f"第 {index} 行至少填写一个仓库的补货数量"
            )
        normalized.append({"offer_id": offer_id, **quantities})
    return normalized


def _quantity(value: Any, row: int, field: str) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise PlatformWarehouseInputError(f"第 {row} 行 {field} 必须为整数") from exc
    if parsed < 0 or parsed > 10_000:
        raise PlatformWarehouseInputError(
            f"第 {row} 行补货数量必须在 0 至 10000 之间"
        )
    return parsed


def _review_items(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in lines:
        offer_id = _positive_int(line.get("offer_id"), "offer_id")
        for region, field in (
            ("CPT", "cpt_quantity"),
            ("JHB", "jhb_quantity"),
            ("DBN", "dbn_quantity"),
        ):
            quantity = int(line.get(field) or 0)
            if quantity:
                items.append(
                    {"offer_id": offer_id, "region": region, "quantity": quantity}
                )
    return items


def _expected_quantities(
    lines: Sequence[Mapping[str, Any]],
) -> dict[tuple[int, str], int]:
    return {
        (item["offer_id"], item["region"]): item["quantity"]
        for item in _review_items(lines)
    }


def _offer_payload(offer: OfferCurrent) -> dict[str, Any]:
    return {
        "offer_id": offer.offer_id,
        "sku": offer.sku,
        "tsin_id": offer.tsin_id,
        "title": offer.title,
        "image_url": offer.image_url,
        "status": offer.status,
        "total_stock": offer.total_stock,
        "takealot_available_stock": offer.takealot_available_stock,
        "takealot_stock_on_way": offer.takealot_stock_on_way,
        "takealot_stock_in_receiving": offer.takealot_stock_in_receiving,
        "official_warehouse_capacity": None,
        "capacity_reason": (
            "最终可约数量只认 Takealot 服务端预审；ERP 不提供或绕过容量判断。"
        ),
    }


def _platform_shipments(
    snapshot: LogisticsProviderSnapshot | None,
) -> list[dict[str, Any]]:
    if snapshot is None or not isinstance(snapshot.payload, Mapping):
        return []
    raw = snapshot.payload.get("_raw_shipments")
    if not isinstance(raw, list):
        return []
    projected: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        items = row.get("shipment_items")
        item_rows = items if isinstance(items, list) else []
        projected.append(
            {
                "shipment_id": _optional_positive_int(row.get("shipment_id")),
                "reference": _text(row.get("reference")),
                "purchase_order_number": _text(row.get("purchase_order_number")),
                "destination_region": _text(row.get("destination_region")),
                "purchase_order_state": _text(row.get("purchase_order_state")),
                "shipment_type": _text(row.get("shipment_type")),
                "shipped": bool(row.get("shipped")),
                "archived": bool(row.get("archived")),
                "cancelled": bool(row.get("cancelled")),
                "due_date": _text(row.get("due_date")),
                "created_at": _text(row.get("created_at")),
                "date_unloaded": _text(row.get("date_unloaded")),
                "tracking_info": _text(row.get("tracking_info")),
                "sku_lines": len(item_rows),
                "quantity_sending": sum(
                    _optional_positive_int(item.get("quantity_sending")) or 0
                    for item in item_rows
                    if isinstance(item, Mapping)
                ),
                "quantity_received": sum(
                    _optional_positive_int(item.get("purchase_order_quantity_received"))
                    or 0
                    for item in item_rows
                    if isinstance(item, Mapping)
                ),
            }
        )
    projected.sort(key=lambda item: item["shipment_id"] or 0, reverse=True)
    return projected


def _quantity_totals(lines: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        region: sum(int(line.get(region) or 0) for line in lines)
        for region in ("cpt_quantity", "jhb_quantity", "dbn_quantity")
    }


def _line_quantities(line: Any) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for region in ("cpt_quantity", "jhb_quantity", "dbn_quantity"):
        raw_value = line.get(region) if isinstance(line, Mapping) else getattr(line, region)
        quantities[region] = int(raw_value or 0)
    return quantities


def _status_label(status: str) -> str:
    return {
        "awaiting_2fa": "等待 2FA 验证",
        "draft": "待创建请求",
        "reviewed": "平台预审通过",
        "creating": "平台建单处理中",
        "platform_draft": "平台草稿",
        "platform_partial": "平台部分创建",
        "create_failed": "平台建单失败",
        "create_unknown": "平台建单结果未知",
        "po_confirmed": "PO 已确认",
        "shipped": "已发货",
        "archived": "已归档",
        "action_unknown": "平台操作结果未知",
    }.get(status, status)


def _action_label(action: str) -> str:
    return {
        "created": "记录创建请求",
        "awaiting_2fa": "等待 2FA 验证",
        "otp_verified": "2FA 验证通过",
        "session_reused": "复用有效 Portal 会话",
        "confirm_po": "确认 PO",
        "confirm_shipped": "确认已发货",
        "archive": "确认归档",
        "upstream_reviewed": "Takealot 服务端预审",
        "upstream_create_confirmed": "直接创建请求确认",
        "upstream_created": "平台草稿创建成功",
        "upstream_created_partial": "平台草稿部分创建",
        "upstream_create_failed": "平台建单失败",
        "upstream_create_unknown": "平台建单结果未知",
        "upstream_confirm_po": "平台确认 PO",
        "upstream_confirm_shipped": "平台确认已发货",
        "upstream_archive": "平台确认归档",
    }.get(action, action)


def _clean_required(value: str, label: str, maximum: int) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise PlatformWarehouseInputError(f"请填写{label}")
    if len(cleaned) > maximum:
        raise PlatformWarehouseInputError(f"{label}最多 {maximum} 个字符")
    return cleaned


def _client_request_id(value: str) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (ValueError, AttributeError) as exc:
        raise PlatformWarehouseInputError("创建请求编号格式无效") from exc


def _clean_optional(value: str | None, maximum: int) -> str | None:
    cleaned = str(value or "").strip()
    if len(cleaned) > maximum:
        raise PlatformWarehouseInputError(f"内容最多 {maximum} 个字符")
    return cleaned or None


def _positive_int(value: Any, label: str) -> int:
    parsed = _optional_positive_int(value)
    if parsed is None:
        raise PlatformWarehouseConflictError(f"Takealot 返回了无效的 {label}")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _text(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_text_value(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _text(payload.get(key))
        if value:
            return value
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = _find_text_value(value, *keys)
            if nested:
                return nested
    return None


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
