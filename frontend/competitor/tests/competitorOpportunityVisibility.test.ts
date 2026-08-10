import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const searchSource = readFileSync(
  new URL("../src/competitorSearch.ts", import.meta.url),
  "utf8",
);
const styleSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

test("removes the follow-selling opportunity selector, summary, and card badge", () => {
  for (const removedUiContract of [
    "followSellingOpportunityFilter",
    "followSellingOpportunitySummary",
    "follow-opportunity-summary",
    "follow-opportunity-badge",
    "可跟卖机会",
    "全部机会",
  ]) {
    assert.equal(pageSource.includes(removedUiContract), false, removedUiContract);
  }
  assert.equal(styleSource.includes("follow-opportunity"), false);
});

test("keeps API payload compatibility without matching hidden opportunity fields", () => {
  assert.match(typesSource, /跟卖机会\?: boolean/);
  assert.equal(searchSource.includes("跟卖机会"), false);
  assert.equal(
    existsSync(new URL("../src/competitorFollowOpportunities.ts", import.meta.url)),
    false,
  );
});
