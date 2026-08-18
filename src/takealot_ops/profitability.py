"""Evidence-bounded unit profitability for exact connected-store Offers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from takealot_ops.exchange_rates import (
    RATE_SOURCE,
    ExchangeRateProvider,
    ExchangeRateQuote,
    ExchangeRateUnavailable,
)
from takealot_ops.product_master import load_product_master_links, normalize_product_sku
from takealot_ops.storage.models import DailySalesMetricState, ErpStore, OfferCurrent, SaleItem
from takealot_ops.storage.store_context import normalize_store_code, store_scope


DEFAULT_FEE_WINDOW_DAYS = 30
_MONEY_QUANTUM = Decimal("0.01")
_PERCENT_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class ProfitabilitySalesLine:
    """One formal Seller Sales line used only to derive a weighted fee ratio."""

    sales_day: date
    selling_price: Decimal | None
    total_fees: Decimal | None
    quantity: int


@dataclass(frozen=True)
class ProfitabilityOffer:
    """Exact current Offer, current cost identity, and its verified fee sample."""

    store_code: str
    store_name: str
    offer_id: str
    plid: str
    sku: str | None
    company_sku: str | None
    company_product_name: str | None
    cost_rmb: Decimal | None
    cost_effective_date: date | None
    selling_price_zar: Decimal | None
    rrp_zar: Decimal | None
    fee_covered_days: int
    fee_lines: tuple[ProfitabilitySalesLine, ...]


def empty_own_store_profitability(
    *,
    store_codes: set[str],
    fee_window_end: date,
    fee_window_days: int = DEFAULT_FEE_WINDOW_DAYS,
    message: str,
) -> dict[str, Any]:
    """Return the stable empty response contract without requesting a live rate."""
    fee_window_start = _fee_window_start(fee_window_end, fee_window_days)
    return {
        "items": [],
        "store_codes": sorted(store_codes),
        "fee_window": _fee_window_payload(
            fee_window_start=fee_window_start,
            fee_window_end=fee_window_end,
            fee_window_days=fee_window_days,
        ),
        "exchange_rate": _exchange_rate_payload(None, status="not_required"),
        "message": message,
    }


def load_own_store_profitability(
    engine: Engine,
    *,
    plid: str,
    store_codes: set[str],
    rate_service: ExchangeRateProvider,
    cost_as_of: date,
    fee_window_end: date,
    fee_window_days: int = DEFAULT_FEE_WINDOW_DAYS,
) -> dict[str, Any]:
    """Load local exact-Offer evidence and build current unit-profit scenarios.

    This function performs no Takealot or warehouse calls. The only possible
    network access is delegated to the shared cached CNY/ZAR rate service.
    """
    normalized_plid = str(plid or "").strip()
    normalized_codes = sorted(
        {
            normalize_store_code(store_code)
            for store_code in store_codes
            if str(store_code or "").strip()
        }
    )
    if not normalized_plid or not normalized_codes:
        return empty_own_store_profitability(
            store_codes=set(normalized_codes),
            fee_window_end=fee_window_end,
            fee_window_days=fee_window_days,
            message="当前授权范围内没有可计算利润的自有 Offer。",
        )

    fee_window_start = _fee_window_start(fee_window_end, fee_window_days)
    offers: list[ProfitabilityOffer] = []
    with Session(engine) as session:
        store_names = {
            str(store.code): str(store.display_name)
            for store in session.scalars(
                select(ErpStore).where(ErpStore.code.in_(normalized_codes))
            )
        }
        for store_code in normalized_codes:
            with store_scope(store_code):
                offers.extend(
                    _load_store_offers(
                        session,
                        plid=normalized_plid,
                        store_code=store_code,
                        store_name=store_names.get(store_code, store_code),
                        cost_as_of=cost_as_of,
                        fee_window_start=fee_window_start,
                        fee_window_end=fee_window_end,
                    )
                )
    return build_own_store_profitability_payload(
        offers,
        rate_service=rate_service,
        store_codes=set(normalized_codes),
        fee_window_start=fee_window_start,
        fee_window_end=fee_window_end,
        fee_window_days=fee_window_days,
    )


def build_own_store_profitability_payload(
    offers: Sequence[ProfitabilityOffer],
    *,
    rate_service: ExchangeRateProvider,
    store_codes: set[str],
    fee_window_start: date,
    fee_window_end: date,
    fee_window_days: int,
) -> dict[str, Any]:
    """Build JSON-safe unit-profit scenarios from explicit local evidence."""
    quote: ExchangeRateQuote | None = None
    has_cost = any(_positive_decimal(offer.cost_rmb) is not None for offer in offers)
    rate_status = "not_required"
    if has_cost:
        try:
            quote = rate_service.latest()
        except ExchangeRateUnavailable:
            rate_status = "unavailable"
        else:
            rate_status = "stale" if quote.stale else "converted"

    items = [
        _offer_payload(
            offer,
            quote=quote,
            fee_window_days=fee_window_days,
        )
        for offer in offers
    ]
    if not items:
        message = "当前 PLID 没有可见的 Seller API 自有 Offer。"
    elif not has_cost:
        message = "当前自有 Offer 均未关联人民币单件成本，暂不能计算利润。"
    elif quote is None:
        message = "人民币成本已保留，但参考汇率暂不可用，未生成利润换算。"
    else:
        message = "利润按当前单件售价、当前有效人民币成本和披露汇率逐 Offer 计算。"
    return {
        "items": items,
        "store_codes": sorted(store_codes),
        "fee_window": _fee_window_payload(
            fee_window_start=fee_window_start,
            fee_window_end=fee_window_end,
            fee_window_days=fee_window_days,
        ),
        "exchange_rate": _exchange_rate_payload(quote, status=rate_status),
        "message": message,
    }


def _load_store_offers(
    session: Session,
    *,
    plid: str,
    store_code: str,
    store_name: str,
    cost_as_of: date,
    fee_window_start: date,
    fee_window_end: date,
) -> list[ProfitabilityOffer]:
    current_offers = list(
        session.scalars(
            select(OfferCurrent)
            .where(OfferCurrent.productline_id == plid)
            .order_by(OfferCurrent.offer_id)
        )
    )
    if not current_offers:
        return []

    links = load_product_master_links(
        session,
        platform_skus=[offer.sku for offer in current_offers if offer.sku],
        as_of_date=cost_as_of,
    )
    states = list(
        session.scalars(
            select(DailySalesMetricState).where(
                DailySalesMetricState.metric_date >= fee_window_start,
                DailySalesMetricState.metric_date <= fee_window_end,
            )
        )
    )
    verified_dates = sorted(
        {
            state.metric_date
            for state in states
            if _sales_state_is_verified(state)
        }
    )
    offer_ids = [str(offer.offer_id) for offer in current_offers]
    sales = (
        list(
            session.scalars(
                select(SaleItem).where(
                    SaleItem.offer_id.in_(offer_ids),
                    SaleItem.sales_day.in_(verified_dates),
                )
            )
        )
        if verified_dates
        else []
    )
    sales_by_offer: dict[str, list[ProfitabilitySalesLine]] = defaultdict(list)
    for sale in sales:
        offer_id = str(sale.offer_id or "").strip()
        if not offer_id:
            continue
        sales_by_offer[offer_id].append(
            ProfitabilitySalesLine(
                sales_day=sale.sales_day,
                selling_price=_decimal_or_none(sale.selling_price),
                total_fees=_decimal_or_none(sale.total_fees),
                quantity=int(sale.quantity or 0),
            )
        )

    result: list[ProfitabilityOffer] = []
    for offer in current_offers:
        offer_id = str(offer.offer_id)
        sku = str(offer.sku).strip() if offer.sku else None
        link = links.get(normalize_product_sku(sku)) if sku else None
        result.append(
            ProfitabilityOffer(
                store_code=store_code,
                store_name=store_name,
                offer_id=offer_id,
                plid=plid,
                sku=sku,
                company_sku=link.company_sku if link is not None else None,
                company_product_name=link.product_name if link is not None else None,
                cost_rmb=link.cost_rmb if link is not None else None,
                cost_effective_date=(
                    link.cost_effective_date if link is not None else None
                ),
                selling_price_zar=_decimal_or_none(offer.selling_price),
                rrp_zar=_decimal_or_none(offer.rrp),
                fee_covered_days=len(verified_dates),
                fee_lines=tuple(sales_by_offer.get(offer_id, [])),
            )
        )
    return result


def _offer_payload(
    offer: ProfitabilityOffer,
    *,
    quote: ExchangeRateQuote | None,
    fee_window_days: int,
) -> dict[str, Any]:
    cost_rmb = _positive_decimal(offer.cost_rmb)
    selling_price_zar = _positive_decimal(offer.selling_price_zar)
    rrp_zar = _positive_decimal(offer.rrp_zar)
    rate = quote.rate if quote is not None else None
    fee_basis, fee_rate = _fee_basis_payload(
        offer.fee_lines,
        covered_days=offer.fee_covered_days,
        total_days=fee_window_days,
    )

    current_gross = _profit_scenario(
        key="current_gross",
        label="当前售价毛利润",
        price_zar=selling_price_zar,
        cost_rmb=cost_rmb,
        rate=rate,
        fee_rate=Decimal(0),
        note="当前单件售价换算成人民币后减当前单件成本；未扣平台及履约费用。",
    )
    current_fee_adjusted = (
        _profit_scenario(
            key="current_fee_adjusted",
            label="平台直接费用后利润（估算）",
            price_zar=selling_price_zar,
            cost_rmb=cost_rmb,
            rate=rate,
            fee_rate=fee_rate,
            note=(
                "按近 30 个已完成南非自然日中同店同 Offer 的已验证 "
                "Seller Sales 综合费率估算；total_fees 包含成功费、履约费、"
                "揽收费和库存调拨费，不含仓储费、广告费、月租、头程、税费或退货损失。"
            ),
        )
        if fee_rate is not None
        else None
    )
    rrp_gross = (
        _profit_scenario(
            key="rrp_gross",
            label="原价毛利润",
            price_zar=rrp_zar,
            cost_rmb=cost_rmb,
            rate=rate,
            fee_rate=Decimal(0),
            note="Takealot 当前 Offer 原价换算成人民币后减当前单件成本；未扣费用。",
        )
        if rrp_zar is not None and rrp_zar != selling_price_zar
        else None
    )
    cost_zar = (
        _money(cost_rmb * rate)
        if cost_rmb is not None and rate is not None
        else None
    )
    if cost_rmb is None:
        message = "该 Offer 的平台 SKU 尚未关联人民币单件成本，未计算利润。"
    elif rate is None:
        message = "参考汇率暂不可用；保留人民币成本，未生成利润换算。"
    elif selling_price_zar is None:
        message = "当前 Offer 售价缺失，未生成当前售价利润。"
    else:
        message = "已按当前选中 Offer/SKU 生成可复算的单件利润场景。"
    return {
        "offer_key": f"seller-api:{offer.store_code}:{offer.offer_id}",
        "store_code": offer.store_code,
        "store_name": offer.store_name,
        "offer_id": offer.offer_id,
        "plid": offer.plid,
        "sku": offer.sku,
        "company_sku": offer.company_sku,
        "company_product_name": offer.company_product_name,
        "cost_rmb": _float_or_none(cost_rmb),
        "cost_effective_date": (
            offer.cost_effective_date.isoformat()
            if offer.cost_effective_date is not None
            else None
        ),
        "cost_zar": _float_or_none(cost_zar),
        "selling_price_zar": _float_or_none(selling_price_zar),
        "rrp_zar": _float_or_none(rrp_zar),
        "fee_basis": fee_basis,
        "scenarios": {
            "current_gross": current_gross,
            "current_fee_adjusted": current_fee_adjusted,
            "rrp_gross": rrp_gross,
        },
        "message": message,
    }


def _fee_basis_payload(
    lines: Sequence[ProfitabilitySalesLine],
    *,
    covered_days: int,
    total_days: int,
) -> tuple[dict[str, Any], Decimal | None]:
    base = {
        "window_days": total_days,
        "covered_days": max(0, min(covered_days, total_days)),
        "sales_days": len({line.sales_day for line in lines}),
        "order_line_count": len(lines),
        "ordered_units": sum(max(0, int(line.quantity)) for line in lines),
        "sales_revenue_zar": None,
        "total_fees_zar": None,
        "fee_rate_percentage": None,
        "source": "Takealot Seller Sales /sales",
    }
    if covered_days <= 0:
        return (
            {
                **base,
                "status": "unverified",
                "message": "费用窗口内没有 Seller Sales 已验证日，未估算扣费后利润。",
            },
            None,
        )
    if not lines:
        return (
            {
                **base,
                "status": "no_sales",
                "message": "已验证窗口内该 Offer 没有销售行，未估算综合费率。",
            },
            None,
        )

    valid_rows: list[tuple[Decimal, Decimal]] = []
    invalid_count = 0
    for line in lines:
        price = _positive_decimal(line.selling_price)
        fees = _non_negative_decimal(line.total_fees)
        if price is None or fees is None:
            invalid_count += 1
            continue
        valid_rows.append((price, fees))
    if invalid_count or not valid_rows:
        return (
            {
                **base,
                "status": "incomplete",
                "invalid_line_count": invalid_count,
                "message": "销售行的售价或总费用不完整，未使用部分样本估算费率。",
            },
            None,
        )

    revenue = sum((price for price, _ in valid_rows), Decimal(0))
    fees = sum((fee for _, fee in valid_rows), Decimal(0))
    if revenue <= 0:
        return (
            {
                **base,
                "status": "incomplete",
                "invalid_line_count": 0,
                "message": "销售样本收入不是正数，未估算综合费率。",
            },
            None,
        )
    fee_rate = fees / revenue
    return (
        {
            **base,
            "status": "available",
            "invalid_line_count": 0,
            "sales_revenue_zar": float(_money(revenue)),
            "total_fees_zar": float(_money(fees)),
            "fee_rate_percentage": float(_percentage(fee_rate * Decimal(100))),
            "message": (
                "综合费率 = 同店同 Offer 已验证 Seller Sales 行 total_fees 合计 / "
                "selling_price 合计；total_fees 包含成功费、履约费、揽收费和"
                "库存调拨费，不含仓储费、广告费及月租。"
            ),
        },
        fee_rate,
    )


def _profit_scenario(
    *,
    key: str,
    label: str,
    price_zar: Decimal | None,
    cost_rmb: Decimal | None,
    rate: Decimal | None,
    fee_rate: Decimal,
    note: str,
) -> dict[str, Any] | None:
    if price_zar is None or cost_rmb is None or rate is None or rate <= 0:
        return None
    estimated_fees_zar = price_zar * fee_rate
    price_rmb_exact = price_zar / rate
    fees_rmb_exact = estimated_fees_zar / rate
    profit_rmb_exact = price_rmb_exact - fees_rmb_exact - cost_rmb
    return {
        "key": key,
        "label": label,
        "price_zar": float(_money(price_zar)),
        "price_rmb": float(_money(price_rmb_exact)),
        "estimated_fees_zar": float(_money(estimated_fees_zar)),
        "estimated_fees_rmb": float(_money(fees_rmb_exact)),
        "profit_rmb": float(_money(profit_rmb_exact)),
        "profit_margin_percentage": float(
            _percentage(profit_rmb_exact / price_rmb_exact * Decimal(100))
        ),
        "cost_markup_percentage": float(
            _percentage(profit_rmb_exact / cost_rmb * Decimal(100))
        ),
        "note": note,
    }


def _exchange_rate_payload(
    quote: ExchangeRateQuote | None,
    *,
    status: str,
) -> dict[str, Any]:
    if quote is None:
        message = (
            "汇率服务暂不可用；未生成兰特与人民币利润换算。"
            if status == "unavailable"
            else "当前没有可换算的人民币成本，因此未请求汇率。"
        )
        return {
            "base_currency": "CNY",
            "quote_currency": "ZAR",
            "rate": None,
            "rate_date": None,
            "fetched_at": None,
            "source": RATE_SOURCE,
            "status": status,
            "message": message,
        }
    return {
        "base_currency": "CNY",
        "quote_currency": "ZAR",
        "rate": float(quote.rate),
        "rate_date": quote.rate_date.isoformat(),
        "fetched_at": _utc_datetime(quote.fetched_at).isoformat(),
        "source": RATE_SOURCE,
        "status": status,
        "message": (
            "汇率刷新失败，使用本进程最近一次成功缓存；仅用于利润估算。"
            if quote.stale
            else "按最新发布的机构参考汇率换算，仅供利润估算，非交易结算价。"
        ),
    }


def _fee_window_payload(
    *,
    fee_window_start: date,
    fee_window_end: date,
    fee_window_days: int,
) -> dict[str, Any]:
    return {
        "start": fee_window_start.isoformat(),
        "end": fee_window_end.isoformat(),
        "days": fee_window_days,
        "date_basis": "Africa/Johannesburg",
    }


def _fee_window_start(fee_window_end: date, fee_window_days: int) -> date:
    if fee_window_days <= 0:
        raise ValueError("fee_window_days must be positive")
    return fee_window_end - timedelta(days=fee_window_days - 1)


def _sales_state_is_verified(state: DailySalesMetricState) -> bool:
    if state.source_kind != "takealot_sales_api":
        return False
    raw_value: object = state.verified_at
    if raw_value is None:
        details = state.source_details if isinstance(state.source_details, dict) else {}
        raw_value = details.get("verified_at") or details.get("collected_at")
    if isinstance(raw_value, datetime):
        return True
    if not isinstance(raw_value, str):
        return False
    try:
        datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    amount = _decimal_or_none(value)
    return amount if amount is not None and amount > 0 else None


def _non_negative_decimal(value: Any) -> Decimal | None:
    amount = _decimal_or_none(value)
    return amount if amount is not None and amount >= 0 else None


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _percentage(value: Decimal) -> Decimal:
    return value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def _float_or_none(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
