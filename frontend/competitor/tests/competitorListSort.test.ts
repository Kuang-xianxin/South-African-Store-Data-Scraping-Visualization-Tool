import assert from "node:assert/strict";
import test from "node:test";

import {
  competitorListSortMetricLabel,
  sortCompetitorItems,
} from "../src/competitorListSort.ts";
import type { CompetitorItem } from "../src/types.ts";

function item(
  plid: string,
  values: Partial<Pick<
    CompetitorItem,
    | "价格变化"
    | "库存净变化"
    | "库存净流出"
    | "周期销售件数"
    | "周期销售额"
    | "周期库存周转金额"
    | "新增评论"
    | "新增好评"
    | "新增差评"
    | "新增跟卖卖家数"
  >>,
): CompetitorItem {
  return {
    plid,
    价格变化: null,
    库存净变化: null,
    库存净流出: null,
    周期销售件数: null,
    周期销售额: null,
    周期库存周转金额: null,
    新增评论: null,
    新增好评: null,
    新增差评: null,
    新增跟卖卖家数: 0,
    ...values,
  } as CompetitorItem;
}

const items = [
  item("A", {
    价格变化: -5,
    库存净变化: -3,
    库存净流出: 3,
    周期销售件数: 3,
    周期销售额: 1200,
    周期库存周转金额: 1500,
    新增评论: 2,
    新增好评: 1,
    新增差评: 1,
    新增跟卖卖家数: 1,
  }),
  item("B", {
    价格变化: 8,
    库存净变化: 4,
    库存净流出: 0,
    周期销售件数: 8,
    周期销售额: 800,
    周期库存周转金额: 2400,
    新增评论: 7,
    新增好评: 5,
    新增差评: 2,
    新增跟卖卖家数: 3,
  }),
  item("C", { 价格变化: null, 库存净变化: null, 新增评论: null }),
];

test("selected price, stock, and review signals drive list direction", () => {
  assert.deepEqual(
    sortCompetitorItems(items, "降价", "asc").map((row) => row.plid),
    ["A", "B", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "补货", "desc").map((row) => row.plid),
    ["A", "B", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "库存减少", "desc").map((row) => row.plid),
    ["B", "A", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "评论增加", "desc").map((row) => row.plid),
    ["B", "A", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "差评增加", "asc").map((row) => row.plid),
    ["A", "B", "C"],
  );
});

test("stock movement signals disclose and use their dedicated interval metrics", () => {
  assert.equal(competitorListSortMetricLabel("补货"), "周期销售额");
  assert.equal(competitorListSortMetricLabel("库存减少"), "区间内售出件数");
  assert.equal(competitorListSortMetricLabel("库存减少且评论增加"), "周期销售额");
  assert.equal(competitorListSortMetricLabel("库存变化大"), "库存周转金额");
  assert.deepEqual(
    sortCompetitorItems(items, "库存减少且评论增加", "desc").map((row) => row.plid),
    ["A", "B", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "库存变化大", "desc").map((row) => row.plid),
    ["B", "A", "C"],
  );
});

test("new follower seller count is sortable and null metrics always stay last", () => {
  assert.deepEqual(
    sortCompetitorItems(items, "新增跟卖卖家", "desc").map((row) => row.plid),
    ["B", "A", "C"],
  );
  assert.deepEqual(
    sortCompetitorItems(items, "全部", "asc").map((row) => row.plid),
    ["A", "B", "C"],
  );
});
