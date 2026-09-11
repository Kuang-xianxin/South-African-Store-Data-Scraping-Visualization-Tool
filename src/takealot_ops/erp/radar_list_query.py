"""Compact list index; preserve the radar's product/search/filter semantics."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from fastapi import Query


@dataclass
class RadarListQuery:
    page: int | None = Query(default=None, ge=1)
    page_size: int = Query(default=20, ge=1, le=100)
    q: str = Query(default="", max_length=200)
    seller: str = Query(default="", max_length=200)
    stock: str = Query(default="全部", max_length=20)
    status: str = Query(default="全部", max_length=40)
    follower: str = Query(default="全部", max_length=20)
    signal: str = Query(default="全部", max_length=40)
    direction: str = Query(default="desc", pattern="^(asc|desc)$")
    sort: str = Query(default="signal", pattern="^(signal|(follower_)?sales_(7|15|30|60|90|total))$")
    watchlist: bool = Query(default=False)


SORT_FIELDS = {
    "降价": "价格变化", "涨价": "价格变化", "价格不变": "价格变化",
    "库存减少": "周期销售件数", "补货": "周期销售额",
    "库存减少且评论增加": "周期销售额", "库存数量不变": "库存净变化",
    "库存变化大": "周期库存周转金额", "评论增加": "新增评论",
    "好评增加": "新增好评", "差评增加": "新增差评", "新增跟卖卖家": "新增跟卖卖家数",
}

SALES_WINDOWS = ("7", "15", "30", "60", "90", "total")


def sales_sort_values(item: dict[str, Any]) -> dict[str, float | int | None]:
    own = item.get("来源") == "own_store"
    sales = item.get("自有官方销量" if own else "近期观察售出") or {}
    followers = (item.get("跟卖近期观察售出") or {}) if own else sales
    return {f"{prefix}_{window}": value if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) else None
            for prefix, values in (("sales", sales), ("follower_sales", followers))
            for window in SALES_WINDOWS for value in [values.get(window)]}


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 0


def operating_signals(item: dict[str, Any]) -> list[str]:
    offers = item.get("跟卖报价") or []
    signals = {v for v in [item.get("价格信号"), *(o.get("价格信号") for o in offers)]
               if v in {"降价", "涨价", "价格不变"}}
    sales, replenishment = item.get("周期销售件数"), item.get("周期补货量")
    increased = _positive(replenishment) if replenishment is not None else (
        item.get("趋势判断") == "检测到补货"
        or any(o.get("库存信号") in {"库存增加", "恢复有货"} for o in offers))
    decreased = _positive(sales) if sales is not None else (
        _positive(item.get("库存净流出"))
        or any(o.get("库存信号") in {"库存减少", "转为没货"} for o in offers))
    if increased:
        signals.add("补货")
    if decreased:
        signals.add("库存减少")
    unchanged = item.get("库存可比") is True and sales == 0 and replenishment == 0
    if sales is None and replenishment is None and not increased and not decreased:
        unchanged = ((item.get("库存可比") is True and item.get("库存净变化") == 0)
                     or any(o.get("库存信号") == "库存数量不变" for o in offers))
    if unchanged:
        signals.add("库存数量不变")
    for signal, field in [("库存变化大", "周期库存周转金额"), ("评论增加", "新增评论"),
                          ("好评增加", "新增好评"), ("差评增加", "新增差评"),
                          ("新增跟卖卖家", "新增跟卖卖家数")]:
        if _positive(item.get(field)):
            signals.add(signal)
    if decreased and _positive(item.get("新增评论")):
        signals.add("库存减少且评论增加")
    return sorted(signals)


def list_index(item: dict[str, Any]) -> dict[str, Any]:
    """No card bodies/history in the searchable index."""
    own, followers = item.get("自有报价") or [], item.get("跟卖报价") or []
    comparison = item.get("对比报价", followers) or []
    quantity, label = item.get("库存数量"), str(item.get("库存上限") or "")
    stock = "未探测"
    if not item.get("库存参考过期"):
        if quantity is not None:
            stock = "有货" if quantity > 0 else "没货"
        elif "没货" in label or "售罄" in label:
            stock = "没货"
        elif re.search(r"\d", label):
            stock = "有货"
    sellers = []
    for offer in comparison:
        raw_id = str(offer.get("卖家ID") or "").strip()
        seller_id = raw_id[1:] if re.fullmatch(r"m\d+", raw_id, re.I) else raw_id
        name = str(offer.get("卖家") or "").strip()
        if name.lower() in {"未知卖家", "unknown", "unknown seller"}:
            name = ""
        sellers.append({"卖家ID": seller_id, "卖家": name, "raw_id": raw_id})
    other = [item.get(k) for k in ("plid", "当前卖家", "库存上限", "趋势判断", "价格信号", "company_sku")]
    other.extend(item.get("company_skus") or [])
    for offer in own:
        other.extend(offer.get(k) for k in ("offer_id", "店铺", "SKU", "company_sku", "状态"))
    for offer in followers:
        other.extend(offer.get(k) for k in ("offer_id", "卖家ID", "卖家", "SKU", "变体", "库存状态", "价格信号", "库存信号"))
    # Own follower count is based on follower offers, never the own Seller API offers.
    current_followers = followers if item.get("来源") == "own_store" else [
        o for o in followers if (o.get("是否跟卖") if o.get("是否跟卖") is not None else not o.get("是否变体主报价"))]
    return {
        "plid": item["plid"], "source": item.get("来源"),
        "names": [item.get("商品"), *(o.get("company_product_name") for o in own)],
        "other": [str(v).lower() for v in other if v is not None],
        "sellers": sellers, "stock": stock,
        "own_status": [[o.get("最新Offer状态"), o.get("最新Offer库存状态")
                        if o.get("最新Offer库存状态") in {"有货", "没货"} else "未探测"]
                       for o in comparison if o.get("报价来源") == "seller_api"],
        "follower": "现在被跟卖" if current_followers else "曾经被跟卖" if item.get("跟卖发现日期") else "未发现跟卖",
        "signals": operating_signals(item),
        "sort": {**{k: item.get(v) for k, v in SORT_FIELDS.items()}, **sales_sort_values(item)},
        "collected": item.get("采集时间") or "", "snapshot": item.get("快照ID") or 0,
        "own_order": [str(item.get("趋势判断") or ""), str(item.get("商品") or "")],
        "exact": bool(item.get("库存精确")), "rating": item.get("评分"),
    }


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    return " ".join("".join(c if c.isalnum() else " " for c in text
                            if not unicodedata.category(c).startswith("M")).split())


def seller_groups(indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send unique seller counts/name evidence, not an offer list for every PLID."""
    groups: dict[str, dict[str, Any]] = {}
    for item in indexes:
        for seller in item["sellers"]:
            sid, name = seller["卖家ID"], seller["卖家"]
            key = f"id:{sid.lower()}" if sid else f"name:{name.lower()}" if name else ""
            if not key:
                continue
            group = groups.setdefault(key, {"key": key, "sellerId": sid or None, "plids": set(), "names": {}})
            group["plids"].add(item["plid"])
            if name:
                evidence = group["names"].setdefault(name.lower(), {"value": name, "count": 0})
                evidence["count"] += 1
    return [{"key": g["key"], "sellerId": g["sellerId"], "productCount": len(g["plids"]),
             "names": list(g["names"].values())} for g in groups.values()]


def _token_match(name: str, query: str) -> bool:
    if query in name:
        return True
    limit = 2 if len(query) >= 9 else 1 if len(query) >= 5 else 0
    if not limit or not any(c.isalpha() for c in query) or not any(c.isalpha() for c in name) or abs(len(name) - len(query)) > limit:
        return False
    matrix = [[0] * (len(query) + 1) for _ in range(len(name) + 1)]
    for i in range(len(name) + 1):
        matrix[i][0] = i
    for j in range(len(query) + 1):
        matrix[0][j] = j
    for i in range(1, len(name) + 1):
        for j in range(1, len(query) + 1):
            matrix[i][j] = min(matrix[i-1][j] + 1, matrix[i][j-1] + 1,
                               matrix[i-1][j-1] + (name[i-1] != query[j-1]))
            if i > 1 and j > 1 and name[i-1] == query[j-2] and name[i-2] == query[j-1]:
                matrix[i][j] = min(matrix[i][j], matrix[i-2][j-2] + 1)
    return matrix[-1][-1] <= limit


def matches_index(item: dict[str, Any], query: RadarListQuery, watchlist: set[str]) -> bool:
    if query.watchlist and item["source"] != "own_store" and item["plid"] not in watchlist:
        return False
    search = query.q.strip().lower()
    plid = re.search(r"plid(\d+)", search)
    if plid:
        search = plid[1]
    if search and not any(search in v for v in item["other"]):
        words = _normalized(search)
        def name_matches(value: Any) -> bool:
            name = _normalized(value)
            return (not words or words in name or words.replace(" ", "") in name.replace(" ", "")
                    or all(any(_token_match(n, q) for n in name.split()) for q in words.split()))
        if not any(name_matches(name) for name in item["names"]):
            return False
    if item["source"] == "own_store":
        if (query.stock != "全部" or query.status != "全部") and not any(
            (query.status == "全部" or query.status == status)
            and (query.stock == "全部" or query.stock == stock) for status, stock in item["own_status"]
        ):
            return False
    else:
        if query.stock != "全部" and query.stock != item["stock"]:
            return False
        seller_query = query.seller.strip().lower()
        if seller_query:
            def seller_matches(seller: dict[str, str]) -> bool:
                name, sid = seller["卖家"], seller["卖家ID"]
                values = [name, sid, seller["raw_id"]]
                if sid:
                    values.extend([f"M{sid}", f"sellers {sid}", f"sellers={sid}"])
                values.append(f"{name} · sellers {sid}" if name and sid else name or f"sellers {sid}")
                return any(seller_query in v.lower() for v in values)
            if not any(seller_matches(s) for s in item["sellers"]):
                return False
    return ((query.follower == "全部" or query.follower == item["follower"])
            and (query.signal == "全部" or query.signal in item["signals"]))
