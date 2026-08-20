import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/QuadrantsPage.vue", import.meta.url),
  "utf8",
);
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

test("coordinate points use low-priority same-origin product thumbnails", () => {
  assert.match(pageSource, /class="matrix-dot coordinate"/);
  assert.match(pageSource, /'has-thumbnail': Boolean\(markerImageUrl\(item\)\)/);
  assert.match(pageSource, /v-if="markerImageUrl\(item\)"/);
  assert.match(pageSource, /productThumbnailUrl\(source, PRODUCT_IMAGE_SIZE\.list, item\.store_code\)/);
  assert.match(pageSource, /loading="lazy"/);
  assert.match(pageSource, /decoding="async"/);
  assert.match(pageSource, /fetchpriority="low"/);
  assert.match(pageSource, /requestAnimationFrame/);
  assert.match(stylesSource, /\.matrix-dot\.coordinate\.has-thumbnail img/);
});

test("all-product cards open standalone own-link detail by mouse or keyboard", () => {
  assert.match(pageSource, /openOwnStoreDetailTab/);
  assert.match(pageSource, /item\.productline_id/);
  assert.match(pageSource, /@click="activateProductCard\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.enter\.self\.prevent="activateProductCard\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.space\.self\.prevent="activateProductCard\(item, \$event\)"/);
  assert.match(pageSource, /window\.getSelection\(\)\?\.toString\(\)\.trim\(\)/);
  assert.match(pageSource, /scope === "current" && storeCode/);
  assert.match(pageSource, /startDate: props\.rangeStart/);
  assert.match(pageSource, /endDate: props\.rangeEnd/);
  assert.match(appSource, /if \(key === "quadrants"\)[\s\S]*?canViewCompetitors:/);
  assert.match(typesSource, /productline_id\?: string \| null;/);
  assert.match(stylesSource, /\.coordinate-product-card\.is-clickable:hover/);
});

test("the product list mounts one bounded page while retaining full search and sort", () => {
  assert.match(pageSource, /const productPageSize = 60/);
  assert.match(pageSource, /const visibleSortedItems = computed/);
  assert.match(pageSource, /v-for="item in visibleSortedItems"/);
  assert.match(pageSource, /当前显示 \{\{ productPageStart \}\}–\{\{ productPageEnd \}\}/);
  assert.match(pageSource, /watch\(\[skuQuery, productSort\]/);
  assert.doesNotMatch(pageSource, /v-for="item in sortedItems"/);
});
