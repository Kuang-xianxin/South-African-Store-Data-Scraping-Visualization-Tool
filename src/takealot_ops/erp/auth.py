"""Local ERP authentication, sessions, and role management."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from takealot_ops.erp.permissions import (
    ROLE_PERMISSIONS,
    USERS_MANAGE,
    permissions_from_storage,
    permissions_to_storage,
    validate_role,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import ErpSession, ErpStore, ErpUser, ErpUserStore


SESSION_COOKIE = "takealot_erp_session"
SESSION_LIFETIME = timedelta(days=7)
SESSION_RENEWAL_INTERVAL = timedelta(days=1)
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_STORE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PASSWORD_PREFIX = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthInputError(ValueError):
    """Raised for a safe-to-display authentication input error."""


class AuthConflictError(ValueError):
    """Raised when an authentication state transition is not allowed."""


@dataclass(frozen=True)
class StoreIdentity:
    id: int
    code: str
    display_name: str
    active: bool
    data_connected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "display_name": self.display_name,
            "active": self.active,
            "data_connected": self.data_connected,
        }


@dataclass(frozen=True)
class UserIdentity:
    id: int
    username: str
    display_name: str
    role: str
    permissions: tuple[str, ...]
    permissions_customized: bool
    all_stores: bool
    assigned_store_ids: tuple[int, ...]
    accessible_stores: tuple[StoreIdentity, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "permissions": list(self.permissions),
            "permissions_customized": self.permissions_customized,
            "all_stores": self.all_stores,
            "assigned_store_ids": list(self.assigned_store_ids),
            "accessible_stores": [
                store.as_dict() for store in self.accessible_stores
            ],
        }

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def can_access_connected_store(self) -> bool:
        return any(store.data_connected for store in self.accessible_stores)


@dataclass(frozen=True)
class SessionIdentity:
    user: UserIdentity
    csrf_token: str
    expires_at: datetime
    renewed: bool


@dataclass(frozen=True)
class IssuedSession:
    user: UserIdentity
    token: str
    csrf_token: str
    expires_at: datetime


class AuthManager:
    """Lazily connect to the configured database and manage ERP identities."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._engine: Engine | None = None
        self._engine_lock = Lock()
        self._bootstrap_lock = Lock()

    def close(self) -> None:
        with self._engine_lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None

    def user_count(self) -> int:
        with Session(self._get_engine()) as session:
            return int(session.scalar(select(func.count(ErpUser.id))) or 0)

    def list_stores(self) -> list[dict[str, Any]]:
        with Session(self._get_engine()) as session:
            stores = session.scalars(
                select(ErpStore).order_by(
                    ErpStore.data_connected.desc(),
                    ErpStore.display_name,
                    ErpStore.id,
                )
            ).all()
            return [_store_payload(store) for store in stores]

    def create_store(
        self,
        *,
        code: str,
        display_name: str,
    ) -> dict[str, Any]:
        normalized_code = _normalize_store_code(code)
        shown_name = _validate_store_display_name(display_name)
        now = _utc_now()
        try:
            with Session(self._get_engine()) as session, session.begin():
                store = ErpStore(
                    code=normalized_code,
                    display_name=shown_name,
                    active=True,
                    data_connected=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(store)
                session.flush()
                return _store_payload(store)
        except IntegrityError as exc:
            raise AuthConflictError("该店铺代码已存在") from exc

    def update_store(
        self,
        store_id: int,
        *,
        display_name: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        with Session(self._get_engine()) as session, session.begin():
            store = session.get(ErpStore, store_id)
            if store is None:
                raise AuthInputError("店铺不存在")
            new_active = active if active is not None else store.active
            if store.data_connected and not new_active:
                raise AuthConflictError("当前已接入数据的店铺不能停用")
            if display_name is not None:
                store.display_name = _validate_store_display_name(display_name)
            store.active = new_active
            store.updated_at = _utc_now()
            return _store_payload(store)

    def bootstrap(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> IssuedSession:
        normalized = _normalize_username(username)
        shown_name = _validate_display_name(display_name, normalized)
        password_hash = hash_password(password)
        now = _utc_now()
        with self._bootstrap_lock:
            engine = self._get_engine()
            try:
                with Session(engine) as session, session.begin():
                    if int(session.scalar(select(func.count(ErpUser.id))) or 0) != 0:
                        raise AuthConflictError("系统已完成初始化，请直接登录")
                    user = ErpUser(
                        username=normalized,
                        display_name=shown_name,
                        password_hash=password_hash,
                        role="admin",
                        store_access_all=True,
                        active=True,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(user)
                    session.flush()
                    issued = self._issue_session(session, user, now)
            except IntegrityError as exc:
                raise AuthConflictError("系统已完成初始化，请直接登录") from exc
        return issued

    def login(self, username: str, password: str) -> IssuedSession | None:
        normalized = _normalize_username(username)
        now = _utc_now()
        with Session(self._get_engine()) as session, session.begin():
            user = session.scalar(select(ErpUser).where(ErpUser.username == normalized))
            if (
                user is None
                or not user.active
                or not verify_password(password, user.password_hash)
            ):
                return None
            user.last_login_at = now
            user.updated_at = now
            return self._issue_session(session, user, now)

    def resolve_session(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        now = _utc_now()
        token_hash = _token_hash(token)
        with Session(self._get_engine()) as session, session.begin():
            record = session.get(ErpSession, token_hash)
            if record is None:
                return None
            if record.expires_at <= now:
                session.delete(record)
                return None
            user = session.get(ErpUser, record.user_id)
            if user is None or not user.active:
                session.delete(record)
                return None
            renewed = (
                now - record.last_seen_at >= SESSION_RENEWAL_INTERVAL
                or record.expires_at - now
                < SESSION_LIFETIME - SESSION_RENEWAL_INTERVAL
            )
            if renewed:
                record.last_seen_at = now
                record.expires_at = now + SESSION_LIFETIME
            return SessionIdentity(
                user=_identity(session, user),
                csrf_token=record.csrf_token,
                expires_at=record.expires_at,
                renewed=renewed,
            )

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with Session(self._get_engine()) as session, session.begin():
            session.execute(
                delete(ErpSession).where(ErpSession.token_hash == _token_hash(token))
            )

    def list_users(self) -> list[dict[str, Any]]:
        with Session(self._get_engine()) as session:
            users = session.scalars(select(ErpUser).order_by(ErpUser.id)).all()
            return [_user_payload(session, user) for user in users]

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str,
        permissions: list[str] | None = None,
        all_stores: bool | None = None,
        store_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_username(username)
        shown_name = _validate_display_name(display_name, normalized)
        validated_role = _validate_role(role)
        permissions_json = _validated_permissions_json(
            validated_role,
            permissions,
        )
        password_hash = hash_password(password)
        store_access_all = (
            True
            if all_stores is None and store_ids is None
            else bool(all_stores)
        )
        now = _utc_now()
        try:
            with Session(self._get_engine()) as session, session.begin():
                validated_store_ids = _validated_store_ids(session, store_ids or [])
                user = ErpUser(
                    username=normalized,
                    display_name=shown_name,
                    password_hash=password_hash,
                    role=validated_role,
                    permissions_json=permissions_json,
                    store_access_all=store_access_all,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                _replace_user_stores(session, user.id, validated_store_ids)
                payload = _user_payload(session, user)
        except IntegrityError as exc:
            raise AuthConflictError("该用户名已存在") from exc
        return payload

    def update_user(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        password: str | None = None,
        role: str | None = None,
        permissions: list[str] | None = None,
        permissions_provided: bool = False,
        all_stores: bool | None = None,
        store_ids: list[int] | None = None,
        store_ids_provided: bool = False,
        active: bool | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with Session(self._get_engine()) as session, session.begin():
            user = session.get(ErpUser, user_id)
            if user is None:
                raise AuthInputError("用户不存在")
            new_role = _validate_role(role) if role is not None else user.role
            new_active = active if active is not None else user.active
            role_changed = new_role != user.role
            if permissions_provided:
                new_permissions_json = _validated_permissions_json(
                    new_role,
                    permissions,
                )
            elif role_changed:
                new_permissions_json = None
            else:
                new_permissions_json = user.permissions_json
            current_store_ids = _assigned_store_ids(session, user.id)
            new_store_ids = (
                _validated_store_ids(session, store_ids or [])
                if store_ids_provided
                else current_store_ids
            )
            new_store_access_all = (
                all_stores if all_stores is not None else user.store_access_all
            )
            current_permissions = _permissions(user)
            new_permissions = permissions_from_storage(
                new_role,
                new_permissions_json,
            )
            if (
                user.active
                and USERS_MANAGE in current_permissions
                and (not new_active or USERS_MANAGE not in new_permissions)
            ):
                other_managers = [
                    other
                    for other in session.scalars(
                        select(ErpUser).where(
                            ErpUser.active.is_(True),
                            ErpUser.id != user.id,
                        )
                    ).all()
                    if USERS_MANAGE in _permissions(other)
                ]
                if not other_managers:
                    raise AuthConflictError("必须保留至少一个可管理用户权限的启用账号")

            invalidate_sessions = (
                role_changed
                or new_active != user.active
                or new_permissions_json != user.permissions_json
                or new_store_access_all != user.store_access_all
                or new_store_ids != current_store_ids
            )
            user.role = new_role
            user.permissions_json = new_permissions_json
            user.store_access_all = new_store_access_all
            user.active = new_active
            if new_store_ids != current_store_ids:
                _replace_user_stores(session, user.id, new_store_ids)
            if display_name is not None:
                user.display_name = _validate_display_name(display_name, user.username)
            if password is not None:
                user.password_hash = hash_password(password)
                invalidate_sessions = True
            user.updated_at = now
            if invalidate_sessions:
                session.execute(delete(ErpSession).where(ErpSession.user_id == user.id))
            return _user_payload(session, user)

    def _get_engine(self) -> Engine:
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is None:
                settings = DashboardSettings.from_env(self.project_root)
                engine = create_engine_for_settings(settings)
                try:
                    create_schema(engine)
                except BaseException:
                    engine.dispose()
                    raise
                self._engine = engine
        return self._engine

    @staticmethod
    def _issue_session(
        session: Session,
        user: ErpUser,
        now: datetime,
    ) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = now + SESSION_LIFETIME
        session.execute(delete(ErpSession).where(ErpSession.expires_at <= now))
        session.add(
            ErpSession(
                token_hash=_token_hash(token),
                user_id=user.id,
                csrf_token=csrf_token,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
            )
        )
        return IssuedSession(
            user=_identity(session, user),
            token=token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )


def hash_password(password: str) -> str:
    _validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            _PASSWORD_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if prefix != _PASSWORD_PREFIX:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not _USERNAME_RE.fullmatch(normalized):
        raise AuthInputError("用户名需为 3-64 位小写字母、数字、点、下划线或连字符")
    return normalized


def _normalize_store_code(code: str) -> str:
    normalized = code.strip().lower()
    if not _STORE_CODE_RE.fullmatch(normalized):
        raise AuthInputError("店铺代码需为 1-64 位小写字母、数字、下划线或连字符")
    return normalized


def _validate_store_display_name(display_name: str) -> str:
    value = display_name.strip()
    if not value:
        raise AuthInputError("店铺名称不能为空")
    if len(value) > 100:
        raise AuthInputError("店铺名称不能超过 100 个字符")
    return value


def _validate_display_name(display_name: str, fallback: str) -> str:
    value = display_name.strip() or fallback
    if len(value) > 100:
        raise AuthInputError("显示名称不能超过 100 个字符")
    return value


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise AuthInputError("密码至少需要 8 个字符")
    if len(password) > 128:
        raise AuthInputError("密码不能超过 128 个字符")


def _validate_role(role: str) -> str:
    try:
        return validate_role(role)
    except ValueError as exc:
        raise AuthInputError(str(exc)) from exc


def _validated_permissions_json(
    role: str,
    permissions: list[str] | None,
) -> str | None:
    try:
        return permissions_to_storage(role, permissions)
    except ValueError as exc:
        raise AuthInputError(str(exc)) from exc


def _permissions(user: ErpUser) -> frozenset[str]:
    return permissions_from_storage(user.role, user.permissions_json)


def _assigned_store_ids(session: Session, user_id: int) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(ErpUserStore.store_id)
            .where(ErpUserStore.user_id == user_id)
            .order_by(ErpUserStore.store_id)
        ).all()
    )


def _validated_store_ids(
    session: Session,
    values: list[int],
) -> tuple[int, ...]:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        raise AuthInputError("店铺编号必须是正整数")
    normalized = tuple(sorted(set(values)))
    if not normalized:
        return normalized
    existing = set(
        session.scalars(
            select(ErpStore.id).where(ErpStore.id.in_(normalized))
        ).all()
    )
    unknown = sorted(set(normalized) - existing)
    if unknown:
        names = "、".join(str(value) for value in unknown)
        raise AuthInputError(f"店铺不存在：{names}")
    return normalized


def _replace_user_stores(
    session: Session,
    user_id: int,
    store_ids: tuple[int, ...],
) -> None:
    session.execute(
        delete(ErpUserStore).where(ErpUserStore.user_id == user_id)
    )
    session.add_all(
        [
            ErpUserStore(user_id=user_id, store_id=store_id)
            for store_id in store_ids
        ]
    )
    session.flush()


def _store_identity(store: ErpStore) -> StoreIdentity:
    return StoreIdentity(
        id=store.id,
        code=store.code,
        display_name=store.display_name,
        active=store.active,
        data_connected=store.data_connected,
    )


def _accessible_stores(
    session: Session,
    user: ErpUser,
    assigned_store_ids: tuple[int, ...],
) -> tuple[StoreIdentity, ...]:
    statement = select(ErpStore).where(ErpStore.active.is_(True))
    if not user.store_access_all:
        if not assigned_store_ids:
            return ()
        statement = statement.where(ErpStore.id.in_(assigned_store_ids))
    stores = session.scalars(
        statement.order_by(
            ErpStore.data_connected.desc(),
            ErpStore.display_name,
            ErpStore.id,
        )
    ).all()
    return tuple(_store_identity(store) for store in stores)


def _identity(session: Session, user: ErpUser) -> UserIdentity:
    permissions = _permissions(user)
    assigned_store_ids = _assigned_store_ids(session, user.id)
    return UserIdentity(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        permissions=tuple(sorted(permissions)),
        permissions_customized=permissions != ROLE_PERMISSIONS[user.role],
        all_stores=user.store_access_all,
        assigned_store_ids=assigned_store_ids,
        accessible_stores=_accessible_stores(
            session,
            user,
            assigned_store_ids,
        ),
    )


def _store_payload(store: ErpStore) -> dict[str, Any]:
    return {
        **_store_identity(store).as_dict(),
        "created_at": store.created_at.isoformat(),
        "updated_at": store.updated_at.isoformat(),
    }


def _user_payload(session: Session, user: ErpUser) -> dict[str, Any]:
    return {
        **_identity(session, user).as_dict(),
        "active": user.active,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.utcnow()
