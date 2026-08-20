import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  revenuePeriodLabels,
  summarizeRevenuePeriod,
} from "../src/overviewRevenue.ts";

const overviewSource = readFileSync(
  new URL("../src/pages/OverviewPage.vue", import.meta.url),
  "utf8",
);

test("period revenue sums only known days and averages over the same evidence", () => {
  assert.deepEqual(
    summarizeRevenuePeriod([
      { amount: 1200 },
      { amount: null },
      { amount: 0 },
      { amount: 600, partial: true, pending: true },
    ]),
    {
      total: 1800,
      dailyAverage: 600,
      knownDayCount: 3,
      missingDayCount: 1,
      partialDayCount: 1,
      pendingDayCount: 1,
    },
  );
});

test("period revenue remains unavailable when every day lacks an amount", () => {
  assert.deepEqual(
    summarizeRevenuePeriod([{ amount: null }, { amount: undefined }]),
    {
      total: null,
      dailyAverage: null,
      knownDayCount: 0,
      missingDayCount: 2,
      partialDayCount: 0,
      pendingDayCount: 0,
    },
  );
});

test("natural month viewports use month labels and custom ranges stay truthful", () => {
  assert.deepEqual(revenuePeriodLabels("2026-08-01", "2026-08-20"), {
    total: "8月总销售额",
    dailyAverage: "8月内日均销售额",
  });
  assert.deepEqual(revenuePeriodLabels("2026-07-01", "2026-07-31"), {
    total: "7月总销售额",
    dailyAverage: "7月内日均销售额",
  });
  assert.deepEqual(revenuePeriodLabels("2026-07-15", "2026-08-05"), {
    total: "所选区间总销售额",
    dailyAverage: "所选区间日均销售额",
  });
});

test("overview renders period total and daily average for multi-store and single-store scopes", () => {
  assert.match(overviewSource, /currency\(multiStorePeriodRevenue\.total\)/);
  assert.match(overviewSource, /currency\(multiStorePeriodRevenue\.dailyAverage\)/);
  assert.match(overviewSource, /currency\(singleStorePeriodRevenue\.total\)/);
  assert.match(overviewSource, /currency\(singleStorePeriodRevenue\.dailyAverage\)/);
  assert.match(overviewSource, /缺失不补 0/);
});
