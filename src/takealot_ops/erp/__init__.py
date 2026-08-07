"""Unified local ERP service and Vue application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from takealot_ops.erp.web import app, create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    """Load the web app lazily so shared ERP helpers remain independently importable."""
    if name not in __all__:
        raise AttributeError(name)
    from takealot_ops.erp import web

    return getattr(web, name)
