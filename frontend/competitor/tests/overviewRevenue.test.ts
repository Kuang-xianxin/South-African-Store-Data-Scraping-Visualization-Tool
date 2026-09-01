import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  projectRevenueMonthTotal,
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
    projectedTotal: "预计8月总销售额",
  });
  assert.deepEqual(revenuePeriodLabels("2026-07-01", "2026-07-31"), {
    total: "7月总销售额",
    dailyAverage: "7月内日均销售额",
    projectedTotal: "预计7月总销售额",
  });
  assert.deepEqual(revenuePeriodLabels("2026-07-15", "2026-08-05"), {
    total: "所选区间总销售额",
    dailyAverage: "所选区间日均销售额",
    projectedTotal: "预计月总销售额",
  });
});

test("month projection multiplies the known-day average by calendar days", () => {
  assert.deepEqual(
    projectRevenueMonthTotal(600, "2026-08-01", "2026-08-20"),
    { projectedTotal: 18_600, monthDayCount: 31 },
  );
  assert.deepEqual(
    projectRevenueMonthTotal(600, "2028-02-01", "2028-02-18"),
    { projectedTotal: 17_400, monthDayCount: 29 },
  );
  assert.deepEqual(
    projectRevenueMonthTotal(null, "2026-04-01", "2026-04-12"),
    { projectedTotal: null, monthDayCount: 30 },
  );
});

test("month projection stays unavailable outside one natural-month viewport", () => {
  assert.deepEqual(
    projectRevenueMonthTotal(600, "2026-07-15", "2026-08-05"),
    { projectedTotal: null, monthDayCount: null },
  );
  assert.deepEqual(
    projectRevenueMonthTotal(600, "2026-13-01", "2026-13-05"),
    { projectedTotal: null, monthDayCount: null },
  );
});

test("overview renders period total and daily average for multi-store and single-store scopes", () => {
  assert.match(overviewSource, /currency\(multiStorePeriodRevenue\.total\)/);
  assert.match(overviewSource, /currency\(multiStorePeriodRevenue\.dailyAverage\)/);
  assert.match(overviewSource, /currency\(multiStoreRevenueProjection\.projectedTotal\)/);
  assert.match(overviewSource, /currency\(singleStorePeriodRevenue\.total\)/);
  assert.match(overviewSource, /currency\(singleStorePeriodRevenue\.dailyAverage\)/);
  assert.match(overviewSource, /currency\(singleStoreRevenueProjection\.projectedTotal\)/);
  assert.match(overviewSource, /月内日均 × \$\{projection\.monthDayCount\} 天/);
  assert.match(overviewSource, /仅在单个自然月视口计算/);
  assert.match(overviewSource, /缺失不补 0/);
});
