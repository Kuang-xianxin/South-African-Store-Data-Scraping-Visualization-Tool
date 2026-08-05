import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCompetitorOfferHistory,
  buildCompetitorOfferTrend,
  comparableOfferNetOutflow,
  followerOffers,
  groupCompetitorOffersBySeller,
  findSnapshotOffer,
  sortCompetitorOffers,
} from "../src/competitorOfferHistory.ts";
import type { CompetitorItem, CompetitorOfferItem } from "../src/types.ts";

function offer(overrides: Partial<CompetitorOfferItem>): CompetitorOfferItem {
  return {
    报价键: "offer:main",
    offer_id: "main",
    卖家ID: "seller-main",
    卖家: "Main seller",
    SKU: "sku-main",
    价格: 100,
    库存状态: "有货",
    库存原始状态: "In Stock",
    库存数量: 5,
    库存精确: true,
    库存方式: "cart",
    库存说明: null,
    条件: null,
    变体键: "default",
    变体: "默认款",
    是否主报价: true,
    是否变体主报价: true,
    plid: "123",
    链接: "https://www.takealot.com/item/PLID123",
    区间起始价格: 100,
    价格变化: 0,
    价格信号: "价格不变",
    区间起始库存状态: "有货",
    区间起始库存数量: 5,
    库存数量变化: 0,
    库存可比: true,
    库存信号: "库存数量不变",
    ...overrides,
  };
}

function snapshot(id: number, offers: CompetitorOfferItem[]): CompetitorItem {
  return {
    来源: "competitor",
    快照ID: id,
    plid: "123",
    商品: "Shared product",
    图片: null,
    采集时间: `2026-08-0${id}T00:00:00`,
    当前卖家: "Main seller",
    价格: 100,
    区间起始价格: null,
    价格变化: null,
    价格信号: "原始快照",
    库存上限: "精确 5 件",
    库存数量: 5,
    库存精确: true,
    库存说明: null,
    库存参考过期: false,
    上次成功库存: null,
    上次成功库存数量: null,
    上次成功库存精确: false,
    上次成功库存时间: null,
    评论数: 10,
    评分: 4.5,
    好评: 8,
    中评: 1,
    差评: 1,
    观察期销量信号: "待积累",
    观察期估算下限: null,
    观察期估算上限: null,
    库存净变化: null,
    库存净流入: null,
    库存净流出: null,
    新增评论: null,
    新增好评: null,
    新增差评: null,
    趋势判断: "原始快照",
    判断说明: "原始快照",
    信号区间开始: null,
    信号区间结束: null,
    区间快照数: null,
    库存可比: null,
    链接: "https://www.takealot.com/item/PLID123",
    跟卖报价: offers,
    自有报价: [],
    共享评论说明: null,
  };
}

test("offer history hides snapshots that do not contain the selected seller offer", () => {
  const main = offer({});
  const follower = offer({
    报价键: "offer:follower",
    offer_id: "follower",
    卖家: "Follower seller",
    SKU: "sku-follower",
    价格: 90,
    库存数量: 2,
    是否主报价: false,
    是否变体主报价: false,
  });
  const history = buildCompetitorOfferHistory(
    [snapshot(1, [main]), snapshot(2, [main, follower])],
    follower,
  );

  assert.equal(history.length, 1);
  assert.equal(history[0]?.snapshot.快照ID, 2);
  assert.equal(history[0]?.offer.卖家, "Follower seller");
  assert.equal(history[0]?.offer.价格, 90);
});

test("offer_id keeps the same seller offer linked when a raw key changes", () => {
  const selected = offer({ 报价键: "latest-key", offer_id: "stable-offer" });
  const historical = offer({ 报价键: "older-key", offer_id: "stable-offer", 价格: 80 });

  assert.equal(findSnapshotOffer([historical], selected)?.价格, 80);
});

test("seller identity links history when offer_id is missing or changed", () => {
  const selected = offer({
    报价键: "offer:latest",
    offer_id: "latest-id",
    卖家ID: "seller-one",
    卖家: "Seller One",
    SKU: "seller-sku",
    变体键: "colour=black",
  });
  const historical = offer({
    报价键: "fallback:historical",
    offer_id: null,
    卖家ID: null,
    卖家: "Seller One",
    SKU: "seller-sku",
    变体键: "colour=black",
    价格: 75,
  });

  assert.equal(findSnapshotOffer([historical], selected)?.价格, 75);
});

test("seller-only fallback never crosses SKU or an ambiguous seller snapshot", () => {
  const selected = offer({
    报价键: "offer:latest",
    offer_id: null,
    卖家ID: "seller-one",
    卖家: "Seller One",
    SKU: null,
  });
  const first = offer({
    报价键: "fallback:first",
    offer_id: null,
    卖家ID: "seller-one",
    卖家: "Seller One",
    SKU: "sku-one",
  });
  const second = offer({
    报价键: "fallback:second",
    offer_id: null,
    卖家ID: "seller-one",
    卖家: "Seller One",
    SKU: "sku-two",
  });

  assert.equal(findSnapshotOffer([first, second], selected), null);
  assert.equal(
    findSnapshotOffer([first], { ...selected, SKU: "different-sku" }),
    null,
  );
});

test("offer trend sorts chronologically and only plots exact stock quantities", () => {
  const selected = offer({ 报价键: "offer:selected" });
  const later = snapshot(2, [
    offer({
      报价键: "offer:selected",
      价格: 95,
      库存数量: 8,
      库存精确: false,
    }),
  ]);
  later.采集时间 = "2026-08-03T08:00:00";
  later.评论数 = 14;
  const earlier = snapshot(1, [
    offer({ 报价键: "offer:selected", 价格: 100, 库存数量: 10, 库存精确: true }),
  ]);
  earlier.采集时间 = "2026-08-02T08:00:00";
  earlier.评论数 = 12;

  const trend = buildCompetitorOfferTrend([later, earlier], selected);

  assert.deepEqual(trend.map((point) => point.snapshot.快照ID), [1, 2]);
  assert.deepEqual(trend.map((point) => point.price), [100, 95]);
  assert.deepEqual(trend.map((point) => point.exactStock), [10, null]);
  assert.deepEqual(trend.map((point) => point.reviews), [12, 14]);
});

test("seller sorting prioritizes comparable interval net outflow and leaves unknowns last", () => {
  const replenished = offer({
    报价键: "offer:replenished",
    库存可比: true,
    库存数量变化: 6,
  });
  const unavailable = offer({
    报价键: "offer:unknown",
    库存可比: false,
    库存数量变化: null,
  });
  const outflowTwo = offer({
    报价键: "offer:outflow-two",
    库存可比: true,
    库存数量变化: -2,
  });
  const outflowSeven = offer({
    报价键: "offer:outflow-seven",
    库存可比: true,
    库存数量变化: -7,
  });

  assert.equal(comparableOfferNetOutflow(outflowSeven), 7);
  assert.equal(comparableOfferNetOutflow(replenished), 0);
  assert.equal(comparableOfferNetOutflow(unavailable), null);
  assert.deepEqual(
    sortCompetitorOffers(
      [replenished, unavailable, outflowTwo, outflowSeven],
      "net_outflow_desc",
    ).map((item) => item.报价键),
    ["offer:outflow-seven", "offer:outflow-two", "offer:replenished", "offer:unknown"],
  );
});

test("seller sorting keeps missing price and non-exact stock after comparable values", () => {
  const main = offer({
    报价键: "offer:main",
    价格: 120,
    库存数量: 9,
    库存精确: true,
    是否主报价: true,
  });
  const lowPrice = offer({
    报价键: "offer:low-price",
    价格: 80,
    库存数量: 4,
    库存精确: true,
    是否主报价: false,
  });
  const uncertainStock = offer({
    报价键: "offer:uncertain",
    价格: 90,
    库存数量: 2,
    库存精确: false,
    是否主报价: false,
  });
  const missingPrice = offer({
    报价键: "offer:missing-price",
    价格: null,
    库存数量: null,
    库存精确: false,
    是否主报价: false,
  });
  const offers = [uncertainStock, lowPrice, missingPrice, main];

  assert.deepEqual(
    sortCompetitorOffers(offers, "price_asc").map((item) => item.报价键),
    ["offer:low-price", "offer:uncertain", "offer:main", "offer:missing-price"],
  );
  assert.deepEqual(
    sortCompetitorOffers(offers, "stock_asc").map((item) => item.报价键),
    ["offer:low-price", "offer:main", "offer:uncertain", "offer:missing-price"],
  );
  assert.deepEqual(
    sortCompetitorOffers(offers, "default").map((item) => item.报价键),
    ["offer:main", "offer:uncertain", "offer:low-price", "offer:missing-price"],
  );
});

test("seller navigation deduplicates by seller name and keeps variants together", () => {
  const firstVariant = offer({
    报价键: "offer:first-variant",
    卖家ID: "seller-a",
    卖家: "Same Seller",
    SKU: "sku-a",
    变体: "Black",
  });
  const secondVariant = offer({
    报价键: "offer:second-variant",
    卖家ID: "seller-b",
    卖家: " same   seller ",
    SKU: "sku-b",
    变体: "White",
  });
  const otherSeller = offer({
    报价键: "offer:other-seller",
    卖家ID: "seller-c",
    卖家: "Other Seller",
  });

  const groups = groupCompetitorOffersBySeller(
    [firstVariant, secondVariant, otherSeller],
    "default",
  );

  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0]?.offers.map((item) => item.SKU), ["sku-a", "sku-b"]);
  assert.equal(groups[1]?.sellerName, "Other Seller");
});

test("follower status ignores variant primaries but includes green and red offer areas", () => {
  const variantPrimary = offer({
    报价键: "offer:variant-primary",
    是否变体主报价: true,
    是否跟卖: false,
  });
  const greenBuyboxAlternative = offer({
    报价键: "offer:green",
    是否主报价: false,
    是否变体主报价: true,
    是否跟卖: true,
  });
  const legacyRedOffer = offer({
    报价键: "offer:red",
    是否主报价: false,
    是否变体主报价: false,
    是否跟卖: undefined,
  });

  assert.deepEqual(
    followerOffers(snapshot(1, [variantPrimary, greenBuyboxAlternative, legacyRedOffer]))
      .map((item) => item.报价键),
    ["offer:green", "offer:red"],
  );
});
