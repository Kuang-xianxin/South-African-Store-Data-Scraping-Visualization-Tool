import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { defaultMultiStoreScope } from "../src/defaultStoreScope.ts";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");
const usersSource = readFileSync(
  new URL("../src/pages/UsersPage.vue", import.meta.url),
  "utf8",
);
const overviewSource = readFileSync(
  new URL("../src/pages/OverviewPage.vue", import.meta.url),
  "utf8",
);

test("top store selector offers the account operating-store merge", () => {
  assert.match(appSource, /const operatingStoresSelectorValue = "my-operating-stores"/);
  assert.match(
    appSource,
    /const operatingStoreIds = new Set\(\s+session\.value\?\.user\.assigned_store_ids \?\? \[\]/,
  );
  assert.match(appSource, /operatingConnectedStoreCount > 0/);
  assert.match(appSource, /operatingConnectedStoreCount > 1 \? "店合并" : "店"/);
  assert.match(appSource, /const scope: OwnStoreScope = value === operatingStoresSelectorValue\s+\? "operating"/);
});

test("modules with a visible multi-store range default to the widest account scope", () => {
  assert.equal(defaultMultiStoreScope(6, 0), "all");
  assert.equal(defaultMultiStoreScope(6, 4), "all");
  assert.equal(defaultMultiStoreScope(6, 6), "operating");
  assert.equal(defaultMultiStoreScope(2, 2), "operating");
  assert.equal(defaultMultiStoreScope(1, 1), "current");
  assert.equal(defaultMultiStoreScope(0, 0), "current");

  const acceptSessionSource = appSource.slice(
    appSource.indexOf("function acceptSession"),
    appSource.indexOf("function handleExpired"),
  );
  const switchPageSource = appSource.slice(
    appSource.indexOf("function switchPage"),
    appSource.indexOf("function initialPage"),
  );
  assert.match(
    acceptSessionSource,
    /applyDefaultStoreScopeForPage\(currentPage\.value\)/,
  );
  assert.match(
    switchPageSource,
    /applyDefaultStoreScopeForPage\(page\)\s*;\s*currentPage\.value = page/,
  );
});

test("all-store visibility keeps an independent multi-select operating assignment", () => {
  assert.match(usersSource, /运营店铺授权（可多选）/);
  assert.match(
    usersSource,
    /\{ all_stores: user\.all_stores, store_ids: next \}/,
  );
  assert.doesNotMatch(
    usersSource,
    /<div v-if="!user\.all_stores" class="store-checkbox-grid">/,
  );
  assert.match(
    usersSource,
    /开启全部查看后，未勾选店铺仍可单店查看但不进入该合并项/,
  );
});

test("multi-store APIs carry an explicit operating scope", () => {
  assert.match(typesSource, /OwnStoreScope = "current" \| "all" \| "operating"/);
  assert.match(apiSource, /store_scope: storeScope/);
  assert.match(apiSource, /params\.set\("store_scope", options\.storeScope\)/);
});

test("store responsibility follows explicit assignments for every account role", () => {
  assert.match(overviewSource, /admin: "管理员"/);
  assert.match(overviewSource, /暂未分配运营账号/);
  assert.doesNotMatch(overviewSource, /暂未分配非管理员运营/);
});
