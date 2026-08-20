import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  aggregateOwnStoreSalesPoints,
  buildOwnStoreSalesChart,
  filterOwnStoreSalesPoints,
  getOwnStoreSalesDateBounds,
  getOwnStoreSalesRecentRange,
  nearestOwnStoreSalesPointIndex,
} from "../src/ownStoreSalesChart.ts";
import type { OwnStoreSalesPoint } from "../src/types.ts";

const points: OwnStoreSalesPoint[] = [
  {
    date: "2026-08-02",
    ordered_units: 2,
    data_status: "verified",
    revision_count: 0,
  },
  {
    date: "2026-08-03",
    ordered_units: 0,
    data_status: "verified",
    revision_count: 0,
  },
  {
    date: "2026-08-04",
    ordered_units: null,
    data_status: "missing",
    revision_count: 0,
  },
  {
    date: "2026-08-05",
    ordered_units: 5,
    data_status: "verified",
    revision_count: 1,
  },
];

test("sales geometry renders independent bars, keeps verified zero visible, and omits missing dates", () => {
  const chart = buildOwnStoreSalesChart(
    aggregateOwnStoreSalesPoints(points, "day").buckets,
  );

  assert.equal(chart.yMaximum, 5);
  assert.equal(chart.points[1]?.y, 188);
  assert.deepEqual(
    chart.points.map((point) => [point.barX, point.barY, point.barWidth, point.barHeight]),
    [
      [182.5, 123.2, 42, 64.8],
      [461.5, 184, 42, 4],
      [740.5, null, 42, null],
      [1019.5, 26, 42, 162],
    ],
  );
  assert.equal(chart.points[2]?.y, null);
  assert.equal("segments" in chart, false);
  assert.deepEqual(
    chart.xTicks.map((tick) => tick.label),
    ["08/02", "08/03", "08/04", "08/05"],
  );
  assert.deepEqual(
    chart.yTicks.map((tick) => tick.label),
    ["5", "4", "3", "2", "1", "0"],
  );
});

test("zero-only ranges use a single integer baseline instead of a fractional scale", () => {
  const chart = buildOwnStoreSalesChart(
    aggregateOwnStoreSalesPoints([
      {
        date: "2026-08-02",
        ordered_units: 0,
        data_status: "verified",
        revision_count: 0,
      },
      {
        date: "2026-08-03",
        ordered_units: 0,
        data_status: "partial",
        revision_count: 0,
      },
    ], "day").buckets,
  );

  assert.equal(chart.yMaximum, 1);
  assert.deepEqual(chart.yTicks.map((tick) => tick.label), ["0"]);
  assert.deepEqual(chart.points.map((point) => point.barHeight), [4, 4]);
  assert.ok(chart.points.every((point) => point.focusWidth > point.barWidth));
});

test("positive sales scales keep every visible y-axis label integral", () => {
  const chart = buildOwnStoreSalesChart(
    aggregateOwnStoreSalesPoints([
      {
        date: "2026-08-02",
        ordered_units: 9,
        data_status: "verified",
        revision_count: 0,
      },
    ], "day").buckets,
  );

  assert.equal(chart.yMaximum, 10);
  assert.deepEqual(
    chart.yTicks.map((tick) => tick.label),
    ["10", "8", "6", "4", "2", "0"],
  );
  assert.ok(chart.yTicks.every((tick) => Number.isInteger(tick.value)));
});

test("pointer selection stays inside the responsive plot bounds", () => {
  assert.equal(nearestOwnStoreSalesPointIndex(-10, 480, 4), 0);
  assert.equal(nearestOwnStoreSalesPointIndex(260, 480, 4), 2);
  assert.equal(nearestOwnStoreSalesPointIndex(600, 480, 4), 3);
});

test("chart geometry preserves a successful but unfinished Beijing day as partial", () => {
  const chart = buildOwnStoreSalesChart(
    aggregateOwnStoreSalesPoints([
      {
        date: "2026-08-06",
        ordered_units: 4,
        data_status: "partial",
        revision_count: 0,
      },
    ], "day").buckets,
  );

  assert.equal(chart.points[0]?.status, "partial");
  assert.equal(chart.points[0]?.units, 4);
});

test("sales component uses bars and contains no line-chart rendering contract", () => {
  const component = readFileSync(
    new URL("../src/components/OwnStoreSalesChart.vue", import.meta.url),
    "utf8",
  );

  assert.match(component, /class="own-sales-bar"/);
  assert.match(component, /条形图显示/);
  assert.match(component, /近30天/);
  assert.match(component, /近90天/);
  assert.match(component, /按周汇总/);
  assert.match(component, /aggregateOwnStoreSalesPoints/);
  assert.match(component, /下单件数（整数）/);
  assert.match(component, /已覆盖日期均为 0 件/);
  assert.match(component, /完整 0 件基线/);
  assert.match(component, /缺失，不补 0/);
  assert.match(component, /class="own-sales-active-band"/);
  assert.doesNotMatch(
    component,
    /geometry\.segments|own-sales-line|own-sales-point|折线显示|折线图/,
  );
});

test("date range filtering is inclusive and preserves missing-day evidence", () => {
  assert.deepEqual(getOwnStoreSalesDateBounds(points), {
    start: "2026-08-02",
    end: "2026-08-05",
  });

  const selected = filterOwnStoreSalesPoints(
    points,
    "2026-08-03",
    "2026-08-04",
  );
  assert.deepEqual(
    selected.map((point) => [point.date, point.ordered_units]),
    [
      ["2026-08-03", 0],
      ["2026-08-04", null],
    ],
  );
});

test("date range filtering normalizes reversed boundaries", () => {
  assert.deepEqual(
    filterOwnStoreSalesPoints(points, "2026-08-05", "2026-08-03").map(
      (point) => point.date,
    ),
    ["2026-08-03", "2026-08-04", "2026-08-05"],
  );
  assert.equal(getOwnStoreSalesDateBounds([]), null);
});

test("automatic aggregation keeps short ranges daily and condenses long ranges", () => {
  assert.equal(aggregateOwnStoreSalesPoints(makePoints(45)).granularity, "day");
  assert.equal(aggregateOwnStoreSalesPoints(makePoints(46)).granularity, "week");
  assert.equal(aggregateOwnStoreSalesPoints(makePoints(420)).granularity, "week");
  assert.equal(aggregateOwnStoreSalesPoints(makePoints(421)).granularity, "month");
});

test("weekly buckets sum only known values and retain coverage evidence", () => {
  const aggregation = aggregateOwnStoreSalesPoints([
    {
      date: "2026-08-03",
      ordered_units: 2,
      data_status: "verified",
      revision_count: 0,
    },
    {
      date: "2026-08-04",
      ordered_units: 0,
      data_status: "verified",
      revision_count: 1,
    },
    {
      date: "2026-08-05",
      ordered_units: null,
      data_status: "missing",
      revision_count: 0,
    },
    {
      date: "2026-08-06",
      ordered_units: 4,
      data_status: "partial",
      revision_count: 2,
    },
  ], "week");

  assert.equal(aggregation.granularity, "week");
  assert.deepEqual(aggregation.buckets[0], {
    endDate: "2026-08-06",
    granularity: "week",
    missingDays: 1,
    partialDays: 1,
    revisionCount: 3,
    salesDays: 2,
    startDate: "2026-08-03",
    status: "partial",
    totalDays: 4,
    units: 6,
    verifiedDays: 2,
  });

  const missing = aggregateOwnStoreSalesPoints([
    {
      date: "2026-08-10",
      ordered_units: null,
      data_status: "missing",
      revision_count: 0,
    },
    {
      date: "2026-08-11",
      ordered_units: null,
      data_status: "missing",
      revision_count: 0,
    },
  ], "week").buckets[0];
  assert.equal(missing?.units, null);
  assert.equal(missing?.status, "missing");
});

test("recent range shortcuts are inclusive and clamp to the available start", () => {
  const bounds = { start: "2026-01-01", end: "2026-08-18" };
  assert.deepEqual(getOwnStoreSalesRecentRange(bounds, 30), {
    start: "2026-07-20",
    end: "2026-08-18",
  });
  assert.deepEqual(getOwnStoreSalesRecentRange(bounds, 90), {
    start: "2026-05-21",
    end: "2026-08-18",
  });
  assert.deepEqual(
    getOwnStoreSalesRecentRange({ start: "2026-08-01", end: "2026-08-18" }, 30),
    { start: "2026-08-01", end: "2026-08-18" },
  );
});

function makePoints(count: number): OwnStoreSalesPoint[] {
  const start = new Date("2025-01-01T00:00:00Z");
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start);
    date.setUTCDate(date.getUTCDate() + index);
    return {
      date: date.toISOString().slice(0, 10),
      ordered_units: index % 5,
      data_status: "verified" as const,
      revision_count: 0,
    };
  });
}
