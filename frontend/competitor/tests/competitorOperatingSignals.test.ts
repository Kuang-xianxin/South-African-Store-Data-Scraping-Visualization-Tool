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
    库存可比: false,
    新增评论: null,
    新增好评: null,
    新增差评: null,
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
    "评论增加",
    "好评增加",
    "差评增加",
    "库存减少且评论增加",
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

test("price signals, replenishment aliases, and exact unchanged stock are retained", () => {
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
    "库存数量不变",
  ]);
  assert.deepEqual(offerOperatingSignals(retained.跟卖报价[0]!), ["涨价", "补货"]);
  assert.equal(matchesCompetitorOperatingSignal(retained, "库存数量不变"), true);
});

test("stock and PLID review increases produce standalone and combined signals", () => {
  const changed = item({
    价格信号: "价格不变",
    库存净流出: 3,
    新增评论: 4,
    新增好评: 3,
    新增差评: 1,
  });

  assert.deepEqual(competitorOperatingSignals(changed), [
    "价格不变",
    "库存减少",
    "评论增加",
    "好评增加",
    "差评增加",
    "库存减少且评论增加",
  ]);
  assert.equal(matchesCompetitorOperatingSignal(changed, "好评增加"), true);
  assert.equal(matchesCompetitorOperatingSignal(changed, "库存减少且评论增加"), true);
});
