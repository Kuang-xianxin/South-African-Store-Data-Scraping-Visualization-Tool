import assert from "node:assert/strict";
import test from "node:test";

import {
  alignOwnStoreTrafficTrendToOfferTrend,
  buildCompetitorOfferHistory,
  buildCompetitorOfferTrend,
  buildOwnStoreTrafficTrend,
  comparableOfferNetOutflow,
  filterCompetitorHistoryByDate,
  followerOffers,
  getCompetitorHistoryDateBounds,
  groupCompetitorOffersBySeller,
  findSnapshotOffer,
  needsFullCompetitorHistory,
  nearestObservedOwnStoreTrafficPoint,
  offerIntervalReplenishmentUnits,
  offerIntervalSalesUnits,
  sortCompetitorOffers,
} from "../src/competitorOfferHistory.ts";
import { getOwnStoreSalesRecentRange } from "../src/ownStoreSalesChart.ts";
import type {
  CompetitorItem,
  CompetitorOfferItem,
  OwnStoreTrafficSeries,
} from "../src/types.ts";

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

test("own-store traffic trend preserves gaps and title-change nodes", () => {
  const series: OwnStoreTrafficSeries = {
    store_code: "store-01",
    store_name: "Own store",
    plid: "123",
    offer_id: "offer-1",
    sku: "SKU-1",
    range_start: "2026-08-02",
    range_end: "2026-08-04",
    observed_count: 2,
    traffic_count: 2,
    missing_count: 1,
    metric_notice: "rolling traffic",
    points: [
      {
        date: "2026-08-02",
        captured_at: "2026-08-02T02:00:00Z",
        page_views_30_days: 100,
        title: "Old title",
        title_changed: false,
        previous_title: null,
        data_status: "observed",
      },
      {
        date: "2026-08-03",
        captured_at: null,
        page_views_30_days: null,
        title: null,
        title_changed: false,
        previous_title: null,
        data_status: "missing",
      },
      {
        date: "2026-08-04",
        captured_at: "2026-08-04T02:00:00Z",
        page_views_30_days: 140,
        title: "New title",
        title_changed: true,
        previous_title: "Old title",
        data_status: "observed",
      },
    ],
  };

  const trend = buildOwnStoreTrafficTrend(series);

  assert.deepEqual(trend.map((point) => point.page_views_30_days), [100, null, 140]);
  assert.equal(trend[2]?.title_changed, true);
  assert.equal(
    nearestObservedOwnStoreTrafficPoint(trend, Date.parse("2026-08-04T01:00:00Z"))
      ?.page_views_30_days,
    140,
  );
  assert.equal(
    nearestObservedOwnStoreTrafficPoint(trend, Date.parse("2026-08-03T00:00:00Z"))
      ?.data_status,
    "observed",
  );
});

test("traffic nodes align only to the exact Seller refresh timestamp", () => {
  const selected = offer({ 报价键: "offer:selected" });
  const firstOffer = snapshot(1, [selected]);
  firstOffer.采集时间 = "2026-08-02T01:00:00";
  const secondOffer = snapshot(2, [selected]);
  secondOffer.采集时间 = "2026-08-03T01:00:00";
  const offerTrend = buildCompetitorOfferTrend([firstOffer, secondOffer], selected);
  const trafficTrend = buildOwnStoreTrafficTrend({
    store_code: "store-01",
    store_name: "Own store",
    plid: "123",
    offer_id: "offer-1",
    sku: "SKU-1",
    range_start: "2026-08-02",
    range_end: "2026-08-03",
    observed_count: 3,
    traffic_count: 3,
    missing_count: 0,
    metric_notice: "rolling traffic",
    points: [
      {
        date: "2026-08-02",
        captured_at: "2026-08-02T01:00:00Z",
        page_views_30_days: 100,
        title: "Old title",
        title_changed: false,
        previous_title: null,
        data_status: "observed",
      },
      {
        date: "2026-08-03",
        captured_at: "2026-08-03T01:00:00Z",
        page_views_30_days: 120,
        title: "New title",
        title_changed: true,
        previous_title: "Old title",
        data_status: "observed",
      },
      {
        date: "2026-08-03",
        captured_at: "2026-08-03T08:00:00Z",
        page_views_30_days: 125,
        title: "New title",
        title_changed: false,
        previous_title: null,
        data_status: "observed",
      },
    ],
  });

  const aligned = alignOwnStoreTrafficTrendToOfferTrend(trafficTrend, offerTrend);

  assert.deepEqual(aligned.map((point) => point.alignedOfferIndex), [0, 1]);
  assert.deepEqual(
    aligned.map((point) => point.alignedCapturedAtMs),
    offerTrend.map((point) => point.capturedAtMs),
  );
  assert.deepEqual(
    aligned.map((point) => point.capturedAtMs),
    trafficTrend.slice(0, 2).map((point) => point.capturedAtMs),
  );
  const filteredOfferTrend = buildCompetitorOfferTrend(
    filterCompetitorHistoryByDate([firstOffer, secondOffer], "2026-08-03", "2026-08-03"),
    selected,
  );
  const filteredTraffic = alignOwnStoreTrafficTrendToOfferTrend(trafficTrend, filteredOfferTrend);
  assert.equal(filteredTraffic.length, 1);
  assert.equal(filteredTraffic[0]?.capturedAtMs, filteredOfferTrend[0]?.capturedAtMs);
  assert.equal(filteredTraffic[0]?.page_views_30_days, 120);
  assert.equal(filteredTraffic[0]?.title_changed, false);
  assert.equal(filteredTraffic[0]?.previous_title, null);
  assert.equal(trafficTrend[1]?.title_changed, true);
});

test("offer interval units accumulate exact decreases and replenishment separately", () => {
  const selected = offer({ 报价键: "offer:selected", 报价来源: "public_offer" });
  const stocks = [10, 6, 12, 9];
  const history = stocks.map((stock, index) => snapshot(index + 1, [
    offer({
      报价键: "offer:selected",
      报价来源: "public_offer",
      库存数量: stock,
      库存精确: true,
    }),
  ]));

  assert.equal(offerIntervalSalesUnits(history, selected), 7);
  assert.equal(offerIntervalReplenishmentUnits(history, selected), 6);
});

test("history date filters include both Beijing calendar days and reject invalid timestamps", () => {
  const timestamps = [
    "2026-07-31T15:59:59Z",
    "2026-07-31T16:00:00",
    "2026-08-02T23:59:59+08:00",
    "2026-08-02T16:00:00Z",
    "invalid",
  ];
  const history = timestamps.map((timestamp, index) => ({
    ...snapshot(index + 1, [offer({})]),
    采集时间: timestamp,
  }));

  assert.deepEqual(getCompetitorHistoryDateBounds(history), {
    start: "2026-07-31", end: "2026-08-03",
  });
  assert.equal(getCompetitorHistoryDateBounds([]), null);
  assert.equal(getCompetitorHistoryDateBounds([history[4]!]), null);
  for (const [start, end] of [["2026-08-01", "2026-08-02"], ["2026-08-02", "2026-08-01"]]) {
    assert.deepEqual(
      filterCompetitorHistoryByDate(history, start!, end!).map((item) => item.快照ID),
      [2, 3],
    );
  }
});

test("7, 15, 30, 60, 90 and all dates drive the same offer plots and separate inventory totals", () => {
  const observations = [
    ["2026-05-15", 300], ["2026-05-31", 280], ["2026-06-10", 320],
    ["2026-06-29", 270], ["2026-06-30", 200], ["2026-07-20", 260],
    ["2026-07-29", 240], ["2026-07-30", 230], ["2026-08-10", 220],
    ["2026-08-20", 250], ["2026-08-28", 245],
  ] as const;
  for (const source of ["public_offer", "seller_api"] as const) {
    const selected = offer({ 报价来源: source });
    const history = observations.map(([date, stock], index) => ({
      ...snapshot(index + 1, [offer({ ...selected, 库存数量: stock, 价格: index + 100 })]),
      采集时间: `${date}T10:00:00+08:00`,
      评论数: index + 20,
    }));
    const bounds = getCompetitorHistoryDateBounds(history)!;
    for (const [days, expectedSales, expectedReplenishment, expectedPoints] of [
      [7, null, null, 1], [15, 5, 0, 2],
      [30, 15, 30, 4], [60, 45, 90, 7], [90, 165, 130, 10], [0, 185, 130, 11],
    ] as const) {
      const range = getOwnStoreSalesRecentRange(bounds, days);
      const filtered = filterCompetitorHistoryByDate(history, range.start, range.end);
      const trend = buildCompetitorOfferTrend(filtered, selected);
      assert.equal(trend.length, expectedPoints);
      assert.equal(trend[0]?.price, filtered[0]?.跟卖报价[0]?.价格);
      assert.equal(trend[0]?.reviews, filtered[0]?.评论数);
      assert.equal(offerIntervalSalesUnits(filtered, selected), expectedSales);
      assert.equal(offerIntervalReplenishmentUnits(filtered, selected), expectedReplenishment);
    }
    const singlePoint = filterCompetitorHistoryByDate(history, "2026-08-28", "2026-08-28");
    assert.equal(offerIntervalSalesUnits(singlePoint, selected), null);
    assert.equal(offerIntervalReplenishmentUnits(singlePoint, selected), null);
    const empty = filterCompetitorHistoryByDate(history, "2026-08-21", "2026-08-27");
    assert.equal(buildCompetitorOfferTrend(empty, selected).length, 0);
    assert.equal(offerIntervalSalesUnits(empty, selected), null);
  }
});

test("inventory movement orders timezone-free database times as UTC like the chart", () => {
  const selected = offer({});
  const history = [
    { ...snapshot(1, [offer({ 库存数量: 6 })]), 采集时间: "2026-08-02T01:00:00" },
    { ...snapshot(2, [offer({ 库存数量: 10 })]), 采集时间: "2026-08-02T08:00:00+08:00" },
  ];
  assert.deepEqual(buildCompetitorOfferTrend(history, selected).map((point) => point.exactStock), [10, 6]);
  assert.equal(offerIntervalSalesUnits(history, selected), 4);
  assert.equal(offerIntervalReplenishmentUnits(history, selected), 0);
});

test("a restricted or unknown list range needs full local history, while a complete range is reused", () => {
  const available = { available_start: "2026-05-01", available_end: "2026-08-28" };
  assert.equal(needsFullCompetitorHistory("", "", available), false);
  assert.equal(needsFullCompetitorHistory("2026-05-01", "2026-08-28", available), false);
  assert.equal(needsFullCompetitorHistory("2026-07-01", "2026-08-28", available), true);
  assert.equal(needsFullCompetitorHistory("2026-05-01", "2026-07-31", available), true);
  assert.equal(needsFullCompetitorHistory("2026-07-01", "", { available_start: null, available_end: null }), true);
});

test("offer interval sales units ignore interleaved points from another source", () => {
  const selected = offer({ 报价键: "offer:selected", 报价来源: "public_offer" });
  const publicStart = offer({
    报价键: "offer:selected",
    报价来源: "public_offer",
    库存数量: 10,
  });
  const sellerApiPoint = offer({
    报价键: "seller-api:store-01:offer-1",
    报价来源: "seller_api",
    offer_id: "offer-1",
    卖家ID: "store-01",
    卖家: "Own store",
    库存数量: 99,
  });
  const publicEnd = offer({
    报价键: "offer:selected",
    报价来源: "public_offer",
    库存数量: 8,
  });

  assert.equal(
    offerIntervalSalesUnits(
      [snapshot(1, [publicStart]), snapshot(2, [sellerApiPoint]), snapshot(3, [publicEnd])],
      selected,
    ),
    2,
  );
  assert.equal(
    offerIntervalReplenishmentUnits(
      [snapshot(1, [publicStart]), snapshot(2, [sellerApiPoint]), snapshot(3, [publicEnd])],
      selected,
    ),
    0,
  );
});

test("offer interval units skip missing, inexact, and isolated scope observations", () => {
  const selected = offer({ 报价键: "offer:selected", 报价来源: "public_offer" });
  const exactStart = offer({
    报价键: "offer:selected",
    报价来源: "public_offer",
    库存数量: 10,
    库存精确: true,
  });
  const exactEnd = offer({
    报价键: "offer:selected",
    报价来源: "public_offer",
    库存数量: 7,
    库存精确: true,
  });
  const otherPublicOffer = offer({
    报价键: "offer:other",
    报价来源: "public_offer",
    offer_id: "other",
    卖家ID: "other-seller",
    卖家: "Other seller",
  });
  const inexactPoint = { ...exactStart, 库存数量: 500, 库存精确: false };
  const changedScopeStart = { ...exactStart, SKU: "changed-sku", 库存数量: 20 };
  const changedScopeEnd = { ...exactStart, SKU: "changed-sku", 库存数量: 17 };
  const isolatedScope = { ...exactStart, SKU: "isolated-sku", 库存数量: 99 };
  const usableHistory = [
    snapshot(1, [exactStart]),
    snapshot(2, [otherPublicOffer]),
    snapshot(3, [inexactPoint]),
    snapshot(4, [changedScopeStart]),
    snapshot(5, [isolatedScope]),
    snapshot(6, [changedScopeEnd]),
    snapshot(7, [exactEnd]),
  ];

  assert.equal(
    offerIntervalSalesUnits(usableHistory, selected),
    6,
  );
  assert.equal(
    offerIntervalReplenishmentUnits(usableHistory, selected),
    0,
  );
  assert.equal(
    offerIntervalSalesUnits(
      [snapshot(1, [exactStart]), snapshot(2, [inexactPoint])],
      selected,
    ),
    null,
  );
  assert.equal(
    offerIntervalSalesUnits(
      [snapshot(1, [exactStart]), snapshot(2, [changedScopeStart])],
      selected,
    ),
    null,
  );
  assert.equal(
    offerIntervalReplenishmentUnits(
      [snapshot(1, [exactStart]), snapshot(2, [changedScopeStart])],
      selected,
    ),
    null,
  );
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
