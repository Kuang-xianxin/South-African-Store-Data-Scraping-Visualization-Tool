import assert from "node:assert/strict";
import test from "node:test";
import { homeChartPath, homeCoverageLabel } from "../src/homeDashboard.ts";

test("zero is a real chart point; missing days split the line", () => {
  assert.equal(homeChartPath([{ x: 1, y: 200 }, { x: 2, y: null }, { x: 3, y: 40 }, { x: 4, y: 0 }]), "M1.00,200.00  M3.00,40.00 L4.00,0.00");
});
test("partial history exposes its store-day coverage", () => {
  assert.equal(homeCoverageLabel({ known_store_days: 4, expected_store_days: 6, complete_stores: 4, store_count: 6 }), "已核验 4/6 店日 · 部分数据");
});
