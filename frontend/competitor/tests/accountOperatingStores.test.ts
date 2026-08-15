import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
