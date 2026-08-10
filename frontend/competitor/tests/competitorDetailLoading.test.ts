import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

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
