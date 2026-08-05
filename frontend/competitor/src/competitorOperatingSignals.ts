import type { CompetitorItem, CompetitorOfferItem } from "./types";

export const COMPETITOR_OPERATING_SIGNAL_OPTIONS = [
  "降价",
  "涨价",
  "价格不变",
  "补货",
  "库存减少",
  "库存数量不变",
  "评论增加",
  "好评增加",
  "差评增加",
  "库存减少且评论增加",
] as const;

export type CompetitorOperatingSignal =
  | "全部"
  | (typeof COMPETITOR_OPERATING_SIGNAL_OPTIONS)[number];

const KEPT_PRICE_SIGNALS = new Set(["降价", "涨价", "价格不变"]);
const REPLENISHMENT_SIGNALS = new Set(["库存增加", "恢复有货"]);

export function offerPriceOperatingSignal(offer: CompetitorOfferItem) {
  return KEPT_PRICE_SIGNALS.has(offer.价格信号) ? offer.价格信号 : null;
}

export function offerStockOperatingSignal(offer: CompetitorOfferItem) {
  if (REPLENISHMENT_SIGNALS.has(offer.库存信号)) return "补货";
  return offer.库存信号 === "库存数量不变" ? "库存数量不变" : null;
}

export function offerOperatingSignals(offer: CompetitorOfferItem) {
  return [offerPriceOperatingSignal(offer), offerStockOperatingSignal(offer)].filter(
    (signal): signal is string => signal !== null,
  );
}

export function competitorOperatingSignals(item: CompetitorItem) {
  const signals = new Set<(typeof COMPETITOR_OPERATING_SIGNAL_OPTIONS)[number]>();
  if (KEPT_PRICE_SIGNALS.has(item.价格信号)) {
    signals.add(item.价格信号 as "降价" | "涨价" | "价格不变");
  }
  for (const offer of item.跟卖报价) {
    const priceSignal = offerPriceOperatingSignal(offer);
    if (priceSignal !== null) {
      signals.add(priceSignal as "降价" | "涨价" | "价格不变");
    }
  }
  if (
    item.趋势判断 === "检测到补货"
    || item.跟卖报价.some((offer) => REPLENISHMENT_SIGNALS.has(offer.库存信号))
  ) {
    signals.add("补货");
  }
  const stockDecreased = (
    (item.库存净流出 !== null && item.库存净流出 > 0)
    || item.跟卖报价.some(
      (offer) => offer.库存信号 === "库存减少" || offer.库存信号 === "转为没货",
    )
  );
  if (stockDecreased) signals.add("库存减少");
  if (
    (item.库存可比 === true && item.库存净变化 === 0)
    || item.跟卖报价.some((offer) => offer.库存信号 === "库存数量不变")
  ) {
    signals.add("库存数量不变");
  }
  const reviewsIncreased = item.新增评论 !== null && item.新增评论 > 0;
  if (reviewsIncreased) signals.add("评论增加");
  if (item.新增好评 !== null && item.新增好评 > 0) signals.add("好评增加");
  if (item.新增差评 !== null && item.新增差评 > 0) signals.add("差评增加");
  if (stockDecreased && reviewsIncreased) signals.add("库存减少且评论增加");
  return COMPETITOR_OPERATING_SIGNAL_OPTIONS.filter((signal) => signals.has(signal));
}

export function matchesCompetitorOperatingSignal(
  item: CompetitorItem,
  signal: CompetitorOperatingSignal,
) {
  return signal === "全部" || competitorOperatingSignals(item).includes(signal);
}
