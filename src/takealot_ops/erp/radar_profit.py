"""Small profit summaries from already authorized radar quotes; no detail reads."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from takealot_ops.nf_profit import workbook_profit


def radar_profit_summary(
    item: Mapping[str, Any], catalog: dict[str, Any],
) -> dict[str, Any]:
    own = item.get("自有报价") or []
    profits: list[float] = []
    margins: list[float] = []
    reasons: set[str] = set()
    # Minimal own quotes omit store_code. Resolve only an exact official
    # Offer/SKU/shop identity; public sellers cannot supply a private store.
    stores: dict[tuple[str, str, str], set[str]] = {}
    for offer in item.get("对比报价") or []:
        if offer.get("报价来源") != "seller_api":
            continue
        key = _identity(offer)
        code = str(offer.get("卖家ID") or "").strip()
        if all(key) and code:
            stores.setdefault(key, set()).add(code)
    for offer in own:
        codes = stores.get(_identity(offer), set())
        if len(codes) != 1:
            reasons.add("当前报价的店铺对应依据缺失或不唯一。")
            continue
        price: Decimal | None
        try:
            price = Decimal(str(offer.get("价格")))
            if not price.is_finite() or price <= 0:
                price = None
        except (InvalidOperation, ValueError):
            price = None
        result = workbook_profit(
            catalog, company_sku=offer.get("company_sku"), platform_sku=offer.get("SKU"),
            store_code=next(iter(codes)), price=price,
        )
        calculation = result["calculation"]
        if result["status"] != "available" or calculation is None:
            reasons.add(result["message"])
            continue
        profits.append(calculation["profit_zar"])
        margins.append(calculation["margin_percentage"])
    return {
        "profit": _range(profits), "margin": _range(margins),
        "available": len(profits), "total": len(own), "reasons": sorted(reasons),
    }


def _identity(offer: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(offer.get("offer_id") or "").strip(),
            str(offer.get("SKU") or "").strip(), str(offer.get("店铺") or "").strip())


def _range(values: Sequence[float]) -> list[float] | None:
    return [min(values), max(values)] if values else None
