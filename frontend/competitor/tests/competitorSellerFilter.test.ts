import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildCompetitorSellerOptions,
  matchesCompetitorSellerFilter,
  normalizeCompetitorSellerId,
  type CompetitorSellerFilterItem,
  type CompetitorSellerFilterOffer,
} from "../src/competitorSellerFilter.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

function offer(
  sellerId: string | number | null,
  sellerName: string,
): CompetitorSellerFilterOffer {
  return { 卖家ID: sellerId, 卖家: sellerName };
}

function item(
  plid: string,
  offers: CompetitorSellerFilterOffer[],
): CompetitorSellerFilterItem {
  return { plid, 跟卖报价: offers };
}

test("normalizes the public-offer M prefix without changing other seller IDs", () => {
  assert.equal(normalizeCompetitorSellerId("M29895177"), "29895177");
  assert.equal(normalizeCompetitorSellerId("m29895177"), "29895177");
  assert.equal(normalizeCompetitorSellerId(29895177), "29895177");
  assert.equal(normalizeCompetitorSellerId("Merchant-A"), "Merchant-A");
});

test("matches a true competitor by sellers ID, store name, or selected option value", () => {
  const competitor = item("PLID1", [
    offer("M29895177", "Danilo"),
    offer("29853614", "Tech IT Store"),
  ]);

  assert.equal(matchesCompetitorSellerFilter(competitor, "29895177"), true);
  assert.equal(matchesCompetitorSellerFilter(competitor, "M29895177"), true);
  assert.equal(matchesCompetitorSellerFilter(competitor, "sellers=29895177"), true);
  assert.equal(matchesCompetitorSellerFilter(competitor, "dAnIl"), true);
  assert.equal(
    matchesCompetitorSellerFilter(competitor, "Danilo · sellers 29895177"),
    true,
  );
  assert.equal(matchesCompetitorSellerFilter(competitor, "another shop"), false);
  assert.equal(matchesCompetitorSellerFilter(competitor, ""), true);
});

test("uses current comparison offers instead of stale fallback offers", () => {
  const competitor: CompetitorSellerFilterItem = {
    plid: "PLID2",
    跟卖报价: [offer("100", "Old Store")],
    对比报价: [offer("200", "Current Store")],
  };

  assert.equal(matchesCompetitorSellerFilter(competitor, "Old Store"), false);
  assert.equal(matchesCompetitorSellerFilter(competitor, "Current Store"), true);
});

test("deduplicates options by seller ID and counts unique products", () => {
  const options = buildCompetitorSellerOptions([
    item("PLID1", [
      offer("M101", "Shared Store"),
      offer("101", "Shared Store"),
    ]),
    item("PLID2", [offer(101, "SHARED STORE")]),
    item("PLID3", [offer("202", "Shared Store")]),
    item("PLID4", [offer(null, "Name Only Store")]),
    item("PLID5", [offer(null, "未知卖家")]),
  ]);

  assert.equal(options.length, 3);
  assert.deepEqual(
    options.filter((option) => option.sellerName.toLocaleLowerCase() === "shared store")
      .map((option) => option.sellerId)
      .sort(),
    ["101", "202"],
  );
  const seller101 = options.find((option) => option.sellerId === "101");
  assert.equal(seller101?.productCount, 2);
  assert.equal(seller101?.inputValue, "Shared Store · sellers 101");
  assert.equal(
    options.find((option) => option.sellerName === "Name Only Store")?.sellerId,
    null,
  );
});

test("true competitor workspace exposes an autocomplete seller selector", () => {
  assert.match(pageSource, /竞品店铺（\{\{ competitorSellerOptions\.length \}\} 个）/);
  assert.match(pageSource, /placeholder="输入 sellers ID 或店铺名"/);
  assert.match(pageSource, /list="competitor-seller-options"/);
  assert.match(pageSource, /matchesCompetitorSellerFilter\(item, competitorSellerQuery\.value\)/);
});
