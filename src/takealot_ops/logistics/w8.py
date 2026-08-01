"""Strictly read-only client for the Long Reach W8 customer API."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from takealot_ops.settings import W8Settings


READ_ONLY_PATHS = frozenset(
    {
        "/commonApi/inner/getHouseList",
        "/commonApi/inner/getChannelList",
        "/commonApi/dropshipping/queryProducts",
        "/commonApi/dropshipping/queryStocks",
        "/commonApi/dropshipping/queryInBoundOrders",
        "/commonApi/dropshipping/queryOutboundOrders",
        "/commonApi/dropshipping/queryReBoundOrders",
    }
)
RETRY_DELAYS = (1.0, 2.0)


class W8ApiError(RuntimeError):
    """Raised when W8 transport, authentication, or payload validation fails."""


class W8Client:
    """Call only the documented W8 query endpoints used by the logistics overview."""

    def __init__(
        self,
        settings: W8Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        *,
        trust_env: bool = True,
    ) -> None:
        if not settings.configured:
            raise W8ApiError("长睿 W8 授权码尚未配置")
        self._token = settings.token
        self._sleep = sleep or time.sleep
        self._client = httpx.Client(
            base_url=f"{settings.base_url.rstrip('/')}/",
            headers={"token": settings.token, "Content-Type": "application/json"},
            timeout=settings.request_timeout_seconds,
            transport=transport,
            trust_env=trust_env,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def warehouses(self) -> list[dict[str, Any]]:
        return self._list_data("/commonApi/inner/getHouseList", {})

    def channels(self, house_code: str) -> list[dict[str, Any]]:
        return self._list_data(
            "/commonApi/inner/getChannelList",
            {"houseCode": house_code, "busType": 0, "type": "1"},
        )

    def products(self, request_id: str) -> Mapping[str, Any]:
        return self._page_data(
            "/commonApi/dropshipping/queryProducts",
            {
                "param": {
                    "query": {"ids": [], "keywordList": []},
                    "size": 1,
                    "current": 1,
                },
                "uuid": request_id,
            },
        )

    def stocks(self, house_id: int, house_code: str, request_id: str) -> Mapping[str, Any]:
        return self._page_data(
            "/commonApi/dropshipping/queryStocks",
            {
                "param": {
                    "size": 1000,
                    "current": 1,
                    "query": {
                        "skuIds": [],
                        "houseId": house_id,
                        "houseCode": house_code,
                    },
                    "keywordList": [],
                },
                "uuid": request_id,
            },
        )

    def inbound_orders(
        self,
        house_id: int,
        house_code: str,
        date_end: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        return self._page_data(
            "/commonApi/dropshipping/queryInBoundOrders",
            {
                "param": {
                    "query": {
                        "dateBeg": "2020-01-01 00:00:00",
                        "dateEnd": date_end,
                        "houseId": house_id,
                        "houseCode": house_code,
                        "ids": [],
                        "keywordList": [],
                    },
                    "size": 1000,
                    "current": 1,
                },
                "uuid": request_id,
            },
        )

    def outbound_orders(
        self,
        house_id: int,
        house_code: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        return self._page_data(
            "/commonApi/dropshipping/queryOutboundOrders",
            {
                "param": {
                    "size": 1000,
                    "current": 1,
                    "query": {
                        "listStatus": [],
                        "houseCode": house_code,
                        "houseId": house_id,
                        "ids": [],
                        "keywordList": [],
                        "numType": 0,
                    },
                },
                "uuid": request_id,
            },
        )

    def returned_orders(self, house_code: str, request_id: str) -> Mapping[str, Any]:
        return self._page_data(
            "/commonApi/dropshipping/queryReBoundOrders",
            {
                "param": {
                    "query": {"keywordList": [], "houseCode": house_code},
                    "size": 1000,
                    "current": 1,
                },
                "uuid": request_id,
            },
        )

    def _list_data(self, path: str, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        payload = self._post(path, body)
        data = payload.get("data")
        if not isinstance(data, list):
            raise W8ApiError("长睿 W8 返回的数据列表格式异常")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def _page_data(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self._post(path, body)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise W8ApiError("长睿 W8 返回的分页数据格式异常")
        records = data.get("records")
        if not isinstance(records, list):
            raise W8ApiError("长睿 W8 返回的分页记录格式异常")
        return data

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if path not in READ_ONLY_PATHS:
            raise ValueError("W8Client 仅允许调用已审核的只读查询接口")
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = self._client.post(path.lstrip("/"), json=dict(body))
            except httpx.HTTPError as exc:
                if attempt < len(RETRY_DELAYS):
                    self._sleep(RETRY_DELAYS[attempt])
                    continue
                raise W8ApiError(self._sanitize(f"长睿 W8 连接失败：{type(exc).__name__}")) from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < len(RETRY_DELAYS):
                    self._sleep(RETRY_DELAYS[attempt])
                    continue
            if response.status_code >= 400:
                raise W8ApiError(f"长睿 W8 返回 HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError:
                raise W8ApiError("长睿 W8 返回的内容不是有效 JSON") from None
            if not isinstance(payload, Mapping):
                raise W8ApiError("长睿 W8 返回的内容格式异常")
            code = payload.get("code")
            if str(code) != "0":
                message = str(payload.get("msg") or payload.get("errorMsg") or f"业务码 {code}")
                raise W8ApiError(self._sanitize(f"长睿 W8 鉴权或查询失败：{message}"))
            return payload
        raise AssertionError("W8 retry loop must return or raise")

    def _sanitize(self, message: str) -> str:
        return message.replace(self._token, "[REDACTED]")
