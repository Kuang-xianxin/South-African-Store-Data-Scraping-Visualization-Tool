import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { computed, effectScope, nextTick, reactive, ref, shallowRef, watch } from "vue";

const pageSource = readFileSync(new URL("../src/pages/ReturnsPage.vue", import.meta.url), "utf8");
const competitorSource = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("returns module keeps uncollected distinct from verified zero", () => {
  assert.match(pageSource, /尚未采集退货明细/);
  assert.match(pageSource, /Offers 滚动30天退货件数/);
  assert.doesNotMatch(pageSource, /outcomes · transactions/);
});

test("returns product card opens an exact company SKU history with own-link detail entry", () => {
  assert.match(pageSource, /查看该公司 SKU 全部退货/);
  assert.match(pageSource, /filterReturnsForCompanySku\(candidates, companySku\)/);
  assert.match(pageSource, /const requestPageSize = 100/);
  assert.doesNotMatch(pageSource, /忽略主列表当前的关键词、退货原因和处理结果筛选/);
  assert.match(pageSource, /openOwnStoreDetailTab/);
  assert.match(pageSource, /startDate: props\.rangeStart/);
  assert.match(pageSource, /endDate: props\.rangeEnd/);
  assert.match(pageSource, /<Teleport to="body">/);
  assert.match(pageSource, /companySkuOwnLinkImage\(link\)/);
  assert.match(pageSource, /@error="markReturnImageUnavailable\(link\.imageUrl\)"/);
  assert.match(pageSource, /width="192"/);
  assert.match(pageSource, /loading="lazy"/);
  assert.match(pageSource, /暂无图片/);
});

test("returns whole record row opens the company SKU history by pointer or keyboard", () => {
  assert.match(pageSource, /class="returns-record-row"/);
  assert.match(pageSource, /:class="\{ 'is-clickable': hasCompanySku\(item\) \}"/);
  assert.match(pageSource, /:tabindex="hasCompanySku\(item\) \? 0 : undefined"/);
  assert.match(pageSource, /:role="hasCompanySku\(item\) \? 'button' : undefined"/);
  assert.match(pageSource, /@click="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.enter\.self\.prevent="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.space\.self\.prevent="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /<div class="returns-product-card">/);
  assert.doesNotMatch(pageSource, /<button[^>]*class="returns-product-card"/);
  assert.match(pageSource, /tr\.returns-record-row\.is-clickable:hover > td/);
});

test("full Manage Removal Orders module keeps all stages fields and W8 evidence visible", () => {
  assert.match(pageSource, /Manage Removal Orders · PO 全部信息/);
  assert.match(pageSource, /Submitted \(/);
  assert.match(pageSource, /Ready For Pickup \(/);
  assert.match(pageSource, /Closed \(/);
  assert.match(pageSource, /Removal Order、Takealot Removal Order、Returns Removal Order/);
  assert.match(pageSource, /Date Submitted/);
  assert.match(pageSource, /Pickup Date/);
  assert.match(pageSource, /Closed Date/);
  assert.match(pageSource, /Total Weight \/ Boxes/);
  assert.match(pageSource, /Qty Requested \/ Prepared \/ Collected/);
  assert.match(pageSource, /Fees \(Incl VAT\)/);
  assert.match(pageSource, /RRN \/ Seller Return/);
  assert.match(pageSource, /刷新 PO 状态/);
  assert.doesNotMatch(pageSource, /同步移除 PO/);
  assert.match(pageSource, /runRemovalSync/);
  assert.match(pageSource, /verifyRemovalOtp/);
  assert.match(pageSource, /临期 \/ 已过期/);
  assert.match(pageSource, /要求\/备好\/已取/);
  assert.match(pageSource, /上架指长睿仓库库存/);
  assert.match(pageSource, /长睿仓已上架.*不代表已经寄回 Takealot/);
  assert.match(pageSource, /未把未知数量补成 0/);
  assert.match(pageSource, /退货与长睿关联概览/);
  assert.match(pageSource, /removalW8Label\(item\.removal_lifecycle\)/);
  assert.match(appSource, /canSyncRemovalOrders: canRefresh\.value/);
  assert.match(apiSource, /refreshReturnRemovalOrders/);
  assert.match(apiSource, /\/api\/erp\/returns\/removal-orders\/sync/);
  assert.match(apiSource, /\/api\/erp\/returns\/removal-orders\/verify-otp/);
});

test("own-store detail links to the consolidated returns module", () => {
  assert.match(competitorSource, /own_store_returns/);
  assert.match(competitorSource, /modulePageHref\('returns'\)/);
  assert.match(competitorSource, /退货情况/);
  assert.match(appSource, /label: "退货管理"/);
  assert.match(appSource, /returns: ReturnsPage/);
});

test("own-store returns show all collected history with bounded pagination", () => {
  const panel = competitorSource.slice(
    competitorSource.indexOf('class="panel own-return-panel"'),
    competitorSource.indexOf('class="panel competitor-offer-workbench"'),
  );
  assert.match(panel, /全部已采集历史/);
  assert.match(panel, /历史退货件数/);
  assert.match(panel, /Offers 滚动30天退货件数/);
  assert.match(panel, /v-for="item in visibleOwnReturnItems"/);
  assert.match(panel, /aria-label="退货明细分页"/);
  assert.match(panel, /:disabled="ownReturnPage <= 1"/);
  assert.match(panel, /:disabled="ownReturnPage >= ownReturnPageCount"/);
  assert.doesNotMatch(panel, /选定区间退货件数|own_store_returns\.range_start/);
});

test("own-store return pagination reaches older rows and resets only for identity or bounds", async () => {
  const start = competitorSource.indexOf("const ownReturnPage = ref(1);");
  const end = competitorSource.indexOf("const sharedBatchProgressIsAuthoritative", start);
  assert.ok(start >= 0 && end > start);
  const source = competitorSource.slice(start, end);
  const items = Array.from({ length: 45 }, (_, id) => ({ seller_return_id: String(id) }));
  const detail = shallowRef({ own_store_returns: { items } });
  const selectedPlid = ref("101190808");
  const detailOwnStoreScope = ref("current");
  const props = reactive({ currentStoreCode: "current" });
  const scope = effectScope();
  try {
    const pagination = scope.run(() => new Function(
      "ref", "computed", "watch", "detail", "selectedPlid", "detailOwnStoreScope", "props",
      `${source}\nreturn { ownReturnPage, ownReturnPageCount, visibleOwnReturnItems };`,
    )(ref, computed, watch, detail, selectedPlid, detailOwnStoreScope, props));
    assert.equal(pagination.ownReturnPageCount.value, 3);
    assert.deepEqual(pagination.visibleOwnReturnItems.value, items.slice(0, 20));
    pagination.ownReturnPage.value = 2;
    assert.deepEqual(pagination.visibleOwnReturnItems.value, items.slice(20, 40));
    pagination.ownReturnPage.value = 3;
    assert.deepEqual(pagination.visibleOwnReturnItems.value, items.slice(40));

    detail.value = { own_store_returns: { items: [...items] } };
    await nextTick();
    assert.equal(pagination.ownReturnPage.value, 3);
    for (const changeIdentity of [
      () => { selectedPlid.value = "another-plid"; },
      () => { detailOwnStoreScope.value = "all"; },
      () => { props.currentStoreCode = "store-02"; },
    ]) {
      pagination.ownReturnPage.value = 3;
      changeIdentity();
      await nextTick();
      assert.equal(pagination.ownReturnPage.value, 1);
    }

    pagination.ownReturnPage.value = 3;
    detail.value = { own_store_returns: { items: items.slice(0, 21) } };
    await nextTick();
    assert.equal(pagination.ownReturnPage.value, 2);
    assert.deepEqual(pagination.visibleOwnReturnItems.value, items.slice(20, 21));
    detail.value = { own_store_returns: { items: [] } };
    await nextTick();
    assert.equal(pagination.ownReturnPage.value, 1);
    assert.deepEqual(pagination.visibleOwnReturnItems.value, []);
  } finally {
    scope.stop();
  }
});
