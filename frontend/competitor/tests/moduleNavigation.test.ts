import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  competitorDetailPageHref,
  competitorDetailPlidFromHash,
  ERP_MODULE_KEYS,
  modulePageFromHash,
  modulePageHref,
  openOwnStoreDetailTab,
  ownStoreDetailPageHref,
  ownStoreDetailRequestFromHash,
  shouldHandleModulePageClick,
} from "../src/moduleNavigation.ts";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("every ERP module has a stable hash link that restores the same module", () => {
  assert.equal(ERP_MODULE_KEYS.length, 10);
  for (const moduleKey of ERP_MODULE_KEYS) {
    const href = modulePageHref(moduleKey);
    assert.equal(modulePageFromHash(href), moduleKey);
  }
  assert.equal(modulePageFromHash("#module=unknown"), null);
  assert.equal(modulePageFromHash("#module=daily-report"), null);
  assert.equal(modulePageFromHash("#module=platform-warehouse"), null);
  assert.equal(modulePageFromHash("#section=products"), null);
  assert.equal(modulePageFromHash(""), null);
});

test("legacy competitor detail hashes preserve the PLID", () => {
  const href = competitorDetailPageHref(" 12345678 ");
  assert.equal(modulePageFromHash(href), "competitors");
  assert.equal(competitorDetailPlidFromHash(href), "12345678");
  assert.equal(competitorDetailPlidFromHash("#module=competitors"), null);
  assert.equal(
    competitorDetailPlidFromHash("#module=anomaly-products&detail_plid=123"),
    null,
  );
  assert.equal(
    competitorDetailPlidFromHash("#module=competitors&detail_plid=not-a-plid"),
    null,
  );
});

test("standalone own-link detail hashes preserve scope, store and date evidence", () => {
  const href = ownStoreDetailPageHref({
    plid: " 12345678 ",
    scope: "current",
    storeCode: "store.za-1",
    startDate: "2026-07-01",
    endDate: "2026-08-17",
  });
  assert.equal(modulePageFromHash(href), "competitors");
  assert.deepEqual(ownStoreDetailRequestFromHash(href), {
    plid: "12345678",
    scope: "current",
    storeCode: "store.za-1",
    startDate: "2026-07-01",
    endDate: "2026-08-17",
  });
  assert.deepEqual(
    ownStoreDetailRequestFromHash(
      "#module=competitors&own_detail_plid=88&own_store_scope=all&store_code=ignored",
    ),
    { plid: "88", scope: "all" },
  );
  assert.equal(
    ownStoreDetailRequestFromHash(
      "#module=competitors&own_detail_plid=88&own_store_scope=current&start_date=2026-08-18&end_date=2026-08-17",
    ),
    null,
  );
  assert.equal(
    ownStoreDetailRequestFromHash(
      "#module=competitors&own_detail_plid=not-a-plid&own_store_scope=current",
    ),
    null,
  );
});

test("own-link detail requests a normal browser tab without popup features", () => {
  const originalWindow = globalThis.window;
  const opened = { opener: {} };
  let call: [string, string] | null = null;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { pathname: "/erp", search: "?mode=local" },
      open: (href: string, target: string) => {
        call = [href, target];
        return opened;
      },
    },
  });
  try {
    assert.equal(
      openOwnStoreDetailTab({ plid: "123", scope: "operating" }),
      opened,
    );
    assert.deepEqual(call, [
      "/erp?mode=local#module=competitors&own_detail_plid=123&own_store_scope=operating",
      "_blank",
    ]);
    assert.equal(opened.opener, null);
  } finally {
    if (originalWindow === undefined) {
      delete (globalThis as { window?: Window }).window;
    } else {
      Object.defineProperty(globalThis, "window", {
        configurable: true,
        value: originalWindow,
      });
    }
  }
});

test("only an unmodified primary click stays inside the current SPA tab", () => {
  const primaryClick = {
    altKey: false,
    button: 0,
    ctrlKey: false,
    defaultPrevented: false,
    metaKey: false,
    shiftKey: false,
  };
  assert.equal(shouldHandleModulePageClick(primaryClick), true);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, button: 1 }), false);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, ctrlKey: true }), false);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, metaKey: true }), false);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, shiftKey: true }), false);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, altKey: true }), false);
  assert.equal(shouldHandleModulePageClick({ ...primaryClick, defaultPrevented: true }), false);
});

test("the sidebar exposes real links while retaining guarded left-click navigation", () => {
  assert.match(appSource, /<a\s+v-for="page in allPages"/);
  assert.match(appSource, /:href="modulePageHref\(page\.key\)"/);
  assert.match(appSource, /@click="openPage\(\$event, page\)"/);
  assert.match(appSource, /event\.preventDefault\(\);\s+showPermissionDenied\(\)/);
  assert.match(appSource, /window\.addEventListener\("hashchange", handleModuleHashChange\)/);
  assert.match(
    appSource,
    /function switchPage[\s\S]*window\.scrollTo\(\{ top: 0, left: 0, behavior: "auto" \}\)/,
  );
  assert.match(appSource, /const linkedPage = modulePageFromHash\(window\.location\.hash\)/);
  assert.match(appSource, /ownStoreDetailRequestFromHash\(window\.location\.hash\)/);
  assert.match(appSource, /class="standalone-own-detail-shell"/);
  assert.match(appSource, /:requested-detail-start-date="standaloneOwnStoreDetailRequest\.startDate"/);
  const initialPageSource = appSource.slice(
    appSource.indexOf("function initialPage"),
    appSource.indexOf("function applyDataViewport"),
  );
  assert.ok(
    initialPageSource.indexOf("const linkedPage")
      < initialPageSource.indexOf("const currentClientId"),
    "an explicit module link must win over a copied competitor tab checkpoint",
  );
  assert.doesNotMatch(appSource, /<button\s+v-for="page in allPages"/);
  assert.doesNotMatch(appSource, /DailyReportPage|daily-report|运营日报/);
  assert.doesNotMatch(appSource, /PlatformWarehousePage|platform-warehouse|约平台仓/);
});

test("ordinary SPA navigation keeps every ERP module instance cached", () => {
  const componentMapSource = appSource.slice(
    appSource.indexOf("const pageComponent = computed"),
    appSource.indexOf("const pageComponentKey = computed"),
  );
  const expectedComponents = new Map([
    ["overview", "OverviewPage"],
    ["products", "ProductsPage"],
    ["keyword-traffic", "KeywordTrafficPage"],
    ["search-ranking", "SearchRankingPage"],
    ["quadrants", "QuadrantsPage"],
    ["anomaly-products", "AnomalyProductsPage"],
    ["returns", "ReturnsPage"],
    ["logistics", "LogisticsPage"],
    ["competitors", "CompetitorsPage"],
    ["users", "UsersPage"],
  ]);

  assert.deepEqual([...expectedComponents.keys()], [...ERP_MODULE_KEYS]);
  for (const [moduleKey, componentName] of expectedComponents) {
    const keyPattern = moduleKey.includes("-") ? `"${moduleKey}"` : moduleKey;
    assert.match(
      componentMapSource,
      new RegExp(`${keyPattern}: ${componentName}`),
      `${moduleKey} must remain part of the cached dynamic component map`,
    );
  }

  assert.match(
    appSource,
    /<KeepAlive v-else :max="ERP_MODULE_KEYS\.length">[\s\S]*?:is="pageComponent"[\s\S]*?:key="pageComponentKey"[\s\S]*?<\/KeepAlive>/,
  );
  assert.doesNotMatch(appSource, /<KeepAlive[^>]*\binclude=/);
});
