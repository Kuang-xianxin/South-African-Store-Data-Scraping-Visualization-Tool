from copy import deepcopy
from decimal import Decimal

import pytest

from takealot_ops.erp.radar_profit import radar_profit_summary
from takealot_ops.nf_profit import load_nf_catalog, workbook_profit


@pytest.fixture
def example():
    catalog = deepcopy(load_nf_catalog())
    profile = next(p for p in catalog["profiles"] if not p["issues"])
    catalog["profiles"] = [profile]
    catalog["mapping_authority"]["mappings"] = {
        "9900000000001": {"company_sku": profile["company_sku"], "source_rows": [2]},
    }
    quote = {"offer_id": "1", "SKU": "9900000000001", "店铺": "Shop", "价格": 1399,
             "company_sku": profile["company_sku"]}
    comparison = {**quote, "卖家ID": "current", "报价来源": "seller_api"}
    return catalog, {"自有报价": [quote], "对比报价": [comparison]}


def test_exact_current_offer_formula_and_partial_mapping_without_mutation(example):
    catalog, card = example
    card["自有报价"].append({**card["自有报价"][0], "offer_id": "2", "SKU": "missing", "价格": 339})
    card["对比报价"].append({**card["自有报价"][1], "卖家ID": "current", "报价来源": "seller_api"})
    before = deepcopy(card)
    actual = radar_profit_summary(card, catalog)
    expected = workbook_profit(catalog, company_sku=card["自有报价"][0]["company_sku"],
                               platform_sku="9900000000001", store_code="current", price=Decimal(1399))["calculation"]
    assert actual["profit"] == [expected["profit_zar"]] * 2
    assert actual["margin"] == [expected["margin_percentage"]] * 2
    assert actual["available"] == 1 and actual["total"] == 2
    assert "未收录" in actual["reasons"][0]
    assert card == before


@pytest.mark.parametrize("price", [None, 0, -1, "NaN", "Infinity", "bad"])
def test_unpriced_offers_never_become_zero_cost_profit(example, price):
    catalog, card = example
    card["自有报价"][0]["价格"] = price
    assert radar_profit_summary(card, catalog)["profit"] is None


@pytest.mark.parametrize("field,value", [("报价来源", "public_offer"), ("offer_id", "2"),
                                        ("SKU", "other"), ("店铺", "Shop 2")])
def test_public_or_mismatching_identity_cannot_supply_store_profile(example, field, value):
    catalog, card = example
    card["对比报价"][0][field] = value
    assert radar_profit_summary(card, catalog)["available"] == 0


def test_ambiguous_stores_cannot_choose_arbitrary_profile(example):
    catalog, card = example
    card["对比报价"].append({**card["对比报价"][0], "卖家ID": "store-02"})
    assert radar_profit_summary(card, catalog)["profit"] is None


def test_ranges_use_only_authorized_own_prices_including_losses(example):
    catalog, card = example
    card["自有报价"].append({**card["自有报价"][0], "价格": 1})
    card["对比报价"].append({**card["对比报价"][0], "价格": 999999, "报价来源": "public_offer"})
    result = radar_profit_summary(card, catalog)
    assert result["available"] == 2
    assert result["profit"][0] < 0 < result["profit"][1]


def test_empty_cards_are_unknown_not_zero(example):
    assert radar_profit_summary({}, example[0])["profit"] is None
