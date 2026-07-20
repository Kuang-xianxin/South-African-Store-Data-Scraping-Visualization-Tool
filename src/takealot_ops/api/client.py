"""Synchronous, read-only Takealot Marketplace API client."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx

from takealot_ops.api.errors import ApiResponseError, AuthenticationError, RateLimitError
from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.settings import Settings


RETRY_DELAYS = (2.0, 5.0, 15.0)


class TakealotClient:
    """Read paginated Marketplace API resources using only HTTP GET requests."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = settings.api_key
        self._sleep = sleep or time.sleep
        self._client = httpx.Client(
            base_url=f"{settings.base_url.rstrip('/')}/",
            headers={"X-API-Key": settings.api_key},
            timeout=settings.request_timeout_seconds,
            transport=transport,
        )

    def iter_items(self, path: str, params: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every item across a continuation-token paginated endpoint."""
        page_params = dict(params)
        while True:
            response = self._request("GET", path, page_params)
            payload = self._json_object(response)
            items = payload.get("items")
            if not isinstance(items, list):
                raise ApiResponseError(self._sanitize("API response must contain an items list"))
            for item in items:
                if not isinstance(item, Mapping):
                    raise ApiResponseError(self._sanitize("API response items must be objects"))
                yield dict(item)

            continuation_token = payload.get("continuation_token")
            if not isinstance(continuation_token, str) or not continuation_token.strip():
                return
            page_params["continuation_token"] = continuation_token

    def list_offers(self) -> Iterator[OfferRecord]:
        """Yield typed current-offer records."""
        captured_at = datetime.now(UTC)
        for item in self.iter_items("/offers", {"limit": 100}):
            try:
                yield OfferRecord.from_api(item, captured_at)
            except (KeyError, TypeError, ValueError) as error:
                raise ApiResponseError(self._sanitize(f"Invalid offer API item: {error}")) from error

    def list_sales(self, start: date, end: date) -> Iterator[SaleRecord]:
        """Yield typed sale records within an inclusive SAST calendar-date range."""
        params = {
            "order_date__gte": start.isoformat(),
            "order_date__lte": end.isoformat(),
            "limit": 100,
        }
        for item in self.iter_items("/sales", params):
            try:
                yield SaleRecord.from_api(item)
            except (KeyError, TypeError, ValueError) as error:
                raise ApiResponseError(self._sanitize(f"Invalid sale API item: {error}")) from error

    def list_returns(self, start: date, end: date) -> Iterator[dict[str, Any]]:
        """Yield return payloads within an inclusive SAST calendar-date range."""
        return self.iter_items(
            "/returns",
            {
                "return_date__gte": start.isoformat(),
                "return_date__lte": end.isoformat(),
                "limit": 100,
            },
        )

    def close(self) -> None:
        """Close the owned HTTP client."""
        self._client.close()

    def _request(self, method: str, path: str, params: Mapping[str, Any]) -> httpx.Response:
        if method != "GET":
            raise ValueError("TakealotClient permits only GET requests")

        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = self._client.request("GET", path.lstrip("/"), params=params)
            except httpx.HTTPError as error:
                raise ApiResponseError(self._sanitize(str(error))) from error

            if response.status_code in {401, 403}:
                raise AuthenticationError(self._error_message(response))
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < len(RETRY_DELAYS):
                    delay = self._retry_delay(response, RETRY_DELAYS[attempt])
                    self._sleep(delay)
                    continue
                if response.status_code == 429:
                    raise RateLimitError(self._error_message(response))
                raise ApiResponseError(self._error_message(response))
            if response.status_code >= 400:
                raise ApiResponseError(self._error_message(response))
            return response

        raise AssertionError("retry loop must return or raise")

    def _json_object(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiResponseError(self._sanitize("API response is not valid JSON")) from error
        if not isinstance(payload, Mapping):
            raise ApiResponseError(self._sanitize("API response must be a JSON object"))
        return payload

    def _retry_delay(self, response: httpx.Response, default_delay: float) -> float:
        if response.status_code != 429:
            return default_delay
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return default_delay
        try:
            parsed_delay = float(retry_after)
        except ValueError:
            return default_delay
        return parsed_delay if math.isfinite(parsed_delay) and parsed_delay >= 0 else default_delay

    def _error_message(self, response: httpx.Response) -> str:
        return self._sanitize(f"Takealot API returned HTTP {response.status_code}: {response.text}")

    def _sanitize(self, message: str) -> str:
        return message.replace(self._api_key, "[REDACTED]")
