import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const loginSource = readFileSync(
  new URL("../src/pages/LoginPage.vue", import.meta.url),
  "utf8",
);
const indexSource = readFileSync(new URL("../index.html", import.meta.url), "utf8");

const brandName = "昂古古科技有限公司ERP";
const previousBrandName = "昂古古科技有限公司ERP系统";
const legacyBrandPattern = /南非\s*(?:店铺\s*)?运营\s*ERP|南非\s*ERP/;

test("uses the company ERP name on the login page, app shell, and browser title", () => {
  assert.match(loginSource, new RegExp(`<strong>${brandName}</strong>`));
  assert.match(appSource, new RegExp(`<strong>${brandName}</strong>`));
  assert.match(indexSource, new RegExp(`<title>${brandName}</title>`));
  assert.match(indexSource, new RegExp(`content="${brandName}：`));

  for (const source of [loginSource, appSource, indexSource]) {
    assert.equal(source.includes(previousBrandName), false);
    assert.doesNotMatch(source, legacyBrandPattern);
  }
});
