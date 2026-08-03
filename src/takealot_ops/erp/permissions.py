"""Permission templates and account-level permission normalization."""

from __future__ import annotations

import json
from collections.abc import Iterable


ROLES = frozenset({"viewer", "operator", "selection", "admin"})

STORE_VIEW = "store.view"
KEYWORD_TRAFFIC_MANAGE = "keyword_traffic.manage"
COMPETITORS_VIEW = "competitors.view"
COMPETITORS_COLLECT = "competitors.collect"
DAILY_REPORT_VIEW = "daily_report.view"
DAILY_REPORT_MANAGE = "daily_report.manage"
DAILY_REPORT_EXPORT = "daily_report.export"
REPORTS_VIEW = "reports.view"
REPORTS_GENERATE = "reports.generate"
NFT102_MANAGE = "nft102.manage"
REFRESH_RUN = "refresh.run"
USERS_MANAGE = "users.manage"

PERMISSIONS = frozenset(
    {
        STORE_VIEW,
        KEYWORD_TRAFFIC_MANAGE,
        COMPETITORS_VIEW,
        COMPETITORS_COLLECT,
        DAILY_REPORT_VIEW,
        DAILY_REPORT_MANAGE,
        DAILY_REPORT_EXPORT,
        REPORTS_VIEW,
        REPORTS_GENERATE,
        NFT102_MANAGE,
        REFRESH_RUN,
        USERS_MANAGE,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset(
        {
            STORE_VIEW,
            COMPETITORS_VIEW,
            DAILY_REPORT_VIEW,
            REPORTS_VIEW,
        }
    ),
    "operator": frozenset(PERMISSIONS - {USERS_MANAGE}),
    "selection": frozenset(
        {
            COMPETITORS_VIEW,
            COMPETITORS_COLLECT,
            DAILY_REPORT_VIEW,
        }
    ),
    "admin": frozenset(PERMISSIONS),
}

PERMISSION_DEPENDENCIES: dict[str, frozenset[str]] = {
    COMPETITORS_COLLECT: frozenset({COMPETITORS_VIEW}),
    DAILY_REPORT_MANAGE: frozenset({DAILY_REPORT_VIEW}),
    DAILY_REPORT_EXPORT: frozenset({DAILY_REPORT_VIEW}),
    REPORTS_GENERATE: frozenset({REPORTS_VIEW}),
    NFT102_MANAGE: frozenset({REPORTS_VIEW}),
    REFRESH_RUN: frozenset({STORE_VIEW}),
    KEYWORD_TRAFFIC_MANAGE: frozenset({STORE_VIEW}),
}


def validate_role(role: str) -> str:
    """Return a normalized permission-template key."""
    value = role.strip().lower()
    if value not in ROLES:
        raise ValueError("权限模板只能是 viewer、operator、selection 或 admin")
    return value


def normalize_permissions(role: str, values: Iterable[str] | None) -> frozenset[str]:
    """Validate a custom permission set and include required parent permissions."""
    template = validate_role(role)
    if values is None:
        return ROLE_PERMISSIONS[template]
    normalized = {str(value).strip() for value in values}
    unknown = normalized - PERMISSIONS
    if unknown:
        names = "、".join(sorted(unknown))
        raise ValueError(f"包含未知权限：{names}")
    changed = True
    while changed:
        changed = False
        for permission in tuple(normalized):
            dependencies = PERMISSION_DEPENDENCIES.get(permission, frozenset())
            before = len(normalized)
            normalized.update(dependencies)
            changed = changed or len(normalized) != before
    return frozenset(normalized)


def permissions_from_storage(role: str, encoded: str | None) -> frozenset[str]:
    """Resolve legacy template defaults or a persisted account override."""
    template = validate_role(role)
    if encoded is None or not encoded.strip():
        return ROLE_PERMISSIONS[template]
    try:
        values = json.loads(encoded)
        if not isinstance(values, list):
            return frozenset()
        return normalize_permissions(template, values)
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()


def permissions_to_storage(role: str, values: Iterable[str] | None) -> str | None:
    """Persist only account-level differences from the selected template."""
    template = validate_role(role)
    normalized = normalize_permissions(template, values)
    if normalized == ROLE_PERMISSIONS[template]:
        return None
    return json.dumps(sorted(normalized), ensure_ascii=True, separators=(",", ":"))
