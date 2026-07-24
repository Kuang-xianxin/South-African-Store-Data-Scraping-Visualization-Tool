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

from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import ErpSession, ErpUser


ROLES = frozenset({"viewer", "operator", "admin"})
OPERATOR_ROLES = frozenset({"operator", "admin"})
SESSION_COOKIE = "takealot_erp_session"
SESSION_LIFETIME = timedelta(hours=12)
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_PASSWORD_PREFIX = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthInputError(ValueError):
    """Raised for a safe-to-display authentication input error."""


class AuthConflictError(ValueError):
    """Raised when an authentication state transition is not allowed."""


@dataclass(frozen=True)
class UserIdentity:
    id: int
    username: str
    display_name: str
    role: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


@dataclass(frozen=True)
class SessionIdentity:
    user: UserIdentity
    csrf_token: str
    expires_at: datetime


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
            if now - record.last_seen_at >= timedelta(minutes=5):
                record.last_seen_at = now
            return SessionIdentity(
                user=_identity(user),
                csrf_token=record.csrf_token,
                expires_at=record.expires_at,
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
            return [_user_payload(user) for user in users]

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str,
    ) -> dict[str, Any]:
        normalized = _normalize_username(username)
        shown_name = _validate_display_name(display_name, normalized)
        validated_role = _validate_role(role)
        password_hash = hash_password(password)
        now = _utc_now()
        try:
            with Session(self._get_engine()) as session, session.begin():
                user = ErpUser(
                    username=normalized,
                    display_name=shown_name,
                    password_hash=password_hash,
                    role=validated_role,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                payload = _user_payload(user)
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
        active: bool | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with Session(self._get_engine()) as session, session.begin():
            user = session.get(ErpUser, user_id)
            if user is None:
                raise AuthInputError("用户不存在")
            new_role = _validate_role(role) if role is not None else user.role
            new_active = active if active is not None else user.active
            if user.active and user.role == "admin" and (
                not new_active or new_role != "admin"
            ):
                other_admins = int(
                    session.scalar(
                        select(func.count(ErpUser.id)).where(
                            ErpUser.active.is_(True),
                            ErpUser.role == "admin",
                            ErpUser.id != user.id,
                        )
                    )
                    or 0
                )
                if other_admins == 0:
                    raise AuthConflictError("不能停用或降级唯一的管理员")

            invalidate_sessions = new_role != user.role or new_active != user.active
            user.role = new_role
            user.active = new_active
            if display_name is not None:
                user.display_name = _validate_display_name(display_name, user.username)
            if password is not None:
                user.password_hash = hash_password(password)
                invalidate_sessions = True
            user.updated_at = now
            if invalidate_sessions:
                session.execute(delete(ErpSession).where(ErpSession.user_id == user.id))
            return _user_payload(user)

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
            user=_identity(user),
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


def _validate_display_name(display_name: str, fallback: str) -> str:
    value = display_name.strip() or fallback
    if len(value) > 100:
        raise AuthInputError("显示名称不能超过 100 个字符")
    return value


def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise AuthInputError("密码至少需要 12 个字符")
    if len(password) > 128:
        raise AuthInputError("密码不能超过 128 个字符")


def _validate_role(role: str) -> str:
    value = role.strip().lower()
    if value not in ROLES:
        raise AuthInputError("角色只能是 viewer、operator 或 admin")
    return value


def _identity(user: ErpUser) -> UserIdentity:
    return UserIdentity(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )


def _user_payload(user: ErpUser) -> dict[str, Any]:
    return {
        **_identity(user).as_dict(),
        "active": user.active,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.utcnow()
