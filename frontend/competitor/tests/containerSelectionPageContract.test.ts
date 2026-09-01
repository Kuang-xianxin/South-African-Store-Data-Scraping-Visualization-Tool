import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/ContainerSelectionPage.vue", import.meta.url),
  "utf8",
);
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const observedSalesSource = readFileSync(
  new URL("../src/components/CompetitorObservedSalesMetrics.vue", import.meta.url),
  "utf8",
);

test("container selection separates replenishment decisions from radar monitoring", () => {
  assert.match(pageSource, /补货建议/);
  assert.match(pageSource, /新品监控/);
  assert.match(pageSource, /缺失日期不补 0/);
  assert.match(pageSource, /近30天.*前30天/);
  assert.match(pageSource, /90天.*仅作背景/);
  assert.match(pageSource, /公开信号，不等同订单/);
  assert.match(pageSource, /category\.decision\.label/);
  assert.match(pageSource, /平台可售|收货中|在途/);
  assert.match(pageSource, /工作簿评论不作为近期需求/);
  assert.match(pageSource, /累计评论不判近期热销/);
  assert.match(pageSource, /category\.representatives/);
  assert.match(pageSource, /item\.role_labels/);
  assert.doesNotMatch(pageSource, /lifetime_sales_(?:min|max)/);
});

test("representative rows show all fixed stock-outflow windows only in the target column", () => {
  assert.match(pageSource, /import CompetitorObservedSalesMetrics/);
  assert.match(pageSource, /库存观察流出（件）/);
  assert.match(pageSource, /class="stock-outflow-cell"/);
  assert.equal(
    [...pageSource.matchAll(/<CompetitorObservedSalesMetrics/g)].length,
    1,
  );
  const outflowColumn = pageSource.slice(
    pageSource.indexOf('class="stock-outflow-cell"'),
    pageSource.indexOf('<td>', pageSource.indexOf('class="stock-outflow-cell"')),
  );
  assert.match(outflowColumn, /:values="item\.monitoring\.recent_observed_sales"/);
  assert.match(outflowColumn, /item\.monitoring\.recent_observed_sales_through/);
  assert.match(outflowColumn, /compact[\s\S]*embedded/);
  assert.doesNotMatch(outflowColumn, /recent_signal_score/);
  assert.doesNotMatch(pageSource, /stockOutflowWindowDays|observedStockOutflowLabel/);
  assert.match(observedSalesSource, /const windowDays = \[7, 15, 30, 60, 90\] as const/);
  assert.match(observedSalesSource, /<dt>\{\{ days \}\}天：<\/dt>/);
  assert.match(observedSalesSource, /数据不足/);
  assert.match(observedSalesSource, /库存观察 · 不等同订单/);
});

test("new-product monitoring keeps stacked category cards with independent link toggles", () => {
  assert.match(pageSource, /const expandedRadarCategoryIds = ref<Set<string>>\(new Set\(\)\)/);
  assert.match(pageSource, /class="radar-card-list"/);
  assert.match(pageSource, /v-for="category in radarCategories"/);
  assert.match(pageSource, /class="representative-links-toggle"/);
  assert.match(pageSource, /:aria-expanded="isRadarCategoryLinksExpanded\(category\.category_id\)"/);
  assert.match(pageSource, /@click="toggleRadarCategoryLinks\(category\.category_id\)"/);
  assert.match(pageSource, /v-if="isRadarCategoryLinksExpanded\(category\.category_id\)"/);
  assert.match(pageSource, /代表链接（\{\{ category\.representatives\.length \}\}）/);
  assert.match(pageSource, /\? "收起" : "展开"/);
  assert.match(pageSource, /const next = new Set\(expandedRadarCategoryIds\.value\)/);
  assert.doesNotMatch(pageSource, /activeRadarCategoryId|radar-directory|新品监控目录/);
});

test("new-product monitoring offers an all-or-specific category selector", () => {
  assert.match(pageSource, /const selectedRadarCategoryId = ref\("all"\)/);
  assert.match(pageSource, /class="radar-category-selector"/);
  assert.match(pageSource, /<select v-model="selectedRadarCategoryId">/);
  assert.match(pageSource, /全部类目（\{\{ payload\.radar_categories\.length \}\}）/);
  assert.match(pageSource, /v-for="category in payload\.radar_categories"/);
  assert.match(pageSource, /category\.category_id !== selectedRadarCategoryId\.value/);
});

test("every category card keeps one recent-signal-first representative image", () => {
  assert.match(pageSource, /function pickRadarCategoryCover/);
  assert.match(pageSource, /item\.monitoring\.qualified_recent_signal && withUsableImage\(item\)/);
  assert.match(pageSource, /item\.monitoring\.recent_signal && withUsableImage\(item\)/);
  assert.match(pageSource, /const radarCategoryCovers = computed/);
  assert.match(pageSource, /class="radar-category-cover"/);
  assert.match(pageSource, /radarCategoryCoverImage\(category\)/);
  assert.match(pageSource, /:alt="`\$\{category\.category_name\} 类目代表商品图`"/);
  assert.match(pageSource, /width="72"[\s\S]*height="72"[\s\S]*loading="lazy"[\s\S]*decoding="async"[\s\S]*fetchpriority="low"/);
  assert.match(pageSource, /@error="retrySelectionImage\(\$event, radarCategoryCoverImage\(category\)\)"/);
  assert.match(pageSource, /\.radar-category-cover img/);
});

test("collapsed categories keep their cover but do not mount representative rows", () => {
  assert.match(
    pageSource,
    /v-if="isRadarCategoryLinksExpanded\(category\.category_id\)"[\s\S]*v-for="item in category\.representatives"/,
  );
  assert.match(pageSource, /item\.current\.image_url/);
  assert.ok(pageSource.indexOf('class="radar-category-cover"') < pageSource.indexOf('class="representative-links-toggle"'));
  assert.doesNotMatch(pageSource, /directory-thumbnail/);
});

test("container selection uses one local read endpoint and keeps own-link detail in new tabs", () => {
  assert.match(apiSource, /\/api\/erp\/container-selection/);
  assert.match(pageSource, /ownStoreDetailPageHref/);
  assert.match(pageSource, /target="_blank"/);
});

test("container selection hides global store and date filters without retaining hidden scope", () => {
  const propsSource = appSource.slice(
    appSource.indexOf('if (key === "container-selection")'),
    appSource.indexOf('if (key === "quadrants")'),
  );

  assert.match(
    appSource,
    /session\.user\.accessible_stores\.length\s+&& currentPage !== 'container-selection'/,
  );
  assert.match(
    appSource,
    /\['search-ranking', 'logistics', 'anomaly-products', 'container-selection', 'competitors', 'users'\]\.includes\(currentPage\)/,
  );
  assert.match(propsSource, /asOf: dataToday/);
  assert.doesNotMatch(
    propsSource,
    /rangeStart|rangeEnd|currentStoreCode|currentStoreName|\.\.\.common/,
  );
  assert.doesNotMatch(
    pageSource,
    /rangeStart\?: string|rangeEnd\?: string|currentStoreCode\?: string|currentStoreName\?: string/,
  );
  assert.doesNotMatch(
    pageSource,
    /requested-detail-start-date|requested-detail-end-date|current-store-code|current-store-name/,
  );
});

test("radar rows and retained cards open the shared competitor detail in place", () => {
  assert.match(pageSource, /const EmbeddedCompetitorDetail = defineAsyncComponent/);
  assert.match(pageSource, /embedded-detail-only/);
  assert.match(pageSource, /@detail-closed="closeRadarDetail"/);
  assert.equal(
    [...pageSource.matchAll(/@click="openRadarDetail\(item, \$event\)"/g)].length,
    2,
  );
  assert.match(pageSource, /<tr[\s\S]*class="clickable-detail-row"[\s\S]*role="button"[\s\S]*tabindex="0"/);
  assert.match(pageSource, /<article[\s\S]*class="clickable-detail-card"[\s\S]*role="button"[\s\S]*tabindex="0"/);
  assert.equal([...pageSource.matchAll(/@keydown\.enter\.self="openRadarDetail\(item, \$event\)"/g)].length, 2);
  assert.equal([...pageSource.matchAll(/@keydown\.space\.self\.prevent="openRadarDetail\(item, \$event\)"/g)].length, 2);
  assert.match(pageSource, /:href="item\.url"[\s\S]*?target="_blank"[\s\S]*?@click\.stop/);
  assert.doesNotMatch(pageSource, />雷达<\/button>|>打开雷达<\/button>/);
  assert.doesNotMatch(pageSource, /competitorDetailPageHref|radarDetailHref/);
});

test("radar detail is warmed while idle, prefetched on intent, and its mounted host is reused", () => {
  assert.match(pageSource, /loadEmbeddedCompetitorDetail/);
  assert.match(pageSource, /requestIdleCallback/);
  assert.match(pageSource, /const radarDetailHostReady = ref\(false\)/);
  assert.match(pageSource, /ensureRadarDetailHost\(\)/);
  assert.match(pageSource, /const radarDetailPrefetchDelayMs = 140/);
  assert.match(pageSource, /scheduleRadarDetailPrefetch\(item\)/);
  assert.match(pageSource, /:requested-detail-prefetch-plid="radarDetailPrefetchPlid"/);
  assert.match(pageSource, /:requested-detail-prefetch-revision="radarDetailPrefetchRevision"/);
  assert.match(pageSource, /v-if="radarDetailHostReady"/);
  assert.doesNotMatch(pageSource, /:key="`container-radar-detail-/);
});

test("closing embedded radar detail preserves the container page state and scroll", () => {
  const closeSource = pageSource.slice(
    pageSource.indexOf("async function closeRadarDetail"),
    pageSource.indexOf("function categoryOpeningTone"),
  );

  assert.match(closeSource, /radarDetailPlid\.value = ""/);
  assert.match(closeSource, /await nextTick\(\)/);
  assert.match(closeSource, /focus\(\{ preventScroll: true \}\)/);
  assert.doesNotMatch(
    closeSource,
    /\bload\(|activeView\.value|recommendationFilter\.value|search\.value|expandedSku\.value|window\.location/,
  );
});

test("replenishment detail expands every link and opens each link in its own store context", () => {
  assert.match(pageSource, /v-for="link in item\.sales\.links"/);
  assert.match(pageSource, /expandedSku === item\.company_sku \? "收起详情" : "详情"/);
  assert.match(pageSource, /ownLinkDetailHref\(link\)/);
  assert.match(pageSource, /plid: link\.plid/);
  assert.match(pageSource, /scope: "current"/);
  assert.match(pageSource, /storeCode: link\.store_code/);
  assert.match(pageSource, /打开该链路完整详情/);
  assert.doesNotMatch(pageSource, /item\.plids\[0\]/);
});

test("replenishment and radar monitoring use authenticated lazy thumbnails", () => {
  assert.match(pageSource, /productThumbnailUrl/);
  assert.match(pageSource, /PRODUCT_IMAGE_SIZE\.list/);
  assert.match(pageSource, /item\.image_url, item\.image_store_code/);
  assert.match(pageSource, /item\.current\.image_url/);
  assert.match(pageSource, /selection-product-image/);
  assert.match(pageSource, /loading="lazy"/);
  assert.match(pageSource, /decoding="async"/);
  assert.match(pageSource, /暂无图片/);
  assert.doesNotMatch(pageSource, /:src="item\.(?:image_url|current\.image_url)"/);
});

test("container selection omits the page-only container mix helper", () => {
  assert.match(pageSource, /payload\.policy\.electrified_volume_limit_percent/);
  assert.doesNotMatch(pageSource, /CONTAINER MIX CHECK|本柜配比速算/);
  assert.doesNotMatch(pageSource, /containerCapacityCbm|plannedElectrifiedCbm|containerMix/);
  assert.doesNotMatch(pageSource, /mix-calculator|mix-inputs|mix-results|mix-placeholder|mix-warning/);
  assert.doesNotMatch(pageSource, /当前补货建议可提供|扣除本批建议后仍待填充/);
});
