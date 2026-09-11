import assert from "node:assert/strict";
import test from "node:test";
import {
  aggregateOwnStoreSalesPoints,
  buildOwnStoreSalesChart,
  nearestOwnStoreSalesPointIndex,
  OWN_STORE_SALES_CHART,
} from "../src/ownStoreSalesChart.ts";
import type { OwnStoreSalesPoint } from "../src/types.ts";

test("phone geometry keeps every daily value and missing day, with legible ticks and correct tap mapping", () => {
  const source = Array.from({ length: 31 }, (_, index) => ({
    date: `2026-08-${String(index + 1).padStart(2, "0")}`,
    ordered_units: index === 12 ? null : index % 4,
    data_status: index === 12 ? "missing" : index === 30 ? "partial" : "verified",
    revision_count: 0,
  })) as OwnStoreSalesPoint[];
  const buckets = aggregateOwnStoreSalesPoints(source, "day").buckets;
  const desktop = buildOwnStoreSalesChart(buckets);
  for (const width of [240, 280, 320, 390]) {
    const layout = { ...OWN_STORE_SALES_CHART, width, plotRight: width - 20 };
    const mobile = buildOwnStoreSalesChart(buckets, layout);
    assert.deepEqual(mobile.points.map(p => [p.units, p.status, p.startDate]), desktop.points.map(p => [p.units, p.status, p.startDate]));
    assert.equal(mobile.points[12].barHeight, null);
    assert.ok(mobile.xTicks.length <= Math.floor((layout.plotRight - layout.plotLeft) / 70));
    assert.equal(mobile.xTicks[0].label, "08/01");
    assert.equal(mobile.xTicks.at(-1)?.label, "08/31");
    assert.ok(mobile.yTicks.every(tick => Number.isInteger(tick.value)));
    mobile.points.forEach((point, index) => {
      assert.ok(point.x >= layout.plotLeft && point.x <= layout.plotRight);
      assert.equal(nearestOwnStoreSalesPointIndex(point.x, width, buckets.length, layout), index);
    });
  }
});

test("zero and missing phone series remain distinct and do not manufacture sales", () => {
  const make = (units: number | null) => aggregateOwnStoreSalesPoints([
    { date: "2026-08-01", ordered_units: units, data_status: units === null ? "missing" : "verified" },
  ] as OwnStoreSalesPoint[], "day").buckets;
  const layout = { ...OWN_STORE_SALES_CHART, width: 320, plotRight: 300 };
  const zero = buildOwnStoreSalesChart(make(0), layout);
  const missing = buildOwnStoreSalesChart(make(null), layout);
  assert.deepEqual(zero.yTicks.map(tick => tick.value), [0]);
  assert.notEqual(zero.points[0].barHeight, null);
  assert.equal(missing.points[0].barHeight, null);
});
