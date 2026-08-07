"""Windows Credential Manager storage for per-store Seller Portal credentials.

The ERP never accepts a Seller Portal password from a browser request.  An operator
provisions it once from the server console; Windows protects it for the account that
runs the ERP process.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, wintypes
from dataclasses import dataclass
from typing import Protocol

from takealot_ops.storage.store_context import normalize_store_code


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_TARGET_PREFIX = "TakealotERP/Portal/"


@dataclass(frozen=True)
class PortalCredential:
    email: str
    password: str


class PortalCredentialStore(Protocol):
    def get(self, store_code: str) -> PortalCredential | None: ...

    def set(self, store_code: str, credential: PortalCredential) -> None: ...

    def delete(self, store_code: str) -> bool: ...


class _CredentialAttributeW(Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", POINTER(wintypes.BYTE)),
    ]


class _CredentialW(Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", POINTER(_CredentialAttributeW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_CredentialPointer = POINTER(_CredentialW)


class WindowsPortalCredentialStore:
    """Store secrets under the current Windows account without project-file plaintext."""

    def get(self, store_code: str) -> PortalCredential | None:
        api = _credential_api()
        pointer = _CredentialPointer()
        if not api.CredReadW(
            _target_name(store_code),
            _CRED_TYPE_GENERIC,
            0,
            byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise OSError(error, "无法读取 Windows 凭据管理器中的 Seller Portal 凭据")
        try:
            value = pointer.contents
            email = str(value.UserName or "").strip()
            password = ctypes.string_at(
                value.CredentialBlob,
                value.CredentialBlobSize,
            ).decode("utf-16-le")
        finally:
            api.CredFree(pointer)
        if not email or not password:
            raise RuntimeError("Windows 凭据管理器中的 Seller Portal 凭据不完整")
        return PortalCredential(email=email, password=password)

    def set(self, store_code: str, credential: PortalCredential) -> None:
        clean_email = credential.email.strip()
        if not clean_email or "@" not in clean_email:
            raise ValueError("Seller Portal 邮箱格式无效")
        if not credential.password:
            raise ValueError("Seller Portal 密码不能为空")
        blob = credential.password.encode("utf-16-le")
        if len(blob) > 2560:
            raise ValueError("Seller Portal 密码超过 Windows 凭据管理器上限")
        buffer = ctypes.create_string_buffer(blob)
        value = _CredentialW()
        value.Flags = 0
        value.Type = _CRED_TYPE_GENERIC
        value.TargetName = _target_name(store_code)
        value.Comment = "Takealot ERP Seller Portal credential"
        value.CredentialBlobSize = len(blob)
        value.CredentialBlob = ctypes.cast(buffer, POINTER(wintypes.BYTE))
        value.Persist = _CRED_PERSIST_LOCAL_MACHINE
        value.AttributeCount = 0
        value.Attributes = None
        value.TargetAlias = None
        value.UserName = clean_email
        api = _credential_api()
        if not api.CredWriteW(byref(value), 0):
            error = ctypes.get_last_error()
            raise OSError(error, "无法写入 Windows 凭据管理器")

    def delete(self, store_code: str) -> bool:
        api = _credential_api()
        if api.CredDeleteW(_target_name(store_code), _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise OSError(error, "无法删除 Windows 凭据管理器中的 Seller Portal 凭据")


def masked_email(credential: PortalCredential | None) -> str | None:
    if credential is None:
        return None
    local, separator, domain = credential.email.partition("@")
    if not separator:
        return "***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


def _target_name(store_code: str) -> str:
    return f"{_TARGET_PREFIX}{normalize_store_code(store_code)}"


def _credential_api() -> ctypes.WinDLL:
    if os.name != "nt":
        raise RuntimeError("Seller Portal 凭据只能保存在 ERP 服务器的 Windows 凭据管理器")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        POINTER(_CredentialPointer),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [POINTER(_CredentialW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [wintypes.LPVOID]
    api.CredFree.restype = None
    return api
