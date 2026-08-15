import assert from "node:assert/strict";
import test from "node:test";

import {
  COMPETITOR_OPERATING_SIGNAL_OPTIONS,
  competitorOperatingSignals,
  matchesCompetitorOperatingSignal,
  offerOperatingSignals,
} from "../src/competitorOperatingSignals.ts";
import type { CompetitorItem, CompetitorOfferItem } from "../src/types.ts";

function offer(priceSignal: string, stockSignal: string): CompetitorOfferItem {
  return { 价格信号: priceSignal, 库存信号: stockSignal } as CompetitorOfferItem;
}

function item(overrides: Partial<CompetitorItem> = {}): CompetitorItem {
  return {
    趋势判断: "库存不可比，评论无新增",
    价格信号: "待建立价格基线",
    库存净变化: null,
    库存净流出: null,
    周期销售件数: null,
    周期补货量: null,
    周期库存周转金额: null,
    库存可比: false,
    新增评论: null,
    新增好评: null,
    新增差评: null,
    新增跟卖卖家数: 0,
    跟卖报价: [],
    ...overrides,
  } as CompetitorItem;
}

test("operating signal options contain only the confirmed categories", () => {
  assert.deepEqual([...COMPETITOR_OPERATING_SIGNAL_OPTIONS], [
    "降价",
    "涨价",
    "价格不变",
    "补货",
    "库存减少",
    "库存数量不变",
    "库存变化大",
    "评论增加",
    "好评增加",
    "差评增加",
    "库存减少且评论增加",
    "新增跟卖卖家",
  ]);
});

test("baseline, incomparable, and legacy trend states are excluded", () => {
  const excluded = item({
    趋势判断: "两个独立正向信号",
    跟卖报价: [offer("待建立报价基线", "库存不可比")],
  });

  assert.deepEqual(competitorOperatingSignals(excluded), []);
  assert.deepEqual(offerOperatingSignals(excluded.跟卖报价[0]!), []);
  assert.equal(matchesCompetitorOperatingSignal(excluded, "全部"), true);
  assert.equal(matchesCompetitorOperatingSignal(excluded, "补货"), false);
});

test("replenishment aliases exclude the contradictory product-level unchanged signal", () => {
  const retained = item({
    趋势判断: "检测到补货",
    价格信号: "降价",
    库存净变化: 0,
    库存可比: true,
    跟卖报价: [
      offer("涨价", "恢复有货"),
      offer("价格不变", "库存数量不变"),
    ],
  });

  assert.deepEqual(competitorOperatingSignals(retained), [
    "降价",
    "涨价",
    "价格不变",
    "补货",
  ]);
  assert.deepEqual(offerOperatingSignals(retained.跟卖报价[0]!), ["涨价", "补货"]);
  assert.deepEqual(offerOperatingSignals(retained.跟卖报价[1]!), ["价格不变", "库存数量不变"]);
  assert.equal(matchesCompetitorOperatingSignal(retained, "库存数量不变"), false);
});

test("only a complete period with zero sales and zero replenishment is unchanged", () => {
  const unchanged = item({
    库存净变化: 0,
    周期销售件数: 0,
    周期补货量: 0,
    库存可比: true,
    跟卖报价: [offer("价格不变", "库存数量不变")],
  });
  const replenishedOnly = item({
    库存净变化: 3,
    周期销售件数: 0,
    周期补货量: 3,
    库存可比: true,
    跟卖报价: [offer("价格不变", "库存数量不变")],
  });
  const replenishedAfterSales = item({
    库存净变化: 0,
    周期销售件数: 5,
    周期补货量: 5,
    库存可比: true,
    跟卖报价: [offer("价格不变", "库存数量不变")],
  });

  assert.equal(matchesCompetitorOperatingSignal(unchanged, "库存数量不变"), true);
  assert.equal(matchesCompetitorOperatingSignal(unchanged, "补货"), false);
  assert.equal(matchesCompetitorOperatingSignal(unchanged, "库存减少"), false);
  assert.equal(matchesCompetitorOperatingSignal(replenishedOnly, "补货"), true);
  assert.equal(matchesCompetitorOperatingSignal(replenishedOnly, "库存数量不变"), false);
  assert.equal(matchesCompetitorOperatingSignal(replenishedAfterSales, "补货"), true);
  assert.equal(matchesCompetitorOperatingSignal(replenishedAfterSales, "库存减少"), true);
  assert.equal(matchesCompetitorOperatingSignal(replenishedAfterSales, "库存数量不变"), false);
});

test("legacy unchanged data is retained only when no movement signal exists", () => {
  const legacyUnchanged = item({
    库存净变化: 0,
    库存可比: true,
    跟卖报价: [offer("价格不变", "库存数量不变")],
  });

  assert.equal(matchesCompetitorOperatingSignal(legacyUnchanged, "库存数量不变"), true);
});

test("stock and PLID review increases produce standalone and combined signals", () => {
  const changed = item({
    价格信号: "价格不变",
    库存净流出: 3,
    周期库存周转金额: 2300,
    新增评论: 4,
    新增好评: 3,
    新增差评: 1,
    新增跟卖卖家数: 2,
  });

  assert.deepEqual(competitorOperatingSignals(changed), [
    "价格不变",
    "库存减少",
    "库存变化大",
    "评论增加",
    "好评增加",
    "差评增加",
    "库存减少且评论增加",
    "新增跟卖卖家",
  ]);
  assert.equal(matchesCompetitorOperatingSignal(changed, "好评增加"), true);
  assert.equal(matchesCompetitorOperatingSignal(changed, "库存减少且评论增加"), true);
  assert.equal(matchesCompetitorOperatingSignal(changed, "库存变化大"), true);
  assert.equal(matchesCompetitorOperatingSignal(changed, "新增跟卖卖家"), true);
});

test("period sales units keep replenished products in the stock-decrease signal", () => {
  const replenishedAfterSales = item({
    趋势判断: "检测到补货",
    库存净变化: 23,
    库存净流出: 0,
    周期销售件数: 50,
    跟卖报价: [offer("价格不变", "待建立库存基线")],
  });
  const noObservedSales = item({
    库存净变化: -4,
    库存净流出: 4,
    周期销售件数: 0,
    跟卖报价: [offer("价格不变", "库存减少")],
  });

  assert.equal(matchesCompetitorOperatingSignal(replenishedAfterSales, "库存减少"), true);
  assert.equal(matchesCompetitorOperatingSignal(noObservedSales, "库存减少"), false);
});
