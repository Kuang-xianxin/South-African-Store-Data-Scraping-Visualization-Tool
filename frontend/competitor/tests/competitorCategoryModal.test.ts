import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  competitorCategoryIdentity,
  competitorItemMatchesCategory,
  mergeCompetitorCategoryCatalog,
  type CompetitorCategoryCatalogItem,
} from "../src/competitorCategoryMatches.ts";
import type { CompetitorCategoryBreadcrumb } from "../src/types.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const radarCardSource = readFileSync(new URL("../src/components/CompetitorRadarCard.vue", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

function category(
  name: string,
  id: string | null = null,
  slug: string | null = null,
  type: string | null = "department",
): CompetitorCategoryBreadcrumb {
  return { name, id, slug, type };
}

function item(
  plid: string,
  source: CompetitorCategoryCatalogItem["来源"],
  path: CompetitorCategoryBreadcrumb[],
): CompetitorCategoryCatalogItem {
  return { plid, 来源: source, 商品: `Product ${plid}`, 类目路径: path };
}

test("matches every breadcrumb level by persisted category identity", () => {
  const broad = category("Office & Stationery", "100");
  const leaf = category("Screens", "104", "screens", "productline");
  const product = item("A", "competitor", [broad, category("Office", "101"), leaf]);

  assert.equal(competitorItemMatchesCategory(product, broad), true);
  assert.equal(competitorItemMatchesCategory(product, leaf), true);
  assert.equal(competitorItemMatchesCategory(product, category("Screens", "999")), false);
});

test("falls back from missing ids to normalized slug then type and name", () => {
  assert.equal(
    competitorCategoryIdentity(category("Screens", null, " Screens ", "productline")),
    "slug:screens",
  );
  assert.equal(
    competitorCategoryIdentity(category(" Screens ", null, null, " ProductLine ")),
    "type-name:productline:screens",
  );
});

test("deduplicates PLIDs, lets own-store evidence win, and lists own links first", () => {
  const selected = category("Screens", "104");
  const catalog = mergeCompetitorCategoryCatalog(
    [item("A", "competitor", [selected]), item("B", "competitor", [selected])],
    [item("A", "own_store", [selected]), item("C", "own_store", [selected])],
  );

  assert.deepEqual(catalog.map((entry) => `${entry.来源}:${entry.plid}`), [
    "own_store:A",
    "own_store:C",
    "competitor:B",
  ]);
});

test("all four radar category hierarchies expose buttons and the catalog uses all-store data", () => {
  assert.equal((pageSource + radarCardSource).match(/class="competitor-category-node-button"/g)?.length, 4);
  assert.equal(pageSource.match(/@click\.stop="openCategoryModal\(category, \$event\)"/g)?.length, 3);
  assert.match(pageSource, /class="competitor-modal competitor-category-modal"/);
  assert.match(pageSource, /fetchOwnStoreCompetitors\([\s\S]*?"all"/);
  assert.match(pageSource, /openCategoryProductDetail\(item\)/);
  assert.match(pageSource, /自有链接/);
  assert.match(stylesSource, /\.competitor-category-product-card\.is-own-store/);
  assert.match(stylesSource, /\.competitor-category-source-badge\.is-own-store/);
});

test("category directory cards expose the radar card operating details", () => {
  const cardStart = pageSource.indexOf(
    'class="competitor-status-card competitor-category-product-card"',
  );
  const cardEnd = pageSource.indexOf("</article>", cardStart);
  const cardSource = pageSource.slice(cardStart, cardEnd);

  assert.ok(cardStart >= 0 && cardEnd > cardStart);
  assert.match(cardSource, /class="competitor-status-header"/);
  assert.match(cardSource, /class="competitor-status-summary"/);
  assert.match(cardSource, /categoryItemOfferSummary\(item\)/);
  assert.match(cardSource, /competitor-first-monitored-badge/);
  assert.match(cardSource, /competitorOfferPriceRange\(item\)/);
  assert.match(cardSource, /item\.库存上限/);
  assert.match(cardSource, /item\.周期销售额/);
  assert.match(cardSource, /class="competitor-card-category"/);
  assert.match(cardSource, /latestReviewCountLabel\(item\)/);
  assert.match(cardSource, /<OwnStoreSalesComparisonMetrics/);
  assert.match(cardSource, /<CompetitorObservedSalesMetrics/);
  assert.match(cardSource, /class="competitor-category-platform-link"/);
  assert.doesNotMatch(cardSource, /competitor-category-product-(?:main|metrics|sales)/);
});

test("radar and category directory keep one compact card per row", () => {
  assert.match(
    stylesSource,
    /\.competitor-status-list \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)[\s\S]*gap: 8px/,
  );
  assert.match(
    stylesSource,
    /\.competitor-category-product-grid \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)[\s\S]*gap: 8px/,
  );
  assert.match(
    stylesSource,
    /\.competitor-status-card \{[\s\S]*max-width: none[\s\S]*gap: 7px[\s\S]*padding: 10px 12px[\s\S]*container-name: competitor-radar-card/,
  );
  assert.match(
    stylesSource,
    /@container competitor-radar-card \(max-width: 760px\) \{[\s\S]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/,
  );
  assert.match(
    stylesSource,
    /@container competitor-radar-card \(max-width: 520px\) \{[\s\S]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/,
  );
});

test("product detail stays above the category directory and its nested action modal", () => {
  assert.match(
    stylesSource,
    /\.competitor-product-detail-backdrop \{[\s\S]*z-index: 100/,
  );
  assert.match(
    stylesSource,
    /\.target-action-modal-backdrop \{[\s\S]*z-index: 110/,
  );
});
