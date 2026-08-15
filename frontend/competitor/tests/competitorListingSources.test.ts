import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DEFAULT_COMPETITOR_LISTING_SORTS,
  classifyCompetitorEntryUrl,
  listingSortFromUrl,
  mergeListingSortsFromUrl,
  parseOptionalListingInteger,
  toggleCompetitorListingSort,
  validateCompetitorEntryUrl,
} from "../src/competitorListingSources.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

test("classifies product, seller, and category URLs separately", () => {
  assert.equal(
    classifyCompetitorEntryUrl("https://www.takealot.com/example/PLID12345678"),
    "product",
  );
  assert.equal(
    classifyCompetitorEntryUrl(
      "https://www.takealot.com/seller/techitstore?sellers=29853614",
    ),
    "seller",
  );
  assert.equal(
    classifyCompetitorEntryUrl(
      "https://www.takealot.com/all?custom=new-to-tal-appliances",
    ),
    "category",
  );
  const seoCategoryUrls = [
    "https://www.takealot.com/camping-outdoor/family-tents-27895",
    "https://www.takealot.com/camping-outdoor/tents-25681",
    "https://www.takealot.com/camping-outdoor/tents-and-shelter-25675",
  ];
  for (const categoryUrl of seoCategoryUrls) {
    assert.equal(classifyCompetitorEntryUrl(categoryUrl), "category");
    assert.equal(validateCompetitorEntryUrl(categoryUrl, "category"), null);
  }
  assert.equal(
    classifyCompetitorEntryUrl(
      "https://www.takealot.com/camping-outdoor/family-tents",
    ),
    null,
  );
  assert.match(
    validateCompetitorEntryUrl(
      "https://www.takealot.com/camping-outdoor/family-tents",
      "category",
    ) ?? "",
    /数字类目 ID/,
  );
});

test("rejects a valid Takealot link in the wrong dedicated entry", () => {
  assert.match(
    validateCompetitorEntryUrl(
      "https://www.takealot.com/seller/techitstore?sellers=29853614",
      "product",
    ) ?? "",
    /店铺链接.*切换/,
  );
  assert.match(
    validateCompetitorEntryUrl(
      "https://www.takealot.com/all?custom=new-to-tal-appliances",
      "seller",
    ) ?? "",
    /类目链接.*切换/,
  );
});

test("reads a supported default sort from the pasted listing URL", () => {
  assert.deepEqual(DEFAULT_COMPETITOR_LISTING_SORTS, [
    "Rating Descending",
    "ReleaseDate Descending",
  ]);
  assert.equal(
    listingSortFromUrl(
      "https://www.takealot.com/all?custom=new-to-tal-appliances"
      + "&sort=ReleaseDate%20Descending",
    ),
    "ReleaseDate Descending",
  );
  assert.equal(
    listingSortFromUrl(
      "https://www.takealot.com/all?custom=new-to-tal-appliances&sort=Unknown",
    ),
    null,
  );
  assert.deepEqual(
    mergeListingSortsFromUrl(
      DEFAULT_COMPETITOR_LISTING_SORTS,
      "https://www.takealot.com/all?custom=new-to-tal-appliances"
      + "&sort=ReleaseDate%20Descending",
    ),
    ["Rating Descending", "ReleaseDate Descending"],
  );
  assert.deepEqual(
    mergeListingSortsFromUrl(
      DEFAULT_COMPETITOR_LISTING_SORTS,
      "https://www.takealot.com/all?custom=new-to-tal-appliances"
      + "&sort=Price%20Ascending",
    ),
    ["Rating Descending", "ReleaseDate Descending", "Price Ascending"],
  );
});

test("accepts numeric values emitted by Vue number inputs", () => {
  assert.equal(parseOptionalListingInteger(800, "最低价格"), 800);
  assert.equal(parseOptionalListingInteger(10, "加入数量"), 10);
  assert.equal(parseOptionalListingInteger("", "最高价格"), undefined);
  assert.equal(parseOptionalListingInteger(" 1000 ", "最高价格"), 1000);
  assert.throws(
    () => parseOptionalListingInteger(10.5, "加入数量"),
    /加入数量必须是非负整数/,
  );
});

test("keeps ascending and descending price sorts mutually exclusive", () => {
  assert.deepEqual(
    toggleCompetitorListingSort(
      ["Rating Descending", "Price Ascending"],
      "Price Descending",
    ),
    ["Rating Descending", "Price Descending"],
  );
  assert.deepEqual(
    toggleCompetitorListingSort(
      ["Rating Descending", "Price Descending"],
      "Price Ascending",
    ),
    ["Rating Descending", "Price Ascending"],
  );
  assert.deepEqual(
    mergeListingSortsFromUrl(
      ["Rating Descending", "Price Descending"],
      "https://www.takealot.com/all?custom=appliances&sort=Price%20Ascending",
    ),
    ["Rating Descending", "Price Ascending"],
  );
});

test("competitor workspace keeps dedicated entries and human preview confirmation", () => {
  assert.match(pageSource, /商品链接/);
  assert.match(pageSource, /店铺链接/);
  assert.match(pageSource, /类目链接/);
  assert.match(pageSource, /family-tents-27895/);
  assert.match(pageSource, /数字类目 ID.*\/all\?custom=/);
  assert.match(pageSource, /排序（可多选）/);
  assert.match(pageSource, /筛选结果超过 20 时必填/);
  assert.match(pageSource, /本次加入类型库（必选）/);
  assert.match(pageSource, /不能使用默认设置代替/);
  assert.match(pageSource, /确认加入 \$\{listingConfirmationCount\} 个去重商品/);
  assert.match(pageSource, /修改数量不会重新扫描 Takealot/);
  assert.match(pageSource, /先比较两边较差名次、再比较名次总和/);
  assert.match(pageSource, /价格：从高到低.*价格：从低到高.*互斥/);
  assert.match(pageSource, /按 PLID 去重/);
});

test("personal watchlist exposes the full competitor and own-store filter set", () => {
  assert.match(pageSource, /aria-label="个人监控池商品来源"/);
  assert.match(pageSource, /personalWatchlistSourceView === 'competitor'/);
  assert.match(pageSource, /personalWatchlistSourceView === 'own_store'/);
  assert.match(pageSource, /v-model="personalWatchlistQuery"/);
  assert.match(pageSource, /v-model="personalWatchlistSellerQuery"/);
  assert.match(pageSource, /v-model="personalWatchlistStockFilter"/);
  assert.match(pageSource, /v-model="personalWatchlistFollowerFilter"/);
  assert.match(pageSource, /v-model="personalWatchlistSignalFilter"/);
  assert.match(pageSource, /v-model="personalWatchlistSortDirection"/);
  assert.match(pageSource, /v-model\.number="personalWatchlistPageSize"/);
  assert.match(pageSource, /当前个人池分区显示/);
});
