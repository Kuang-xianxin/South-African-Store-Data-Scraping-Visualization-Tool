"""NF workbook model: exact source rules, explicit missingness, no network or writes."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any


MODEL_PATH = Path(__file__).resolve().parents[2] / "config" / "nf_profit_model.json"


@lru_cache(maxsize=4)
def _read_catalog(path: str, mtime: int) -> dict[str, Any]:
    del mtime
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(payload.get("profiles"), list)):
        raise ValueError("Unsupported NF model snapshot")
    return payload


def load_nf_catalog() -> dict[str, Any]:
    """Only read the validated deployment snapshot, never the live Excel file."""
    try:
        return _read_catalog(str(MODEL_PATH), MODEL_PATH.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {"source": {}, "profiles": [], "error": "利润计算表配置缺失或损坏。"}


def calculate_cells(profile: dict[str, Any], price: Decimal) -> dict[str, Decimal]:
    """Reproduce J:X without intermediate rounding; G/H overrides remain inputs."""
    inputs = profile["inputs"]
    constants = profile["constants"]
    overrides = profile["overrides"]

    def number(value: Any) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("非有限数值")
        return result

    def value(column: str, computed: Decimal) -> Decimal:
        return number(overrides[column]) if column in overrides else computed

    c = {key: number(inputs[key]) for key in ("F", "I", "Q", "R", "S", "U")}
    if price <= 0 or any(c[k] <= 0 for k in ("F", "I", "Q", "R", "U")):
        raise ValueError("售价、箱规、重量、汇率或成本缺失/非正数")
    if not 0 < c["R"] <= 1 or not 0 <= c["S"] < 1:
        raise ValueError("汇损或广告比例无效")
    c["D"] = price
    c["G"] = value("G", c["F"] * 200)
    c["H"] = value("H", c["F"] * 1000000)
    if min(c["G"], c["H"]) <= 0:
        raise ValueError("体积重或体积无效")
    vat = number(constants["vat"])
    rate = c["Q"] / c["R"]
    c["J"] = value("J", max(c["G"], c["I"]) * number(constants["outbound_rmb_kg"])
                   * number(constants["outbound_factor"]) * rate)
    c["K"] = value("K", number(inputs["commission_rate"]) * price * (1 + vat))
    if "L" in overrides:
        c["L"] = number(overrides["L"])
    else:
        for volume, weight, fee in constants["delivery_tiers"]:
            if c["H"] <= number(volume) and c["I"] <= number(weight):
                c["L"] = number(fee)
                break
        else:
            raise ValueError("箱规/重量超出利润计算表运费阶梯")
    c["M"] = value("M", c["L"] * vat)
    c["N"] = value("N", 3 * c["Q"])
    c["P"] = value("P", Decimal("20.7") if inputs.get("O") == "是" else Decimal(0))
    if any(c[k] < 0 for k in ("J", "K", "L", "M", "N", "P")):
        raise ValueError("费用不能为负数")
    c["T"] = (price * (1 - c["S"]) - sum(c[k] for k in ("J", "K", "L", "M", "N", "P"))) / rate
    c["V"] = c["T"] - c["U"]
    c["W"] = c["V"] * c["Q"] * c["R"]
    c["X"] = c["W"] / price
    return c


def workbook_profit(
    catalog: dict[str, Any], *, company_sku: str | None, platform_sku: str | None,
    store_code: str, price: Decimal | None,
) -> dict[str, Any]:
    """Select only an unambiguous company/store profile and calculate current price."""
    result: dict[str, Any] = {
        "status": "unavailable", "source": catalog.get("source", {}),
        "source_rows": [], "message": "利润计算表没有该公司 SKU 的可用参数。",
        "calculation": None,
    }
    if catalog.get("error"):
        result["message"] = catalog["error"]
        return result
    if platform_sku in catalog.get("mapping_conflicts", []):
        result["message"] = "该 990 与公司 SKU 存在待核对的对应冲突。"
        return result
    candidates = [p for p in catalog["profiles"]
                  if p["company_sku"].casefold() == str(company_sku or "").strip().casefold()]
    exact = [p for p in candidates if store_code in p["stores"]]
    selected = exact or candidates
    result["source_rows"] = [p["row"] for p in selected]
    if not selected:
        return result
    issues = sorted({issue for p in selected for issue in p["issues"]})
    if issues:
        result["message"] = "；".join(issues)
        return result
    signatures = {json.dumps({k: p[k] for k in ("inputs", "constants", "overrides")},
                             sort_keys=True, ensure_ascii=False) for p in selected}
    if len(signatures) != 1:
        result["message"] = "同一公司 SKU 存在多套不同参数，无法唯一确定当前店铺口径。"
        return result
    if price is None:
        result["message"] = "当前 Offer 售价缺失。"
        return result
    try:
        cells = calculate_cells(selected[0], price)
    except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
        result["message"] = str(exc)
        return result
    rate = cells["Q"] / cells["R"]
    fees = sum(cells[k] for k in ("J", "K", "L", "M", "N", "P"))
    result.update(status="available", message="按 NF毛利计算.xlsx 利润计算表计算。",
                  calculation={
                      "cells": {k: float(v) for k, v in cells.items()},
                      "price_rmb": float(price / rate),
                      "cost_rmb": float(cells["U"]),
                      "fees_rmb": float((fees + price * cells["S"]) / rate),
                      "total_cost_rmb": float(cells["U"] + (fees + price * cells["S"]) / rate),
                      "profit_rmb": float(cells["V"]),
                      "profit_zar": float(cells["W"]),
                      "margin_percentage": float(cells["X"] * 100),
                  })
    return result
