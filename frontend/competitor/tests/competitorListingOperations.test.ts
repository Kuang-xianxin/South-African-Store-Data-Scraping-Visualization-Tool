import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(
  new URL("../src/api.ts", import.meta.url),
  "utf8",
);
const typeSource = readFileSync(
  new URL("../src/types.ts", import.meta.url),
  "utf8",
);

test("seller and category confirmations expose admin-only collapsible operation records", () => {
  assert.match(pageSource, /v-if="props\.isAdmin" class="competitor-listing-operations"/);
  assert.match(pageSource, /确认加入才留痕；展开具体记录后分页读取本次全部商品链接/);
  assert.match(pageSource, /@toggle="handleListingOperationToggle\(\$event, operation\.id\)"/);
  assert.match(pageSource, /\{\{ item\.url \}\}/);
  assert.match(pageSource, /listingOperationResultLabel\(item\.result\)/);
  assert.match(pageSource, /operation\.personal_library_name \|\| "升级前未记录"/);
});

test("operation headers and product links use separate paginated read APIs", () => {
  assert.match(apiSource, /\/api\/competitors\/listing-operations\?\$\{query\.toString\(\)\}/);
  assert.match(
    apiSource,
    /\/api\/competitors\/listing-operations\/\$\{encodeURIComponent\(operationId\)\}\/items/,
  );
  assert.match(typeSource, /reactivated_target/);
  assert.match(typeSource, /sort_ranks: Record<string, number>/);
  assert.match(typeSource, /personal_library_name: string \| null/);
  assert.match(typeSource, /balanced_rank_fusion_then_plid_deduplicate/);
});
