import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  competitorDetailPageHref,
  competitorDetailPlidFromHash,
  ERP_MODULE_KEYS,
  modulePageFromHash,
  modulePageHref,
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
  assert.equal(modulePageFromHash("#section=products"), null);
  assert.equal(modulePageFromHash(""), null);
});

test("competitor own-link detail hashes preserve the PLID for a new tab", () => {
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
  assert.match(appSource, /const linkedPage = modulePageFromHash\(window\.location\.hash\)/);
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
});
