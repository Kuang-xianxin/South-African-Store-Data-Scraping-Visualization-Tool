"""Latest reference exchange-rate lookup for product-cost display."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

import httpx


FRANKFURTER_BASE_URL = "https://api.frankfurter.dev"
FRANKFURTER_CNY_ZAR_PATH = "/v2/rate/CNY/ZAR"
RATE_SOURCE = "Frankfurter 机构参考汇率"
DEFAULT_CACHE_TTL = timedelta(hours=1)
DEFAULT_FAILURE_TTL = timedelta(minutes=5)
MAX_RESPONSE_BYTES = 16 * 1024
CACHE_SCHEMA_VERSION = 1
MAX_CACHE_FUTURE_SKEW = timedelta(minutes=5)
_ZAR_QUANTUM = Decimal("0.01")


class ExchangeRateUnavailable(RuntimeError):
    """Raised when no current or previously cached reference rate is available."""


@dataclass(frozen=True)
class ExchangeRateQuote:
    """One validated CNY-to-ZAR reference quote."""

    rate: Decimal
    rate_date: date
    fetched_at: datetime
    stale: bool = False


class ExchangeRateProvider(Protocol):
    """Minimal rate provider contract used by cost conversion and tests."""

    def latest(self) -> ExchangeRateQuote:
        """Return a validated CNY-to-ZAR quote."""


class CnyZarRateService:
    """Fetch a fixed pair and retain the last success across ERP restarts."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
        failure_ttl: timedelta = DEFAULT_FAILURE_TTL,
        cache_path: Path | None = None,
    ) -> None:
        if cache_ttl <= timedelta(0) or failure_ttl <= timedelta(0):
            raise ValueError("exchange-rate cache durations must be positive")
        self._now = now or (lambda: datetime.now(UTC))
        self._cache_ttl = cache_ttl
        self._failure_ttl = failure_ttl
        self._cache_path = cache_path
        self._client = httpx.Client(
            base_url=FRANKFURTER_BASE_URL,
            headers={"Accept": "application/json", "User-Agent": "takealot-ops/0.1"},
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            transport=transport,
        )
        self._lock = Lock()
        self._quote = self._load_persisted_quote()
        self._refresh_after = (
            self._quote.fetched_at + self._cache_ttl
            if self._quote is not None
            else None
        )
        self._retry_after: datetime | None = None

    def close(self) -> None:
        """Release the shared HTTP connection pool."""
        self._client.close()

    def latest(self) -> ExchangeRateQuote:
        """Return the latest quote, retaining a disclosed stale value on refresh failure."""
        now = _utc_datetime(self._now())
        with self._lock:
            if (
                self._quote is not None
                and self._refresh_after is not None
                and now < self._refresh_after
            ):
                return replace(self._quote, stale=False)
            if self._retry_after is not None and now < self._retry_after:
                if self._quote is not None:
                    return replace(self._quote, stale=True)
                raise ExchangeRateUnavailable("最新人民币兑兰特参考汇率暂不可用")
            try:
                quote = self._fetch(now)
            except ExchangeRateUnavailable:
                self._retry_after = now + self._failure_ttl
                if self._quote is not None:
                    return replace(self._quote, stale=True)
                raise
            self._quote = quote
            self._refresh_after = now + self._cache_ttl
            self._retry_after = None
            self._persist_quote(quote)
            return quote

    def _fetch(self, fetched_at: datetime) -> ExchangeRateQuote:
        try:
            response = self._client.get(FRANKFURTER_CNY_ZAR_PATH)
        except httpx.HTTPError as exc:
            raise ExchangeRateUnavailable("最新人民币兑兰特参考汇率暂不可用") from exc
        if response.status_code != 200:
            raise ExchangeRateUnavailable("最新人民币兑兰特参考汇率暂不可用")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ExchangeRateUnavailable("汇率服务返回内容超出安全上限")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeRateUnavailable("汇率服务返回内容格式异常") from exc
        if not isinstance(payload, Mapping):
            raise ExchangeRateUnavailable("汇率服务返回内容格式异常")
        return _quote_from_payload(payload, fetched_at=fetched_at)

    def _load_persisted_quote(self) -> ExchangeRateQuote | None:
        path = self._cache_path
        if path is None:
            return None
        try:
            with path.open("rb") as stream:
                encoded = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(encoded) > MAX_RESPONSE_BYTES:
                return None
            payload = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        try:
            fetched_at = _cached_datetime(payload.get("fetched_at"))
            quote = _quote_from_payload(payload, fetched_at=fetched_at)
        except (ExchangeRateUnavailable, TypeError, ValueError):
            return None
        if fetched_at > _utc_datetime(self._now()) + MAX_CACHE_FUTURE_SKEW:
            return None
        return quote

    def _persist_quote(self, quote: ExchangeRateQuote) -> None:
        path = self._cache_path
        if path is None:
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "base": "CNY",
            "quote": "ZAR",
            "rate": str(quote.rate),
            "date": quote.rate_date.isoformat(),
            "fetched_at": _utc_datetime(quote.fetched_at).isoformat(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            # The in-memory success remains usable even if the disposable runtime
            # cache cannot be written (for example, a transient filesystem issue).
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _quote_from_payload(
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime,
) -> ExchangeRateQuote:
    if str(payload.get("base") or "").upper() != "CNY":
        raise ExchangeRateUnavailable("汇率服务返回的基础币种不一致")
    if str(payload.get("quote") or "").upper() != "ZAR":
        raise ExchangeRateUnavailable("汇率服务返回的目标币种不一致")
    try:
        rate = Decimal(str(payload.get("rate")))
        rate_date = date.fromisoformat(str(payload.get("date")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRateUnavailable("汇率服务返回的汇率或日期无效") from exc
    if not rate.is_finite() or rate <= 0 or rate > Decimal("100"):
        raise ExchangeRateUnavailable("汇率服务返回的汇率超出合理范围")
    if rate_date > fetched_at.date() + timedelta(days=1):
        raise ExchangeRateUnavailable("汇率服务返回了异常的未来日期")
    return ExchangeRateQuote(
        rate=rate,
        rate_date=rate_date,
        fetched_at=fetched_at,
    )


def _cached_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cached exchange-rate timestamp is missing")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cached exchange-rate timestamp must include a timezone")
    return parsed.astimezone(UTC)


def product_cost_conversion_payload(
    cost_rmb: Any,
    service: ExchangeRateProvider,
) -> dict[str, Any]:
    """Convert one positive RMB unit cost without hiding rate evidence or failures."""
    base_payload: dict[str, Any] = {
        "base_currency": "CNY",
        "quote_currency": "ZAR",
        "cost_rmb": None,
        "cost_zar": None,
        "rate": None,
        "rate_date": None,
        "fetched_at": None,
        "source": RATE_SOURCE,
    }
    try:
        amount = Decimal(str(cost_rmb))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal(0)
    if not amount.is_finite() or amount <= 0:
        return {
            **base_payload,
            "status": "missing_cost",
            "message": "该平台 SKU 尚未关联人民币单件成本。",
        }
    base_payload["cost_rmb"] = float(amount)
    try:
        quote = service.latest()
    except ExchangeRateUnavailable:
        return {
            **base_payload,
            "status": "unavailable",
            "message": "汇率服务暂不可用；人民币成本仍保留，未生成兰特估算值。",
        }
    cost_zar = (amount * quote.rate).quantize(_ZAR_QUANTUM, rounding=ROUND_HALF_UP)
    return {
        **base_payload,
        "cost_zar": float(cost_zar),
        "rate": float(quote.rate),
        "rate_date": quote.rate_date.isoformat(),
        "fetched_at": quote.fetched_at.isoformat(),
        "status": "stale" if quote.stale else "converted",
        "message": (
            "最新汇率请求失败，使用最近一次成功缓存；"
            "最近成功时间见汇率证据，仅供成本估算。"
            if quote.stale
            else "按最新发布的机构参考汇率换算，仅供成本估算，非交易结算价。"
        ),
    }


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
