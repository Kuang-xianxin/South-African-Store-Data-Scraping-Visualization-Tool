import assert from "node:assert/strict";
import test from "node:test";
import {
  buildDetailOfferOptions, buildOfferObservationTrend, selectDefaultDetailOffer,
  buildMissingObservationMarkers,
} from "../src/competitorHistoryCoverage.ts";
import { filterCompetitorHistoryByDate } from "../src/competitorOfferHistory.ts";
import type { CompetitorItem, CompetitorOfferItem } from "../src/types.ts";

function offer(patch: Partial<CompetitorOfferItem> = {}): CompetitorOfferItem {
  return {
    plid: "123", 报价键: "offer:one", offer_id: "one", 卖家ID: "seller-one", 卖家: "Seller One",
    SKU: "sku-one", 变体键: "default", 条件: null, 价格: 819, 库存数量: 3, 库存精确: true,
    是否主报价: true, 报价来源: "public", 库存原始状态: "In stock", ...patch,
  } as CompetitorOfferItem;
}
function snapshot(day: number, offers: CompetitorOfferItem[], patch: Partial<CompetitorItem> = {}): CompetitorItem {
  return {
    plid: "123", 来源: "competitor", 快照ID: day, 采集时间: `2026-09-${String(day).padStart(2, "0")}T00:00:00`,
    跟卖报价: offers, 评论数: 3, ...patch,
  } as CompetitorItem;
}
const anonymous = () => offer({
  报价键: "variant-buybox:product-sku|default", offer_id: null, 卖家ID: null,
  卖家: "未知卖家", SKU: "product-sku", 库存数量: 0, 库存原始状态: "Supplier out of stock",
});

test("missing markers follow actual time spacing on bridges without changing observations", () => {
  const points = [{ index: 0, x: 0, y: 10 }, { index: 1, x: 1, y: null },
    { index: 2, x: 9, y: null }, { index: 3, x: 10, y: 30 }];
  const before = JSON.stringify(points);
  assert.deepEqual(buildMissingObservationMarkers(points, 100), [
    { index: 1, x: 1, y: 12 }, { index: 2, x: 9, y: 28 },
  ]);
  assert.equal(JSON.stringify(points), before);
});

test("unbounded missing markers use the missing baseline without extrapolating", () => {
  assert.deepEqual(buildMissingObservationMarkers([
    { index: 0, x: 0, y: null }, { index: 1, x: 5, y: 0 }, { index: 2, x: 10, y: null },
  ], 88), [{ index: 0, x: 0, y: 88 }, { index: 2, x: 10, y: 88 }]);
  assert.deepEqual(buildMissingObservationMarkers([{ index: 7, x: 5, y: null }], 88),
    [{ index: 7, x: 5, y: 88 }]);
  assert.deepEqual(buildMissingObservationMarkers([], 88), []);
});

test("zero remains a real point and duplicate timestamps cannot produce NaN markers", () => {
  assert.deepEqual(buildMissingObservationMarkers([
    { index: 0, x: 5, y: 0 }, { index: 1, x: 5, y: null }, { index: 2, x: 5, y: 10 },
  ], 88), [{ index: 1, x: 5, y: 88 }]);
});

test("sold-out latest product keeps its known seller history selectable and default", () => {
  const past = snapshot(8, [offer()]);
  const latest = snapshot(10, [anonymous()]);
  const original = JSON.stringify([past, latest]);
  const options = buildDetailOfferOptions(latest, [past, latest]);
  assert.equal(options.length, 2);
  assert.equal(selectDefaultDetailOffer(options)?.offer_id, "one");
  assert.equal(options[1]?.historical, true);
  assert.equal(options[1]?.observedAt, past.采集时间);
  assert.equal(JSON.stringify([past, latest]), original);
});

test("price and stock gaps keep actual capture dates and independent product reviews", () => {
  const history = [snapshot(1, [offer()]), snapshot(3, [anonymous()]), snapshot(8, [offer({ 库存数量: 5 })])];
  const trend = buildOfferObservationTrend(history, offer());
  assert.deepEqual(trend.map((p) => p.snapshot.快照ID), [1, 3, 8]);
  assert.deepEqual(trend.map((p) => p.exactStock), [3, null, 5]);
  assert.deepEqual(trend.map((p) => p.price), [819, null, 819]);
  assert.deepEqual(trend.map((p) => p.reviews), [3, 3, 3]);
  assert.equal(trend[1]?.offer, null);
  assert.match(trend[1]!.observationLabel, /供应商缺货/);
  assert.equal(buildOfferObservationTrend(filterCompetitorHistoryByDate(history, "2026-09-02", "2026-09-07"), offer()).length, 1);
});

test("seller, variant, product and source boundaries remain independent", () => {
  const variant = offer({ 报价键: "offer:two", offer_id: "two", SKU: "sku-two", 变体键: "blue" });
  const seller = offer({ 报价键: "offer:three", offer_id: "three", 卖家ID: "seller-three", 卖家: "Seller Three" });
  const latest = snapshot(10, [offer()]);
  const foreign = snapshot(9, [offer({ plid: "456", 报价键: "foreign", offer_id: "foreign" })], { plid: "456" });
  const options = buildDetailOfferOptions(latest, [foreign, snapshot(8, [variant, seller]), snapshot(7, [variant])]);
  assert.equal(options.length, 3);
  assert.equal(selectDefaultDetailOffer(options), latest.跟卖报价[0]);
  const trend = buildOfferObservationTrend([snapshot(1, [variant]), snapshot(2, [seller]), foreign], offer());
  assert.deepEqual(trend.map((p) => p.offer), [null, null]);
});

test("a returning known current offer wins over historical alternatives and is deduplicated", () => {
  const latest = snapshot(10, [offer({ 价格: 900 })]);
  const oldKey = offer({ 报价键: "legacy-key", 价格: 800 });
  const options = buildDetailOfferOptions(latest, [snapshot(1, [oldKey]), latest]);
  assert.equal(options.length, 1);
  assert.equal(selectDefaultDetailOffer(options)?.价格, 900);
  assert.equal(options[0]?.historical, false);
});

test("anonymous-only observations remain available without assigning a known seller", () => {
  const latest = snapshot(10, [anonymous()]);
  const options = buildDetailOfferOptions(latest, [snapshot(1, [anonymous()]), latest]);
  assert.equal(options.length, 1);
  const trend = buildOfferObservationTrend([latest], selectDefaultDetailOffer(options));
  assert.match(trend[0]!.observationLabel, /卖家身份未返回/);
  assert.equal(trend[0]!.exactStock, 0);
});

test("unknown inventory and missing reviews stay null instead of becoming zero", () => {
  const trend = buildOfferObservationTrend([
    snapshot(1, [offer({ 库存数量: null, 库存精确: false })], { 评论数可用: false }),
    snapshot(2, []),
  ], offer());
  assert.equal(trend[0]!.exactStock, null);
  assert.equal(trend[0]!.reviews, null);
  assert.match(trend[0]!.observationLabel, /未取得精确库存/);
  assert.match(trend[1]!.observationLabel, /已采集商品/);
});

test("private Seller API refresh points do not enter a public offer timeline", () => {
  const own = offer({ 报价来源: "seller_api", 报价键: "own", offer_id: "private" });
  const publicPoint = snapshot(1, [offer()], { 来源: "own_store" });
  const privatePoint = snapshot(2, [own], { 来源: "own_store" });
  assert.deepEqual(buildOfferObservationTrend([publicPoint, privatePoint], offer()).map((p) => p.snapshot.快照ID), [1]);
  assert.deepEqual(buildOfferObservationTrend([publicPoint, privatePoint], own).map((p) => p.snapshot.快照ID), [2]);
  assert.equal(buildDetailOfferOptions(snapshot(3, [anonymous()]), [privatePoint]).length, 1);
});
