import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const competitorSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

test("business modules are split into on-demand chunks", () => {
  assert.match(appSource, /defineAsyncComponent\(\{/);
  for (const page of [
    "OverviewPage",
    "ProductsPage",
    "KeywordTrafficPage",
    "SearchRankingPage",
    "AnomalyProductsPage",
    "ReturnsPage",
    "QuadrantsPage",
    "CompetitorsPage",
    "LogisticsPage",
    "UsersPage",
  ]) {
    assert.match(appSource, new RegExp(`lazyPage\\(\\(\\) => import\\("\\./pages/${page}\\.vue"\\)\\)`));
    assert.doesNotMatch(appSource, new RegExp(`import ${page} from`));
  }
});

test("batch polling stays lightweight until a bounded detail page is opened", () => {
  assert.match(apiSource, /query\.set\("include_details", "true"\)/);
  assert.match(apiSource, /query\.set\("page_size", String\(detail\.pageSize\)\)/);
  assert.match(
    competitorSource,
    /async function loadSharedBatchStatus\(\s*includeDetails = collectionDetailsOpen\.value/,
  );
  assert.match(
    competitorSource,
    /<div v-if="collectionDetailsOpen" class="collection-task-detail-groups">/,
  );
  assert.match(competitorSource, /v-for="result in visibleDisplayedCollectionResults"/);
  assert.match(competitorSource, /v-for="error in visibleDisplayedCollectionErrors"/);
  assert.match(competitorSource, /const collectionDetailPageSize = 50/);
});

test("all-store target refresh reuses the selected all-store request", () => {
  assert.match(
    competitorSource,
    /const allStoreTargetPayloadRequest = requestScope === "all"\s*\? storeTargetPayloadRequest/,
  );
});
