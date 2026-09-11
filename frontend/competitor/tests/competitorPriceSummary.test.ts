import assert from "node:assert/strict";
import test from "node:test";
import { competitorPriceSummary } from "../src/competitorPriceSummary.ts";
import type { CompetitorItem } from "../src/types.ts";

function item(own: Array<number | null>, followers: Array<number | null>, price = 350): CompetitorItem {
  return {
    来源: "own_store", 价格: price,
    自有报价: own.map((价格, i) => ({ offer_id: String(i), 价格 })),
    跟卖报价: followers.map((价格) => ({ 报价来源: "public_offer", 价格 })),
  } as CompetitorItem;
}

test("all quotes include own prices while the own block keeps its separate range", () => {
  const result = competitorPriceSummary(item([350, 390], [499]));
  assert.match(result.all, /350.*499/);
  assert.match(result.own, /350.*390/);
  assert.equal(result.isOwn, true);
});

test("matching single quotes show one value rather than an artificial range", () => {
  const result = competitorPriceSummary(item([675, 675], [675], 675));
  assert.match(result.all, /675/);
  assert.ok(!result.all.includes("–"));
  assert.equal(result.all, result.own);
});

test("zero, negative and missing prices never lower either range", () => {
  const source = item([0, 520, null, -1], [0, -5, 600]);
  const before = JSON.stringify(source);
  const result = competitorPriceSummary(source);
  assert.match(result.all, /520.*600/);
  assert.match(result.own, /520/);
  assert.equal(JSON.stringify(source), before);
});

test("missing own prices are not filled with the follower or legacy summary price", () => {
  const result = competitorPriceSummary(item([null, 0], [499], 499));
  assert.equal(result.own, "—");
  assert.match(result.all, /499/);
  assert.equal(competitorPriceSummary(item([0, null], [0, null], 0)).all, "—");
});

test("only connected Seller API quotes populate the own block when compact rows are absent", () => {
  const source = item([], []);
  source.对比报价 = [
    { 报价来源: "seller_api", 价格: 420 },
    { 报价来源: "seller_api", 价格: 520 },
    { 报价来源: "public_offer", 价格: 1 },
  ] as CompetitorItem["对比报价"];
  const result = competitorPriceSummary(source);
  assert.match(result.own, /420.*520/);
  assert.equal(result.all, result.own);
});

test("public competitor ranges and the main quote remain separately identified", () => {
  const source = item([], [600, 499], 600);
  source.来源 = "competitor";
  const result = competitorPriceSummary(source);
  assert.equal(result.isOwn, false);
  assert.match(result.all, /499.*600/);
  assert.match(result.main, /600/);
  assert.equal(result.own, "—");
});

test("nonfinite values stay unknown and empty legacy cards use only their recorded scalar", () => {
  assert.equal(competitorPriceSummary(item([Infinity, NaN], [Infinity])).all, "—");
  const result = competitorPriceSummary(item([], [], 520));
  assert.match(result.own, /520/);
  assert.equal(result.all, result.own);
});
