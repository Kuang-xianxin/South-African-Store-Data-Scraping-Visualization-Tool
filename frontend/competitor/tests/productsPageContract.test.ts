import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/ProductsPage.vue", import.meta.url),
  "utf8",
);
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

test("product detail shows converted ZAR cost with rate evidence", () => {
  assert.match(pageSource, /单件成本（兰特）/);
  assert.match(pageSource, /detail\?\.cost_conversion\.cost_zar/);
  assert.match(pageSource, /1人民币=/);
  assert.match(pageSource, /汇率日/);
  assert.match(pageSource, /formatChinaDateTime\(detail\.cost_conversion\.fetched_at\)/);
  assert.match(pageSource, /最近成功于/);
  assert.match(pageSource, /detail\.cost_conversion\.message/);
});

test("product detail API type keeps converted, stale, missing, and unavailable states", () => {
  const conversionType = typesSource.slice(
    typesSource.indexOf("export interface ProductCostConversion"),
    typesSource.indexOf("export interface ProductDetailPayload"),
  );

  assert.match(conversionType, /cost_rmb: number \| null;/);
  assert.match(conversionType, /cost_zar: number \| null;/);
  assert.match(conversionType, /rate_date: string \| null;/);
  assert.match(
    conversionType,
    /status: "converted" \| "stale" \| "missing_cost" \| "unavailable";/,
  );
});

test("large product lists render one bounded page at a time", () => {
  assert.match(pageSource, /const productPageSize = ref\(50\)/);
  assert.match(pageSource, /const pagedProducts = computed/);
  assert.match(pageSource, /v-for="item in pagedProducts"/);
  assert.match(pageSource, /第 \{\{ productPage \}\} \/ \{\{ productPageCount \}\} 页/);
  assert.doesNotMatch(pageSource, /v-for="item in filtered"/);
});
