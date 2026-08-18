import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  matchesProductName,
  matchesProductSearch,
  normalizeProductSearchText,
} from "../src/productSearch.ts";

test("normalizes product-name punctuation, accents, and spacing", () => {
  assert.equal(
    normalizeProductSearchText("  Café—Flood_Light  "),
    "cafe flood light",
  );
});

test("matches partial and unordered product-name terms", () => {
  const title = "Corduroy Lazy Sofa Chair - Foldable Home Seat";
  assert.equal(matchesProductName(title, "chair cord"), true);
  assert.equal(matchesProductName(title, "sof ch"), true);
  assert.equal(matchesProductName("Outdoor Floodlight", "flood light"), true);
});

test("matches unordered words and bounded typos without anagramming letters", () => {
  assert.equal(matchesProductName("Wireless Bluetooth Speaker", "speaker wireless"), true);
  assert.equal(matchesProductName("Wireless Bluetooth Speaker", "speaker wirless"), true);
  assert.equal(matchesProductName("Portable Charger", "portable chargr"), true);
  assert.equal(matchesProductName("Wireless Bluetooth Speaker", "rekaeps sseleriw"), false);
  assert.equal(matchesProductName("Portable Charger", "portable crhgra"), false);
  assert.equal(matchesProductName("Portable Charger", "garden table"), false);
});

test("keeps identifier matching as substring-only", () => {
  const fields = {
    productNames: ["Wireless Bluetooth Speaker"],
    otherValues: ["PLID12345678", "COMPANY-BLUE-01"],
  };
  assert.equal(matchesProductSearch(fields, "bluetooh"), true);
  assert.equal(matchesProductSearch(fields, "12345678"), true);
  assert.equal(matchesProductSearch(fields, "12345679"), false);
  assert.equal(matchesProductSearch(fields, "COMPANY-BLUE"), true);
  assert.equal(matchesProductSearch(fields, "COMPANI-BLUE"), false);
});

test("all product-name search pages use the shared fuzzy matcher", () => {
  const directMatcherPages = [
    "ProductsPage.vue",
    "KeywordTrafficPage.vue",
    "SearchRankingPage.vue",
    "QuadrantsPage.vue",
    "AnomalyProductsPage.vue",
  ];
  for (const page of directMatcherPages) {
    const source = readFileSync(new URL(`../src/pages/${page}`, import.meta.url), "utf8");
    assert.match(source, /matchesProductSearch/);
    assert.match(source, /模糊搜索/);
  }

  const competitorPage = readFileSync(
    new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
    "utf8",
  );
  assert.match(competitorPage, /matchesCompetitorProductSearchValues/);
  assert.ok((competitorPage.match(/模糊搜索/g) ?? []).length >= 3);

  const personalWorkspace = readFileSync(
    new URL("../src/personalWatchlistWorkspace.ts", import.meta.url),
    "utf8",
  );
  assert.match(personalWorkspace, /matchesCompetitorProductSearchValues/);

  const returnsPage = readFileSync(
    new URL("../src/pages/ReturnsPage.vue", import.meta.url),
    "utf8",
  );
  assert.match(returnsPage, /商品名称支持模糊搜索/);
});
