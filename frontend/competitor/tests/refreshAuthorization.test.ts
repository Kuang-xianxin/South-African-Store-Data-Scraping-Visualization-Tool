import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("full-store refresh is exposed only to kxx with the refresh permission", () => {
  assert.match(
    appSource,
    /const canRefresh = computed\(\s*\(\) =>\s*hasPermission\("refresh\.run"\)\s*&& session\.value\?\.user\.username\.toLowerCase\(\) === "kxx"/,
  );

  const refreshButton = appSource.slice(
    appSource.indexOf('class="refresh-button"') - 400,
    appSource.indexOf('class="refresh-button"') + 400,
  );
  assert.match(refreshButton, /&& canRefresh/);
  assert.doesNotMatch(refreshButton, /!competitorMultiStoreSelected/);
  assert.doesNotMatch(refreshButton, /!overviewMultiStoreSelected/);
});

test("refresh button states the number of connected stores it will refresh", () => {
  assert.match(
    appSource,
    /accessible_stores\.filter\(\s*\(store\) => store\.active && store\.data_connected/,
  );
  assert.match(
    appSource,
    /`刷新全部数据（\$\{refreshTargetStoreCount\.value\}店）`/,
  );
});
