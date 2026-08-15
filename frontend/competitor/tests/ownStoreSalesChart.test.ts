import assert from "node:assert/strict";
import test from "node:test";

import {
  buildOwnStoreSalesChart,
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

test("sales geometry plots verified zero and breaks the line across missing dates", () => {
  const chart = buildOwnStoreSalesChart(points);

  assert.equal(chart.yMaximum, 5);
  assert.equal(chart.points[1]?.y, 208);
  assert.equal(chart.points[2]?.y, null);
  assert.deepEqual(chart.segments, [
    "M 58 136 L 352 208",
    "M 940 28",
  ]);
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
