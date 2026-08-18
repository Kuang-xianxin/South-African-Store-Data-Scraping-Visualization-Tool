import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("does not request the first product detail while its modal is closed", () => {
  assert.match(
    pageSource,
    /if \(!modalOpen\) \{\s+detailLoading\.value = false;/,
  );
  assert.ok(pageSource.indexOf("if (!modalOpen)") < pageSource.indexOf("fetchCompetitorDetail("));
});

test("reuses a bounded detail cache for repeated card opens", () => {
  assert.match(pageSource, /const competitorDetailCacheLimit = 24;/);
  assert.match(pageSource, /const cached = cachedCompetitorDetail\(cacheKey\);/);
  assert.match(pageSource, /cacheCompetitorDetail\(cacheKey, result\);/);
});

test("the shared product detail modal always exposes the persisted category path", () => {
  assert.match(pageSource, /class="competitor-category-path"/);
  assert.match(pageSource, /商品具体类目/);
  assert.match(pageSource, /selectedCategoryPathText/);
  assert.match(pageSource, /末级类目 ID/);
  assert.match(pageSource, /成功完成一次公开商品采集后自动补齐/);
});

test("anomaly detail loads its host and full local detail concurrently", () => {
  assert.match(
    pageSource,
    /const request = Promise\.all\(\[/,
  );
  assert.match(pageSource, /fetchOwnStoreCompetitors\([\s\S]*fetchCompetitorDetail\(/);
  assert.match(pageSource, /requestedOwnStoreDetailRequests\.get\(key\)/);
  assert.match(pageSource, /requestedOwnStoreDetailCacheTtlMs = 15_000/);
  assert.match(pageSource, /defineExpose\(\{ prefetchRequestedOwnStoreDetail \}\)/);
  assert.match(pageSource, /cacheCompetitorDetail\(detailCacheKey, prefetchedDetail\)/);
  assert.match(apiSource, /ownStoreScope: OwnStoreScope = "current",\s+signal\?: AbortSignal/);
  assert.match(apiSource, /\/api\/competitors\/\$\{plid\}\$\{suffix\}`?, \{ signal \}/);
});

test("large detail payloads stay shallow and paginate review nodes", () => {
  assert.match(pageSource, /const detail = shallowRef<CompetitorDetail>/);
  assert.match(pageSource, /const reviewPageSize = 20/);
  assert.match(pageSource, /filteredReviews\.value\.slice\(start, start \+ reviewPageSize\)/);
  assert.match(pageSource, /v-for="\(review, reviewIndex\) in visibleReviews"/);
  assert.match(pageSource, /class="compact-pagination detail-review-pagination"/);
});
