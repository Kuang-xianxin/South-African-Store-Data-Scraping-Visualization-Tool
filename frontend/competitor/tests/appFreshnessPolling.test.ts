import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("sidebar freshness polls while the page is visible and cleans up its lifecycle", () => {
  assert.match(appSource, /const freshnessPollIntervalMs = 15_000/);
  assert.match(
    appSource,
    /freshnessTimer = window\.setInterval\(\(\) => \{\s+if \(document\.visibilityState === "visible"\) void loadFreshness\(\);\s+\}, freshnessPollIntervalMs\)/,
  );
  assert.match(
    appSource,
    /document\.addEventListener\("visibilitychange", handleFreshnessVisibilityChange\)/,
  );
  assert.match(
    appSource,
    /document\.removeEventListener\("visibilitychange", handleFreshnessVisibilityChange\)/,
  );
  assert.match(
    appSource,
    /if \(freshnessTimer !== null\) window\.clearInterval\(freshnessTimer\)/,
  );
});

test("freshness updates immediately when the page becomes visible again", () => {
  assert.match(
    appSource,
    /function handleFreshnessVisibilityChange\(\) \{\s+if \(document\.visibilityState === "visible"\) void loadFreshness\(\);\s+\}/,
  );
});

test("a transient freshness request failure preserves the last known timestamps", () => {
  const loadFreshnessBlock = appSource.slice(
    appSource.indexOf("async function loadFreshness"),
    appSource.indexOf("async function loadRefreshStatus"),
  );
  assert.match(loadFreshnessBlock, /const requestRevision = \+\+freshnessRequestRevision/);
  assert.match(loadFreshnessBlock, /const nextFreshness = await fetchFreshness\(\)/);
  assert.match(
    loadFreshnessBlock,
    /if \(requestRevision === freshnessRequestRevision\) \{\s+freshness\.value = nextFreshness;\s+\}/,
  );
  assert.match(
    loadFreshnessBlock,
    /catch \{\s+\/\/ Keep the last known timestamps during a short local-service interruption\.\s+\}/,
  );
  assert.doesNotMatch(loadFreshnessBlock, /catch\(\(\) => \(\{/);
});

test("switching stores clears the previous store state and rejects stale responses", () => {
  const storeWatcherBlock = appSource.slice(
    appSource.indexOf("watch(\n  () => selectedStore.value?.code"),
    appSource.indexOf("const activePageProps"),
  );
  assert.match(
    storeWatcherBlock,
    /freshness\.value = \{\s+last_collection_at: null,\s+latest_metric_date: null,\s+\};\s+refreshKey\.value \+= 1;\s+void loadFreshness\(\)/,
  );
  assert.match(appSource, /let freshnessRequestRevision = 0/);
});
