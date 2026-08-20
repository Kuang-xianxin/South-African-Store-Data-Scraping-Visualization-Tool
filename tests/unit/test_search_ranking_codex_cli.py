from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import takealot_ops.search_ranking.codex_cli as codex_cli_module
from takealot_ops.search_ranking.codex_cli import (
    CodexAppServerClient,
    CODEX_TERRA_MODEL,
    CodexCliConfigurationError,
    CodexCliProviderError,
    CodexRateLimitWindow,
    CodexWeeklyQuotaGuard,
    _select_weekly_codex_window,
    _strict_output_schema,
)


def _window(*, used_percent: int, resets_at: int = 2_000_000_000) -> CodexRateLimitWindow:
    return CodexRateLimitWindow(
        limit_id="codex",
        bucket="primary",
        used_percent=used_percent,
        window_duration_mins=10_080,
        resets_at=resets_at,
    )


def test_quota_guard_persists_one_ten_point_budget_per_weekly_window(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "quota.json"
    guard = CodexWeeklyQuotaGuard(state_path)

    baseline = guard.observe(_window(used_percent=43))
    middle = guard.observe(_window(used_percent=49))
    exhausted = guard.observe(_window(used_percent=53))

    assert baseline["model"] == CODEX_TERRA_MODEL
    assert baseline["baseline_used_percent"] == 43
    assert baseline["ceiling_used_percent"] == 53
    assert middle["baseline_used_percent"] == 43
    assert middle["consumed_percentage_points"] == 6
    assert middle["remaining_percentage_points"] == 4
    assert exhausted["status"] == "exhausted"
    assert exhausted["remaining_percentage_points"] == 0
    assert json.loads(state_path.read_text(encoding="utf-8"))["ceiling_used_percent"] == 53


def test_quota_guard_opens_a_new_budget_only_after_backend_window_changes(
    tmp_path: Path,
) -> None:
    guard = CodexWeeklyQuotaGuard(tmp_path / "quota.json")
    guard.observe(_window(used_percent=50, resets_at=2_000_000_000))

    refreshed = guard.observe(_window(used_percent=3, resets_at=2_000_604_800))

    assert refreshed["baseline_used_percent"] == 3
    assert refreshed["ceiling_used_percent"] == 13
    assert refreshed["status"] == "active"


def test_quota_guard_fails_closed_if_usage_drops_inside_same_window(tmp_path: Path) -> None:
    guard = CodexWeeklyQuotaGuard(tmp_path / "quota.json")
    guard.observe(_window(used_percent=43))

    with pytest.raises(CodexCliConfigurationError, match="低于已持久化基线"):
        guard.observe(_window(used_percent=42))


def test_rate_limit_selection_requires_one_exact_codex_weekly_bucket() -> None:
    payload = {
        "rateLimits": {"limitId": "codex"},
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 43,
                    "windowDurationMins": 10_080,
                    "resetsAt": 2_000_000_000,
                },
                "secondary": None,
            },
            "codex_bengalfox": {
                "limitId": "codex_bengalfox",
                "primary": {
                    "usedPercent": 1,
                    "windowDurationMins": 10_080,
                    "resetsAt": 2_000_000_001,
                },
            },
        },
    }

    selected = _select_weekly_codex_window(payload)

    assert selected.limit_id == "codex"
    assert selected.used_percent == 43
    assert selected.window_duration_mins == 10_080


def test_rate_limit_selection_rejects_missing_weekly_window() -> None:
    payload = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 1,
                "windowDurationMins": 300,
                "resetsAt": 2_000_000_000,
            },
        }
    }

    with pytest.raises(CodexCliConfigurationError, match="10080"):
        _select_weekly_codex_window(payload)


def test_strict_output_schema_requires_every_property_without_mutating_source() -> None:
    source = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "buyer_job": {"type": "string", "default": ""},
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/Item"},
            },
        },
        "required": ["name"],
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "excluded": {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["phrase"],
            }
        },
    }

    strict = _strict_output_schema(source)

    assert strict["required"] == ["name", "buyer_job", "items"]
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["Item"]["required"] == ["phrase", "excluded"]
    assert strict["$defs"]["Item"]["additionalProperties"] is False
    assert "default" not in strict["properties"]["buyer_job"]
    assert "default" not in strict["$defs"]["Item"]["properties"]["excluded"]
    assert source["required"] == ["name"]
    assert source["properties"]["buyer_job"]["default"] == ""


def test_quota_refresh_retries_only_the_read_before_failing_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryClient(CodexAppServerClient):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "codex",
                project_root=tmp_path,
                quota_guard=CodexWeeklyQuotaGuard(tmp_path / "quota.json"),
                timeout_seconds=1,
            )
            self.calls = 0

        async def _request(self, method: str, params: object) -> dict[str, object]:
            assert method == "account/rateLimits/read"
            assert params == {}
            self.calls += 1
            if self.calls < 3:
                raise CodexCliConfigurationError("temporary quota transport failure")
            return {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 45,
                        "windowDurationMins": 10_080,
                        "resetsAt": 2_000_000_000,
                    },
                }
            }

    monkeypatch.setattr(codex_cli_module, "_RATE_LIMIT_RETRY_DELAY_SECONDS", 0.0)
    client = RetryClient()

    quota = asyncio.run(client._refresh_quota())

    assert client.calls == 3
    assert quota["current_used_percent"] == 45


def test_completed_turn_keeps_usage_when_post_turn_quota_read_fails(
    tmp_path: Path,
) -> None:
    class PostTurnQuotaFailureClient(CodexAppServerClient):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "codex",
                project_root=tmp_path,
                quota_guard=CodexWeeklyQuotaGuard(tmp_path / "quota.json"),
                timeout_seconds=1,
            )
            self.quota_reads = 0

        async def _refresh_quota(self) -> dict[str, object]:
            self.quota_reads += 1
            if self.quota_reads == 1:
                return {"status": "active"}
            raise CodexCliConfigurationError("temporary quota read failure")

        async def _request(self, method: str, params: object) -> dict[str, object]:
            del params
            if method == "thread/start":
                return {"thread": {"id": "thread-1", "modelProvider": "openai"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            raise AssertionError(method)

        async def _collect_turn(self, **_: object) -> tuple[str, dict[str, int]]:
            return (
                '{"name":"mouse"}',
                {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            )

    client = PostTurnQuotaFailureClient()

    with pytest.raises(CodexCliProviderError, match="已完成响应") as exc_info:
        asyncio.run(
            client.run_structured_turn(
                stage="image_title_fusion",
                system_prompt="Return a product",
                user_text="Product context",
                image_path=tmp_path / "image.jpg",
                output_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            )
        )

    assert exc_info.value.usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
    }
