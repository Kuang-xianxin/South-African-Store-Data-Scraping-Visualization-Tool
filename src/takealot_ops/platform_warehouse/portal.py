"""Narrow, safety-first client for Takealot Seller Portal shipment operations.

This module intentionally exposes only the shipment calls required by the ERP.  It does
not accept arbitrary URLs, does not persist credentials/tokens, and never retries writes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urljoin

import httpx

from takealot_ops.settings import TakealotPortalSettings


PortalAction = Literal["confirm_po", "confirm_shipped", "archive"]


class PortalError(RuntimeError):
    """Base error for bounded Seller Portal calls."""


class PortalDisabledError(PortalError):
    """Raised while the integration kill switch is off."""


class PortalAuthenticationError(PortalError):
    """Raised when an in-memory Seller Portal session is unavailable or rejected."""


class PortalTaskError(PortalError):
    """Raised when an asynchronous Seller Portal task fails or times out."""


class PortalAmbiguousWriteError(PortalError):
    """Raised when a write may have reached Takealot but no response was received."""

    def __init__(self, message: str, *, task_id: int | None = None) -> None:
        super().__init__(message)
        self.task_id = task_id


@dataclass(frozen=True)
class PortalSession:
    token: str | None = None
    expires_at: int | None = None
    pending_session_id: str | None = None
    pending_destination: str | None = None
    identity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PortalActionApproval:
    action: PortalAction
    shipment_id: int
    token_hash: str
    expires_at: float
    preview: Mapping[str, Any] | None


ClientFactory = Callable[[float], httpx.Client]


class TakealotPortalClient:
    """Exact-path client for the currently observed Seller Portal BFF contract."""

    def __init__(
        self,
        settings: TakealotPortalSettings,
        *,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or _default_client_factory(settings.base_url)
        self._sleep = sleep

    def login(self, email: str, password: str) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            "/v1/login",
            json={"email": email, "password": password},
            write=False,
        )

    def verify_login_otp(self, session_id: str, otp: str) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            "/v1/otp/verify_login",
            json={
                "otp": otp,
                "session_id": session_id,
                "device_name": "Takealot ERP local session",
                "remember_device": False,
            },
            write=False,
        )

    def logout(self, token: str) -> None:
        try:
            self._request_json(
                "POST",
                "/v1/logout",
                token=token,
                json=None,
                write=False,
                accepted_statuses={200, 401},
            )
        except PortalError:
            # The local token is cleared by the registry regardless of upstream reachability.
            pass

    def whoami(self, token: str) -> Mapping[str, Any]:
        return self._request_json("GET", "/v2/whoami", token=token)

    def facilities(self, token: str) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "/v2/shipment/facilities", token=token)
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise PortalError("Takealot 仓库响应缺少 results")
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def default_reference(self, token: str, facility_code: str) -> str:
        safe_code = quote(facility_code, safe="")
        payload = self._request_json(
            "GET",
            f"/v1/shipment/shipment_default_reference?facility_code={safe_code}",
            token=token,
        )
        reference = str(payload.get("reference") or "").strip()
        if len(reference) < 4 or len(reference) > 200:
            raise PortalError("Takealot 未返回有效的 Shipment reference")
        return reference

    def review_shipments(
        self,
        token: str,
        shipment_items: list[dict[str, Any]],
    ) -> tuple[int, Mapping[str, Any]]:
        started = self._request_json(
            "POST",
            "/v2/shipment/shipments_review",
            token=token,
            json={"data": {"shipment_items": shipment_items}},
            write=True,
        )
        task_id = _required_positive_int(started.get("task_id"), "review task_id")
        self._poll_task(token, task_id, status_path=f"/v2/task/{task_id}/status")
        result = self._request_json(
            "GET",
            f"/v2/task/{task_id}/shipment/download",
            token=token,
        )
        return task_id, result

    def create_replenishment(
        self,
        token: str,
        request_params: Mapping[str, Any],
    ) -> tuple[int, Mapping[str, Any]]:
        started = self._request_json(
            "POST",
            "/v1/task/shipment",
            token=token,
            json={"task_type_id": 22, "request_params": dict(request_params)},
            write=True,
        )
        task_id = _task_id(started)
        try:
            result = self._shipment_task_result(token, task_id)
        except PortalTaskError as exc:
            raise PortalAmbiguousWriteError(
                f"Takealot 建单 task {task_id} 未取得最终结果；禁止自动重试",
                task_id=task_id,
            ) from exc
        return task_id, result

    def confirm_preview(self, token: str, shipment_id: int) -> Mapping[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/shipment/{shipment_id}/confirm/preview",
            token=token,
        )

    def confirm_po(
        self,
        token: str,
        shipment_id: int,
        *,
        my_soh_decrease_warehouse_id: int | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        request_params: dict[str, Any] = {"shipment_id": shipment_id}
        if my_soh_decrease_warehouse_id is not None:
            request_params["my_soh_decrease_warehouse_id"] = my_soh_decrease_warehouse_id
        started = self._request_json(
            "POST",
            "/v1/task/shipment",
            token=token,
            json={"task_type_id": 21, "request_params": request_params},
            write=True,
        )
        task_id = _task_id(started)
        try:
            result = self._shipment_task_result(token, task_id)
        except PortalTaskError as exc:
            raise PortalAmbiguousWriteError(
                f"Takealot 确认 PO task {task_id} 未取得最终结果；禁止自动重试",
                task_id=task_id,
            ) from exc
        return task_id, result

    def update_tracking(self, token: str, shipment_id: int, tracking_info: str) -> None:
        self._request_json(
            "PUT",
            f"/v1/shipment/{shipment_id}/tracking_info",
            token=token,
            json={"tracking_info": tracking_info or " "},
            write=True,
        )

    def mark_shipped(self, token: str, shipment_id: int) -> None:
        if not _env_flag("TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED"):
            raise PortalDisabledError(
                "确认已发货端点仍需人工抓包核验；请核验后显式启用 TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED"
            )
        self._request_json(
            "PUT",
            f"/v1/shipment/{shipment_id}/shipped?status=true",
            token=token,
            write=True,
        )

    def archive(self, token: str, shipment_id: int) -> None:
        self._request_json(
            "PUT",
            f"/v1/shipment/archived?shipment_ids={shipment_id}&status=true",
            token=token,
            write=True,
        )

    def _shipment_task_result(
        self,
        token: str,
        task_id: int,
    ) -> Mapping[str, Any]:
        self._poll_task(
            token,
            task_id,
            status_path=f"/v1/shipment/task/{task_id}/status",
        )
        return self._request_json(
            "GET",
            f"/v1/shipment/task/{task_id}/result",
            token=token,
        )

    def _poll_task(self, token: str, task_id: int, *, status_path: str) -> None:
        deadline = time.monotonic() + self._settings.task_timeout_seconds
        while True:
            status = self._request_json("GET", status_path, token=token)
            state = _task_state(status)
            if state == "success":
                return
            if state == "failed":
                raise PortalTaskError(f"Takealot task {task_id} 执行失败")
            if time.monotonic() >= deadline:
                raise PortalTaskError(
                    f"Takealot task {task_id} 在安全等待窗口内未完成；未自动重试写入"
                )
            self._sleep(0.5)

    def _request_json(
        self,
        method: Literal["GET", "POST", "PUT"],
        path: str,
        *,
        token: str | None = None,
        json: Any = None,
        write: bool = False,
        accepted_statuses: set[int] | None = None,
    ) -> Mapping[str, Any]:
        if not self._settings.enabled:
            raise PortalDisabledError("Takealot Seller Portal 写入总开关当前关闭")
        _validate_path(path)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with self._client_factory(self._settings.request_timeout_seconds) as client:
                response = client.request(method, path, headers=headers, json=json)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if write:
                raise PortalAmbiguousWriteError(
                    "Takealot 写请求未收到明确响应；为避免重复建单，系统不会自动重试"
                ) from exc
            raise PortalError("Takealot Seller Portal 网络请求失败") from exc
        allowed = accepted_statuses or set(range(200, 300))
        if response.status_code not in allowed:
            if response.status_code in {401, 403}:
                raise PortalAuthenticationError("Takealot Seller Portal 会话已失效或权限不足")
            raise PortalError(f"Takealot Seller Portal 返回 HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise PortalError("Takealot Seller Portal 返回了非 JSON 响应") from exc
        if not isinstance(payload, Mapping):
            raise PortalError("Takealot Seller Portal 返回格式不符合预期")
        return dict(payload)


class PortalSessionRegistry:
    """Per-process, per-store Seller Portal sessions and short-lived action approvals."""

    def __init__(self, client: TakealotPortalClient) -> None:
        self.client = client
        self._sessions: dict[str, PortalSession] = {}
        self._approvals: dict[tuple[str, PortalAction, int], PortalActionApproval] = {}
        self._lock = threading.RLock()

    def status(self, store_code: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(store_code)
            authenticated = bool(session and session.token and not _expired(session.expires_at))
            return {
                "authenticated": authenticated,
                "requires_otp": bool(session and session.pending_session_id and not authenticated),
                "otp_destination": (
                    session.pending_destination
                    if session and session.pending_session_id and not authenticated
                    else None
                ),
                "expires_at": (
                    datetime.fromtimestamp(session.expires_at, tz=UTC).isoformat()
                    if session and session.expires_at
                    else None
                ),
                "identity": _safe_identity(session.identity if session else None),
                "credentials_persisted": False,
            }

    def login(self, store_code: str, email: str, password: str) -> dict[str, Any]:
        clean_email = email.strip()
        if not clean_email or not password:
            raise PortalAuthenticationError("Seller Portal 邮箱和密码不能为空")
        result = self.client.login(clean_email, password)
        requires_otp = result.get("requires_2fa") is True or result.get("requires2FA") is True
        if requires_otp:
            pending = str(result.get("session_id") or result.get("sessionId") or "").strip()
            if not pending:
                raise PortalAuthenticationError("Takealot 未返回 OTP session_id")
            with self._lock:
                self._sessions[store_code] = PortalSession(
                    pending_session_id=pending,
                    pending_destination=str(result.get("destination") or "").strip() or None,
                )
            return self.status(store_code)
        self._store_authenticated(store_code, result)
        return self.status(store_code)

    def verify_otp(self, store_code: str, otp: str) -> dict[str, Any]:
        with self._lock:
            pending = self._sessions.get(store_code)
        if pending is None or not pending.pending_session_id:
            raise PortalAuthenticationError("当前店铺没有待验证的 OTP 会话")
        clean_otp = otp.strip()
        if not clean_otp or len(clean_otp) > 12:
            raise PortalAuthenticationError("OTP 格式无效")
        result = self.client.verify_login_otp(pending.pending_session_id, clean_otp)
        self._store_authenticated(store_code, result)
        return self.status(store_code)

    def logout(self, store_code: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(store_code, None)
            for key in [key for key in self._approvals if key[0] == store_code]:
                self._approvals.pop(key, None)
        if session and session.token:
            self.client.logout(session.token)
        return self.status(store_code)

    def token(self, store_code: str) -> str:
        with self._lock:
            session = self._sessions.get(store_code)
            if session and session.token and not _expired(session.expires_at):
                return session.token
            if session and _expired(session.expires_at):
                self._sessions.pop(store_code, None)
        raise PortalAuthenticationError("请先在本机登录当前店铺的 Takealot Seller Portal")

    def validated_token(self, store_code: str) -> str:
        """Confirm the in-memory token is still accepted before any shipment write."""
        token = self.token(store_code)
        try:
            identity = self.client.whoami(token)
        except PortalAuthenticationError:
            with self._lock:
                current = self._sessions.get(store_code)
                if current and current.token == token:
                    self._sessions.pop(store_code, None)
            raise
        with self._lock:
            current = self._sessions.get(store_code)
            if current and current.token == token:
                self._sessions[store_code] = PortalSession(
                    token=current.token,
                    expires_at=current.expires_at,
                    identity=identity,
                )
        return token

    def prepare_action(
        self,
        store_code: str,
        action: PortalAction,
        shipment_id: int,
    ) -> dict[str, Any]:
        token = self.token(store_code)
        preview: Mapping[str, Any] | None = None
        if action == "confirm_po":
            preview = self.client.confirm_preview(token, shipment_id)
        raw_token = os.urandom(32).hex()
        approval = PortalActionApproval(
            action=action,
            shipment_id=shipment_id,
            token_hash=_token_hash(raw_token),
            expires_at=time.time() + 300,
            preview=preview,
        )
        with self._lock:
            self._approvals[(store_code, action, shipment_id)] = approval
        return {
            "action": action,
            "shipment_id": shipment_id,
            "approval_token": raw_token,
            "expires_at": datetime.fromtimestamp(approval.expires_at, tz=UTC).isoformat(),
            "preview": preview,
        }

    def consume_action_approval(
        self,
        store_code: str,
        action: PortalAction,
        shipment_id: int,
        raw_token: str,
    ) -> None:
        key = (store_code, action, shipment_id)
        with self._lock:
            approval = self._approvals.pop(key, None)
        if approval is None or approval.expires_at < time.time():
            raise PortalError("操作确认已过期，请重新预检")
        if not hmac.compare_digest(approval.token_hash, _token_hash(raw_token)):
            raise PortalError("操作确认令牌无效")

    def _store_authenticated(self, store_code: str, result: Mapping[str, Any]) -> None:
        token = str(result.get("api_key") or "").strip()
        if not token:
            raise PortalAuthenticationError("Takealot 登录响应缺少 api_key")
        expires_at = _optional_int(result.get("expires"))
        identity = self.client.whoami(token)
        with self._lock:
            self._sessions[store_code] = PortalSession(
                token=token,
                expires_at=expires_at,
                identity=identity,
            )


def _default_client_factory(base_url: str) -> ClientFactory:
    fixed_base = base_url.rstrip("/") + "/"

    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url=fixed_base,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "takealot-erp-platform-warehouse/1.0"},
        )

    return factory


def _validate_path(path: str) -> None:
    joined = urljoin("https://seller-api.takealot.com/", path.lstrip("/"))
    if not path.startswith("/") or not joined.startswith("https://seller-api.takealot.com/"):
        raise PortalError("Seller Portal 请求路径不在固定主机范围内")


def _task_id(payload: Mapping[str, Any]) -> int:
    return _required_positive_int(payload.get("task_id") or payload.get("taskId"), "task_id")


def _required_positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PortalError(f"Takealot 响应缺少有效 {label}") from exc
    if parsed < 1:
        raise PortalError(f"Takealot 响应缺少有效 {label}")
    return parsed


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _task_state(payload: Mapping[str, Any]) -> Literal["pending", "success", "failed"]:
    candidates: list[Any] = [
        payload.get("task_status_type_id"),
        payload.get("status"),
        payload.get("state"),
    ]
    nested = payload.get("task_status")
    if isinstance(nested, Mapping):
        candidates.extend(
            [nested.get("task_status_type_id"), nested.get("name"), nested.get("status")]
        )
    for candidate in candidates:
        if isinstance(candidate, int):
            if candidate == 4:
                return "success"
            if candidate >= 5:
                return "failed"
        text = str(candidate or "").strip().casefold()
        if text in {"success", "succeeded", "complete", "completed"}:
            return "success"
        if text in {"failed", "cancelled", "canceled", "terminated", "error"}:
            return "failed"
    return "pending"


def _expired(expires_at: int | None) -> bool:
    return expires_at is not None and expires_at <= int(time.time()) + 5


def _safe_identity(identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not identity:
        return None
    allowed = ("account_id", "email", "first_name", "last_name", "account_title")
    return {key: identity.get(key) for key in allowed if identity.get(key) is not None}


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}
