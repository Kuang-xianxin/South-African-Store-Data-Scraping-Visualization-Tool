from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from takealot_ops.search_ranking.auto_rerun import (
    SearchRankingAutoRerunError,
    consume_auto_rerun_request,
)


def test_production_web_has_no_auto_rerun_consumer_entrypoint() -> None:
    web_source = (
        Path(__file__).parents[2] / "src" / "takealot_ops" / "erp" / "web.py"
    ).read_text(encoding="utf-8")

    assert "consume_auto_rerun_request" not in web_source
    assert "AUTO_RERUN_FILENAME" not in web_source


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def preview_payload(self, stores: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("preview", kwargs["actor_username"]))
        return {
            "preview": {"snapshot_id": "a" * 64},
            "batch": {"batch_id": "old", "status": "stopped"},
        }

    def restart(self, stores: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("restart", kwargs["snapshot_id"]))
        return {"batch_id": "new-batch", "target_count": 442, "status": "queued"}

    def start(self, stores: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("existing checkpoint should use restart")


def _request() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "pending",
        "actor_username": "kxx",
        "model": "gpt-5.6-terra",
        "weekly_budget_percent": 10,
        "requested_at": "2026-08-19T00:00:00+08:00",
    }


def _users() -> list[dict[str, Any]]:
    return [
        {
            "username": "kxx",
            "display_name": "管理员",
            "active": True,
            "role": "admin",
            "permissions": ["search_ranking.run"],
            "accessible_stores": [
                {
                    "code": "store-01",
                    "display_name": "Store 1",
                    "active": True,
                    "data_connected": True,
                }
            ],
        }
    ]


def test_pending_request_restarts_from_current_snapshot_once(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")
    controller = _FakeController()

    batch = consume_auto_rerun_request(
        request_path,
        controller=controller,
        users=_users(),
    )

    assert batch == {"batch_id": "new-batch", "target_count": 442, "status": "queued"}
    assert controller.calls == [("preview", "kxx"), ("restart", "a" * 64)]
    persisted = json.loads(request_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "started"
    assert persisted["operation"] == "restart"
    assert persisted["batch_id"] == "new-batch"
    assert persisted["target_count"] == 442

    assert (
        consume_auto_rerun_request(
            request_path,
            controller=controller,
            users=_users(),
        )
        is None
    )
    assert len(controller.calls) == 2


def test_invalid_model_fails_closed_and_records_failure(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request = _request()
    request["model"] = "some-other-model"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    controller = _FakeController()

    with pytest.raises(SearchRankingAutoRerunError, match="只允许 gpt-5.6-terra"):
        consume_auto_rerun_request(
            request_path,
            controller=controller,
            users=_users(),
        )

    persisted = json.loads(request_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert "gpt-5.6-terra" in persisted["error"]
    assert controller.calls == []
