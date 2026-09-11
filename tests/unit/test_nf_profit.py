from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from takealot_ops.nf_profit import calculate_cells, load_nf_catalog, workbook_profit
from takealot_ops.container_selection import _own_profile_payload
from takealot_ops.profitability import ProfitabilityOffer, build_own_store_profitability_payload


CATALOG = load_nf_catalog()
VERIFIED = [p for p in CATALOG["profiles"] if not p["issues"]]


@pytest.mark.parametrize("profile", VERIFIED, ids=lambda p: f"sheet-row-{p['row']}")
def test_replays_original_excel_cached_results(profile):
    actual = calculate_cells(profile, Decimal(str(profile["reference_price_zar"])))
    for column, expected in profile["reference_results"].items():
        if column == "O":
            continue
        assert float(actual[column]) == pytest.approx(expected, abs=1e-7, rel=0)


@pytest.mark.parametrize("volume,weight,fee", [
    (35000, 7, 60), (35000, 25, 60), (35000, 40, 107), (35000, 70, 107),
    (35001, 25, 65), (130000, 40, 107), (130000, 41, 160),
    (200000, 25, 130), (545000, 70, 172),
])
def test_ordered_delivery_boundaries_match_ifs(volume, weight, fee):
    profile = deepcopy(VERIFIED[0])
    profile["inputs"].update(F=volume / 1000000, I=weight)
    assert calculate_cells(profile, Decimal(1000))["L"] == fee


def test_delivery_outside_last_tier_is_missing_not_free():
    profile = deepcopy(VERIFIED[0])
    profile["inputs"]["F"] = 0.546
    with pytest.raises(ValueError, match="运费阶梯"):
        calculate_cells(profile, Decimal(1000))


def test_current_price_recomputes_commission_ads_and_workbook_margin():
    p = VERIFIED[0]
    low = calculate_cells(p, Decimal(1399))
    high = calculate_cells(p, Decimal(1499))
    assert high["K"] - low["K"] == Decimal("11.5")
    assert float(high["V"] - low["V"]) == pytest.approx(85.5 / (2.6 / .97))
    assert high["W"] == high["V"] * Decimal("2.6") * Decimal(".97")
    assert high["X"] == high["W"] / 1499
    assert high["U"] == low["U"]


def test_conflicting_identity_bad_source_and_ambiguous_profiles_do_not_fallback():
    p = deepcopy(VERIFIED[0])
    catalog = {**CATALOG, "profiles": [p]}
    args = dict(company_sku=p["company_sku"], platform_sku="safe", store_code="store-02",
                price=Decimal(1399))
    assert workbook_profit(catalog, **args)["status"] == "available"
    assert workbook_profit(catalog, **{**args, "platform_sku": "9902271464806"})["calculation"] is None
    different = deepcopy(p)
    different["inputs"]["S"] = .1
    catalog["profiles"].append(different)
    assert "多套" in workbook_profit(catalog, **args)["message"]
    catalog["profiles"] = [{**p, "issues": ["源表公式错误"]}]
    assert workbook_profit(catalog, **args)["calculation"] is None


def test_exact_store_uses_its_profile_and_unknown_store_does_not_guess():
    first = deepcopy(VERIFIED[0])
    other = deepcopy(first)
    other["stores"] = ["store-05"]
    other["inputs"]["U"] = 999
    catalog = {**CATALOG, "profiles": [first, other]}
    args = dict(company_sku=first["company_sku"], platform_sku="safe", price=Decimal(1399))
    exact = workbook_profit(catalog, store_code="store-02", **args)
    assert exact["calculation"]["cost_rmb"] == first["inputs"]["U"]
    assert workbook_profit(catalog, store_code="unlisted", **args)["calculation"] is None


def test_missing_or_incompatible_snapshot_fails_closed(tmp_path, monkeypatch):
    from takealot_ops import nf_profit

    path = tmp_path / "model.json"
    monkeypatch.setattr(nf_profit, "MODEL_PATH", path)
    assert nf_profit.load_nf_catalog().get("error")
    path.write_text('{"schema_version": 99, "profiles": []}', encoding="utf-8")
    assert nf_profit.load_nf_catalog().get("error")


def test_fixed_workbook_model_never_calls_market_rate_or_uses_legacy_cost():
    class NoNetwork:
        def latest(self):
            raise AssertionError("Workbook calculation must never fetch live FX")

    p = VERIFIED[0]
    offer = ProfitabilityOffer(
        store_code="store-02", store_name="test", offer_id="one", plid="1", sku="safe",
        company_sku=p["company_sku"], company_product_name="test", cost_rmb=Decimal(999),
        cost_effective_date=date(2026, 1, 1), selling_price_zar=Decimal(1399), rrp_zar=None,
        fee_covered_days=0, fee_lines=(),
    )
    args = dict(rate_service=NoNetwork(), store_codes={"store-02"},
                fee_window_start=date(2026, 8, 1), fee_window_end=date(2026, 8, 30),
                fee_window_days=30, nf_catalog={**CATALOG, "profiles": [p]})
    item = build_own_store_profitability_payload([offer], **args)["items"][0]
    assert item["workbook_profit"]["calculation"]["profit_rmb"] == pytest.approx(238.83161043956)
    assert item["cost_rmb"] == p["inputs"]["U"]
    assert item["scenarios"]["current_fee_adjusted"] is None
    missing = build_own_store_profitability_payload(
        [replace(offer, company_sku="unknown")], **args)["items"][0]
    assert missing["workbook_profit"]["calculation"] is None
    assert missing["cost_rmb"] is None


def test_container_uses_workbook_margin_without_double_charging_first_leg():
    model = workbook_profit({**CATALOG, "profiles": [VERIFIED[0]]},
        company_sku=VERIFIED[0]["company_sku"], platform_sku="safe",
        store_code="store-02", price=Decimal(1399))
    offer = {"store_code": "store-02", "offer_id": "one", "workbook_profit": model}
    args = dict(product_name="test", plids=["1"], links=[], profit_items=[offer],
                rate_available=False, as_of=date(2026, 9, 11), window_start=date(2026, 8, 12),
                target_cover_days=30, clearance_days=30, minimum_known_days=14,
                minimum_recent_monthly_units=3, strong_recent_monthly_units=6,
                maximum_recent_decline_ratio=.2)
    profile = {"company_sku": "test", "sea_freight_rmb": 100, "unit_cbm": .1}
    result = _own_profile_payload(profile, **args)["profit"]
    assert result["status"] == "available"
    assert result["items"][0]["profit_rmb_after_direct_fees_and_sea"] == 238.83
    assert result["items"][0]["profit_margin_percentage"] == 43.05
    args["profit_items"] = [offer, {"workbook_profit": {"status": "unavailable"}}]
    assert _own_profile_payload(profile, **args)["profit"]["status"] != "available"
