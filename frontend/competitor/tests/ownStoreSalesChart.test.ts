import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildOwnStoreSalesChart,
  filterOwnStoreSalesPoints,
  getOwnStoreSalesDateBounds,
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
  const chart = buildOwnStoreSalesChart(points);

  assert.equal(chart.yMaximum, 5);
  assert.equal(chart.points[1]?.y, 208);
  assert.deepEqual(
    chart.points.map((point) => [point.barX, point.barY, point.barWidth, point.barHeight]),
    [
      [159.25, 136, 18, 72],
      [379.75, 206, 18, 2],
      [600.25, null, 18, null],
      [820.75, 28, 18, 180],
    ],
  );
  assert.equal(chart.points[2]?.y, null);
  assert.equal("segments" in chart, false);
  assert.deepEqual(
    chart.xTicks.map((tick) => tick.label),
    ["08/02", "08/03", "08/05"],
  );
});

test("pointer selection stays inside the responsive plot bounds", () => {
  assert.equal(nearestOwnStoreSalesPointIndex(-10, 480, 4), 0);
  assert.equal(nearestOwnStoreSalesPointIndex(260, 480, 4), 2);
  assert.equal(nearestOwnStoreSalesPointIndex(600, 480, 4), 3);
});

test("chart geometry preserves a successful but unfinished Beijing day as partial", () => {
  const chart = buildOwnStoreSalesChart([
    {
      date: "2026-08-06",
      ordered_units: 4,
      data_status: "partial",
      revision_count: 0,
    },
  ]);

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
