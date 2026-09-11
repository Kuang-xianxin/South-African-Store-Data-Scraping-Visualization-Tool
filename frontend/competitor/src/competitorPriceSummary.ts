import { cachedNumberFormatter } from "./numberFormatters.ts";
import type { CompetitorItem } from "./types";

type PriceSource = Pick<CompetitorItem, "来源" | "价格" | "自有报价" | "跟卖报价" | "对比报价">;

function validPrice(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function formatPriceRange(values: Array<number | null | undefined>): string {
  const prices = values.filter(validPrice);
  if (!prices.length) return "—";
  const format = (value: number) => cachedNumberFormatter("en-ZA", {
    style: "currency", currency: "ZAR", maximumFractionDigits: 2,
  }).format(value);
  const low = Math.min(...prices), high = Math.max(...prices);
  return low === high ? format(low) : `${format(low)} – ${format(high)}`;
}

export function competitorPriceSummary(item: PriceSource) {
  const isOwn = item.来源 === "own_store";
  const ownOffers = item.自有报价 ?? [];
  const comparisons = item.对比报价 ?? item.跟卖报价 ?? [];
  const ownComparisons = comparisons.filter((offer) => offer.报价来源 === "seller_api");
  // A present but unpriced own Offer must not borrow a follower's price.
  const ownPrices = ownOffers.length
    ? ownOffers.map((offer) => offer.价格)
    : ownComparisons.length
      ? ownComparisons.map((offer) => offer.价格)
      : isOwn ? [item.价格] : [];
  const publicPrices = (isOwn ? item.跟卖报价 ?? [] : comparisons)
    .filter((offer) => offer.报价来源 !== "seller_api")
    .map((offer) => offer.价格);
  // The public main quote is already represented by comparisons when present.
  const allPrices = isOwn ? [...ownPrices, ...publicPrices]
    : publicPrices.length ? publicPrices : [item.价格];
  return {
    isOwn,
    all: formatPriceRange(allPrices),
    own: formatPriceRange(ownPrices),
    main: formatPriceRange([item.价格]),
  };
}
