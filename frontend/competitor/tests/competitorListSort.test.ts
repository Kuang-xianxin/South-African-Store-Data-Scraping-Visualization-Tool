import assert from "node:assert/strict";
import test from "node:test";

import {
  competitorListSortMetricLabel,
  competitorListSortOptions,
  effectiveCompetitorSortMetric,
  sortCompetitorItems,
  type CompetitorListSortMetric,
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

for (const window of ["7", "15", "30", "60", "90", "total"] as const) {
  for (const source of ["competitor", "own_store", "followers"] as const) {
    test(`${source} ${window}: sorts the displayed sales globally in both directions, keeping missing last`, () => {
      const field = source === "competitor" ? "近期观察售出"
        : source === "own_store" ? "自有官方销量" : "跟卖近期观察售出";
      const metric: CompetitorListSortMetric = `${source === "followers" ? "follower_sales" : "sales"}_${window}`;
      const rows = [10, null, 2, 100, 0, undefined, NaN].map((value, i) => ({
        ...item(String(i), { 周期销售件数: 1000 - i }),
        来源: source === "competitor" ? "competitor" : "own_store",
        自有官方销量: { [window]: 9000 - i },
        跟卖近期观察售出: { [window]: 5000 - i },
        近期观察售出: { [window]: 3000 - i },
        [field]: { [window]: value },
      }) as CompetitorItem);
      const original = [...rows];
      for (const signal of ["全部", "库存减少"] as const) {
        assert.deepEqual(sortCompetitorItems(rows, signal, "asc", metric).map(row => row.plid),
          ["4", "2", "0", "3", "1", "5", "6"]);
        assert.deepEqual(sortCompetitorItems(rows, signal, "desc", metric).map(row => row.plid),
          ["3", "0", "2", "4", "1", "5", "6"]);
      }
      assert.deepEqual(rows, original);
    });
  }
}

test("sales options cover every window and preserve the period when switching product source", () => {
  assert.equal(competitorListSortOptions(false, "全部").length, 7);
  assert.equal(competitorListSortOptions(true, "库存减少").length, 13);
  assert.equal(effectiveCompetitorSortMetric("follower_sales_total", false), "sales_total");
  assert.equal(effectiveCompetitorSortMetric("follower_sales_7", true), "follower_sales_7");
});
