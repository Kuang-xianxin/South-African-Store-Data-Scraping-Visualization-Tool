"""Strict Codex App Server transport and weekly quota guard for search ranking."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CODEX_CLI_PACKAGE_VERSION = "0.147.0"
CODEX_TERRA_MODEL = "gpt-5.6-terra"
CODEX_RATE_LIMIT_ID = "codex"
CODEX_WEEKLY_WINDOW_MINUTES = 10_080
CODEX_WEEKLY_BUDGET_PERCENT = 10
CODEX_QUOTA_STATE_SCHEMA_VERSION = 1
_PROTOCOL_LINE_LIMIT = 1_048_576
_MAX_PROTOCOL_MESSAGES_PER_REQUEST = 10_000
_RATE_LIMIT_READ_ATTEMPTS = 3
_RATE_LIMIT_RETRY_DELAY_SECONDS = 1.5


class CodexCliError(RuntimeError):
    """Base error for the local Codex CLI integration."""


class CodexCliConfigurationError(CodexCliError):
    """The pinned CLI, login, model, or quota contract is unavailable."""


class CodexCliProviderError(CodexCliError):
    """Codex failed to return one usable structured model response."""

    def __init__(
        self,
        message: str,
        *,
        usage: Mapping[str, Any] | None = None,
        quota: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = _normalized_usage(usage)
        self.quota = dict(quota or {})


class CodexCliQuotaExceededError(CodexCliProviderError):
    """The persisted ten-percentage-point weekly budget is exhausted."""


@dataclass(frozen=True)
class CodexRateLimitWindow:
    limit_id: str
    bucket: str
    used_percent: int
    window_duration_mins: int
    resets_at: int


@dataclass(frozen=True)
class CodexStructuredTurnResult:
    payload: dict[str, Any]
    turn_id: str
    usage: dict[str, int]
    quota: dict[str, Any]


def resolve_codex_cli_executable(project_root: Path) -> Path | None:
    """Resolve only an explicit binary or the repository-pinned npm runtime."""

    explicit = os.environ.get("TAKEALOT_SEARCH_CODEX_CLI_PATH", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            raise CodexCliConfigurationError(
                "TAKEALOT_SEARCH_CODEX_CLI_PATH 必须是 Codex CLI 可执行文件的绝对路径"
            )
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise CodexCliConfigurationError("TAKEALOT_SEARCH_CODEX_CLI_PATH 指向的文件不存在")
        return resolved

    package_root = (
        project_root.resolve()
        / "tools"
        / "codex-cli"
        / "node_modules"
        / "@openai"
    )
    if not package_root.is_dir():
        return None
    executable_name = "codex.exe" if os.name == "nt" else "codex"
    candidates = sorted(
        path.resolve()
        for path in package_root.glob(f"codex-*/vendor/*/bin/{executable_name}")
        if path.is_file()
    )
    return candidates[0] if len(candidates) == 1 else None


class CodexWeeklyQuotaGuard:
    """Persist at most ten added percentage points in each exact weekly window."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def status(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        return self._load_state()

    def observe(self, window: CodexRateLimitWindow) -> dict[str, Any]:
        state = self._load_state() if self.state_path.exists() else None
        if state is None or not self._same_window(state, window):
            baseline = window.used_percent
            ceiling = min(100, baseline + CODEX_WEEKLY_BUDGET_PERCENT)
            started_at = _iso_now()
        else:
            baseline = _bounded_percent(state.get("baseline_used_percent"), "额度基线")
            ceiling = _bounded_percent(state.get("ceiling_used_percent"), "额度上限")
            if ceiling != min(100, baseline + CODEX_WEEKLY_BUDGET_PERCENT):
                raise CodexCliConfigurationError("Codex 周额度状态的10%上限校验失败")
            if window.used_percent < baseline:
                raise CodexCliConfigurationError(
                    "Codex 周额度使用率低于已持久化基线，为防止绕过上限已停止调用"
                )
            started_at = str(state.get("started_at") or _iso_now())

        consumed = max(0, window.used_percent - baseline)
        remaining = max(0, ceiling - window.used_percent)
        reached = window.used_percent >= ceiling
        payload = {
            "schema_version": CODEX_QUOTA_STATE_SCHEMA_VERSION,
            "model": CODEX_TERRA_MODEL,
            "limit_id": window.limit_id,
            "bucket": window.bucket,
            "window_duration_mins": window.window_duration_mins,
            "resets_at": window.resets_at,
            "resets_at_iso": datetime.fromtimestamp(window.resets_at, tz=UTC).isoformat(),
            "baseline_used_percent": baseline,
            "ceiling_used_percent": ceiling,
            "current_used_percent": window.used_percent,
            "consumed_percentage_points": consumed,
            "remaining_percentage_points": remaining,
            "budget_percent": CODEX_WEEKLY_BUDGET_PERCENT,
            "status": "exhausted" if reached else "active",
            "started_at": started_at,
            "updated_at": _iso_now(),
            "interpretation": "additional_percentage_points_in_same_weekly_window",
        }
        self._persist_state(payload)
        return payload

    @staticmethod
    def _same_window(
        state: Mapping[str, Any],
        window: CodexRateLimitWindow,
    ) -> bool:
        return bool(
            state.get("schema_version") == CODEX_QUOTA_STATE_SCHEMA_VERSION
            and state.get("model") == CODEX_TERRA_MODEL
            and state.get("limit_id") == window.limit_id
            and state.get("bucket") == window.bucket
            and state.get("window_duration_mins") == window.window_duration_mins
            and state.get("resets_at") == window.resets_at
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexCliConfigurationError(
                "Codex 周额度状态无法安全读取，已失败关闭"
            ) from exc
        if not isinstance(payload, dict):
            raise CodexCliConfigurationError("Codex 周额度状态格式无效，已失败关闭")
        return payload

    def _persist_state(self, payload: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


class CodexAppServerClient:
    """One short-lived stdio App Server session with no model fallback or tools."""

    def __init__(
        self,
        executable: Path,
        *,
        project_root: Path,
        quota_guard: CodexWeeklyQuotaGuard,
        timeout_seconds: float,
    ) -> None:
        self.executable = executable.resolve()
        self.project_root = project_root.resolve()
        self.quota_guard = quota_guard
        self.timeout_seconds = timeout_seconds
        self.runtime_cwd = Path(tempfile.gettempdir()) / "takealot-search-ranking-codex"
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._queued_messages: list[dict[str, Any]] = []
        self._next_request_id = 1

    async def __aenter__(self) -> CodexAppServerClient:
        self.runtime_cwd.mkdir(parents=True, exist_ok=True)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "app-server",
                "--stdio",
                "--strict-config",
                "-c",
                f'model="{CODEX_TERRA_MODEL}"',
                cwd=str(self.runtime_cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                limit=_PROTOCOL_LINE_LIMIT,
            )
        except (OSError, ValueError) as exc:
            raise CodexCliConfigurationError("Codex CLI App Server 无法启动") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            initialized = await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "takealot_erp",
                        "title": "Takealot ERP Search Ranking",
                        "version": "1.0.0",
                    },
                    "capabilities": {
                        "optOutNotificationMethods": [
                            "item/agentMessage/delta",
                            "item/reasoning/summaryTextDelta",
                            "item/reasoning/textDelta",
                        ]
                    },
                },
            )
            user_agent = str(initialized.get("userAgent") or "")
            if f"/{CODEX_CLI_PACKAGE_VERSION}" not in user_agent:
                raise CodexCliConfigurationError(
                    "Codex CLI 实际版本与项目锁定版本不一致，已失败关闭"
                )
            await self._notify("initialized", {})
            await self._assert_terra_available()
        except BaseException:
            await self._close_process()
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._close_process()

    async def _close_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        self._process = None

    async def preflight_quota(self) -> dict[str, Any]:
        quota = await self._refresh_quota()
        if quota.get("status") == "exhausted":
            raise CodexCliQuotaExceededError(
                _quota_exhausted_message(quota),
                quota=quota,
            )
        return quota

    async def run_structured_turn(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_text: str,
        image_path: Path,
        output_schema: Mapping[str, Any],
    ) -> CodexStructuredTurnResult:
        await self.preflight_quota()
        thread_response = await self._request(
            "thread/start",
            {
                "model": CODEX_TERRA_MODEL,
                "cwd": str(self.runtime_cwd),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "serviceName": "takealot_search_ranking",
                "baseInstructions": (
                    system_prompt.strip()
                    + "\nReturn exactly one JSON object matching the supplied output schema."
                ),
                "developerInstructions": (
                    "This is a deterministic product-analysis request. Do not call shell, web, "
                    "MCP, apps, skills, file tools, image-view tools, or sub-agents. Inspect only "
                    "the image supplied directly in the user input. Do not modify any state."
                ),
            },
        )
        thread = thread_response.get("thread")
        if not isinstance(thread, Mapping) or not str(thread.get("id") or ""):
            raise CodexCliProviderError("Codex CLI 没有创建可用的临时分析会话")
        if thread.get("modelProvider") not in {None, "openai"}:
            raise CodexCliConfigurationError("Codex CLI 会话模型提供方不是 OpenAI")
        thread_id = str(thread["id"])
        turn_response = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {"type": "text", "text": user_text},
                    {"type": "localImage", "path": str(image_path.resolve()), "detail": "auto"},
                ],
                "cwd": str(self.runtime_cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "model": CODEX_TERRA_MODEL,
                "effort": "medium",
                "summary": "none",
                "outputSchema": _strict_output_schema(output_schema),
            },
        )
        turn = turn_response.get("turn")
        if not isinstance(turn, Mapping) or not str(turn.get("id") or ""):
            raise CodexCliProviderError(f"Codex CLI {stage} 阶段没有启动可用回合")
        turn_id = str(turn["id"])
        text, usage = await self._collect_turn(thread_id=thread_id, turn_id=turn_id, stage=stage)
        try:
            quota = await self._refresh_quota()
        except CodexCliConfigurationError as exc:
            raise CodexCliProviderError(
                f"Codex CLI {stage} 阶段已完成响应，但周额度复核失败：{exc}",
                usage=usage,
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CodexCliProviderError(
                f"Codex CLI {stage} 阶段没有返回合格 JSON",
                usage=usage,
                quota=quota,
            ) from exc
        if not isinstance(payload, dict):
            raise CodexCliProviderError(
                f"Codex CLI {stage} 阶段返回值不是 JSON 对象",
                usage=usage,
                quota=quota,
            )
        return CodexStructuredTurnResult(
            payload=payload,
            turn_id=turn_id,
            usage=usage,
            quota=quota,
        )

    async def _assert_terra_available(self) -> None:
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            response = await self._request("model/list", params)
            rows = response.get("data")
            if not isinstance(rows, list):
                raise CodexCliConfigurationError("Codex CLI 没有返回可用模型列表")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if row.get("id") != CODEX_TERRA_MODEL and row.get("model") != CODEX_TERRA_MODEL:
                    continue
                modalities = row.get("inputModalities")
                if not isinstance(modalities, list) or "image" not in modalities:
                    raise CodexCliConfigurationError(
                        f"Codex CLI 中的 {CODEX_TERRA_MODEL} 当前不支持图片输入"
                    )
                return
            cursor = str(response.get("nextCursor") or "") or None
            if cursor is None:
                break
        raise CodexCliConfigurationError(
            f"Codex CLI 当前账号不可用指定模型 {CODEX_TERRA_MODEL}"
        )

    async def _refresh_quota(self) -> dict[str, Any]:
        response: dict[str, Any] | None = None
        last_error: CodexCliConfigurationError | None = None
        for attempt in range(_RATE_LIMIT_READ_ATTEMPTS):
            try:
                response = await self._request("account/rateLimits/read", {})
                break
            except CodexCliConfigurationError as exc:
                last_error = exc
                if attempt + 1 >= _RATE_LIMIT_READ_ATTEMPTS:
                    raise
                await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY_SECONDS)
        if response is None:
            raise last_error or CodexCliConfigurationError(
                "Codex CLI 没有返回可验证的周额度"
            )
        window = _select_weekly_codex_window(response)
        return self.quota_guard.observe(window)

    async def _collect_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        stage: str,
    ) -> tuple[str, dict[str, int]]:
        final_messages: list[str] = []
        usage = _normalized_usage(None)
        forbidden_item: str | None = None
        rerouted: tuple[str, str] | None = None
        for _ in range(_MAX_PROTOCOL_MESSAGES_PER_REQUEST):
            message = await self._next_message()
            method = str(message.get("method") or "")
            params = message.get("params")
            normalized = params if isinstance(params, Mapping) else {}
            if method == "model/rerouted" and normalized.get("turnId") == turn_id:
                rerouted = (
                    str(normalized.get("fromModel") or "unknown"),
                    str(normalized.get("toModel") or "unknown"),
                )
                await self._send_interrupt(thread_id, turn_id)
                continue
            if method == "thread/tokenUsage/updated" and normalized.get("turnId") == turn_id:
                token_usage = normalized.get("tokenUsage")
                last = token_usage.get("last") if isinstance(token_usage, Mapping) else None
                usage = _normalized_usage(last)
                continue
            if method == "item/completed" and normalized.get("turnId") == turn_id:
                item = normalized.get("item")
                if not isinstance(item, Mapping):
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "agentMessage":
                    phase = item.get("phase")
                    if phase in {None, "final_answer"} and str(item.get("text") or "").strip():
                        final_messages.append(str(item["text"]).strip())
                elif item_type not in {"userMessage", "reasoning"}:
                    forbidden_item = item_type or "unknown"
                    await self._send_interrupt(thread_id, turn_id)
                continue
            if method == "turn/completed" and normalized.get("threadId") == thread_id:
                completed = normalized.get("turn")
                if not isinstance(completed, Mapping) or completed.get("id") != turn_id:
                    continue
                status = str(completed.get("status") or "")
                if rerouted is not None:
                    raise CodexCliConfigurationError(
                        f"Codex 将模型从 {rerouted[0]} 改道为 {rerouted[1]}；根据 Terra-only 规则已拒绝结果"
                    )
                if forbidden_item is not None:
                    raise CodexCliProviderError(
                        f"Codex CLI {stage} 阶段尝试使用禁止工具 {forbidden_item}，已中断"
                    )
                if status != "completed":
                    error = completed.get("error")
                    error_message = (
                        str(error.get("message") or "") if isinstance(error, Mapping) else ""
                    )
                    raise CodexCliProviderError(
                        f"Codex CLI {stage} 阶段结束状态为 {status or 'unknown'}"
                        + (f"：{error_message}" if error_message else ""),
                        usage=usage,
                    )
                if not final_messages:
                    raise CodexCliProviderError(
                        f"Codex CLI {stage} 阶段未返回最终结构化消息",
                        usage=usage,
                    )
                return final_messages[-1], usage
        raise CodexCliProviderError(f"Codex CLI {stage} 阶段消息超过安全上限")

    async def _send_interrupt(self, thread_id: str, turn_id: str) -> None:
        request_id = self._next_request_id
        self._next_request_id += 1
        await self._send(
            {
                "method": "turn/interrupt",
                "id": request_id,
                "params": {"threadId": thread_id, "turnId": turn_id},
            }
        )

    async def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        await self._send({"method": method, "id": request_id, "params": dict(params)})
        for index, queued in enumerate(self._queued_messages):
            if queued.get("id") == request_id:
                message = self._queued_messages.pop(index)
                return self._response_result(method, message)
        for _ in range(_MAX_PROTOCOL_MESSAGES_PER_REQUEST):
            message = await self._read_message()
            if message.get("id") != request_id:
                self._queued_messages.append(message)
                continue
            return self._response_result(method, message)
        raise CodexCliProviderError(f"Codex CLI {method} 响应超过安全上限")

    @staticmethod
    def _response_result(method: str, message: Mapping[str, Any]) -> dict[str, Any]:
        error = message.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or "Codex App Server 返回未知错误")
            if method in {"initialize", "model/list", "account/rateLimits/read"}:
                raise CodexCliConfigurationError(f"Codex CLI {method} 失败：{detail}")
            raise CodexCliProviderError(f"Codex CLI {method} 失败：{detail}")
        result = message.get("result")
        return dict(result) if isinstance(result, Mapping) else {}

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send({"method": method, "params": dict(params)})

    async def _send(self, payload: Mapping[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None or process.returncode is not None:
            raise CodexCliProviderError("Codex CLI App Server 连接已关闭")
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n"
        process.stdin.write(encoded.encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexCliProviderError(self._process_failure_message()) from exc

    async def _next_message(self) -> dict[str, Any]:
        if self._queued_messages:
            return self._queued_messages.pop(0)
        return await self._read_message()

    async def _read_message(self) -> dict[str, Any]:
        process = self._require_process()
        if process.stdout is None:
            raise CodexCliProviderError("Codex CLI App Server 没有可读输出")
        try:
            raw = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise CodexCliProviderError("Codex CLI App Server 响应超时") from exc
        if not raw:
            raise CodexCliProviderError(self._process_failure_message())
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexCliProviderError("Codex CLI App Server 返回了无效 JSONL") from exc
        if not isinstance(message, dict):
            raise CodexCliProviderError("Codex CLI App Server 返回了无效协议消息")
        return message

    async def _drain_stderr(self) -> None:
        process = self._require_process()
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_lines.append(text)
                self._stderr_lines = self._stderr_lines[-20:]

    def _process_failure_message(self) -> str:
        suffix = f"：{self._stderr_lines[-1]}" if self._stderr_lines else ""
        return "Codex CLI App Server 意外退出" + suffix

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise CodexCliProviderError("Codex CLI App Server 尚未启动")
        return self._process


def _strict_output_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Translate Pydantic JSON Schema into the strict Responses API subset.

    Pydantic omits defaulted compatibility fields from ``required``. Codex
    structured output is strict: every declared object property must be listed
    as required, even when the local validator supplies a historical default.
    The provider also does not need those local defaults in its wire schema.
    """

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            normalized = {
                str(key): normalize(item)
                for key, item in value.items()
                if key != "default"
            }
            properties = normalized.get("properties")
            if isinstance(properties, dict):
                normalized["required"] = list(properties)
                normalized["additionalProperties"] = False
            return normalized
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    normalized = normalize(schema)
    if not isinstance(normalized, dict):
        raise CodexCliConfigurationError("Codex 结构化输出 Schema 不是 JSON 对象")
    return normalized


def _select_weekly_codex_window(payload: Mapping[str, Any]) -> CodexRateLimitWindow:
    by_id = payload.get("rateLimitsByLimitId")
    snapshot: Mapping[str, Any] | None = None
    if isinstance(by_id, Mapping):
        candidate = by_id.get(CODEX_RATE_LIMIT_ID)
        if isinstance(candidate, Mapping):
            snapshot = candidate
    if snapshot is None:
        candidate = payload.get("rateLimits")
        if isinstance(candidate, Mapping) and candidate.get("limitId") == CODEX_RATE_LIMIT_ID:
            snapshot = candidate
    if snapshot is None:
        raise CodexCliConfigurationError("Codex CLI 没有返回 Terra 所在的 codex 额度桶")

    matches: list[CodexRateLimitWindow] = []
    for bucket in ("primary", "secondary"):
        raw_window = snapshot.get(bucket)
        if not isinstance(raw_window, Mapping):
            continue
        duration = raw_window.get("windowDurationMins")
        if duration != CODEX_WEEKLY_WINDOW_MINUTES:
            continue
        resets_at = raw_window.get("resetsAt")
        if not isinstance(resets_at, int) or resets_at <= 0:
            raise CodexCliConfigurationError("Codex 七天额度窗口没有可验证的重置时间")
        matches.append(
            CodexRateLimitWindow(
                limit_id=CODEX_RATE_LIMIT_ID,
                bucket=bucket,
                used_percent=_bounded_percent(raw_window.get("usedPercent"), "Codex 周使用率"),
                window_duration_mins=duration,
                resets_at=resets_at,
            )
        )
    if len(matches) != 1:
        raise CodexCliConfigurationError(
            "Codex CLI 未返回唯一的 10080 分钟额度窗口，已失败关闭"
        )
    return matches[0]


def _normalized_usage(value: Mapping[str, Any] | None) -> dict[str, int]:
    raw = value if isinstance(value, Mapping) else {}
    input_tokens = _nonnegative_int(raw.get("inputTokens", raw.get("input_tokens")))
    output_tokens = _nonnegative_int(raw.get("outputTokens", raw.get("output_tokens")))
    total_tokens = _nonnegative_int(raw.get("totalTokens", raw.get("total_tokens")))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or input_tokens + output_tokens,
    }


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_percent(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise CodexCliConfigurationError(f"{label}不是有效整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise CodexCliConfigurationError(f"{label}不是有效整数") from exc
    if not 0 <= normalized <= 100:
        raise CodexCliConfigurationError(f"{label}必须介于 0 到 100")
    return normalized


def _quota_exhausted_message(quota: Mapping[str, Any]) -> str:
    return (
        "Codex Terra 当前七天额度已触发本系统 10% 硬上限："
        f"基线 {quota.get('baseline_used_percent')}%，"
        f"当前 {quota.get('current_used_percent')}%，"
        f"上限 {quota.get('ceiling_used_percent')}%"
    )


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "CODEX_CLI_PACKAGE_VERSION",
    "CODEX_RATE_LIMIT_ID",
    "CODEX_TERRA_MODEL",
    "CODEX_WEEKLY_BUDGET_PERCENT",
    "CODEX_WEEKLY_WINDOW_MINUTES",
    "CodexAppServerClient",
    "CodexCliConfigurationError",
    "CodexCliProviderError",
    "CodexCliQuotaExceededError",
    "CodexStructuredTurnResult",
    "CodexWeeklyQuotaGuard",
    "resolve_codex_cli_executable",
]
