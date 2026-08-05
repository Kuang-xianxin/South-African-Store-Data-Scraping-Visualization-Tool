import type { CompetitorItem, CompetitorOfferItem } from "./types";

export interface CompetitorOfferHistoryPoint {
  snapshot: CompetitorItem;
  offer: CompetitorOfferItem;
}

export interface CompetitorOfferTrendPoint extends CompetitorOfferHistoryPoint {
  capturedAtMs: number;
  price: number | null;
  exactStock: number | null;
  reviews: number | null;
}

export interface CompetitorSellerOfferGroup {
  key: string;
  sellerName: string;
  offers: CompetitorOfferItem[];
}

export type CompetitorOfferSort =
  | "net_outflow_desc"
  | "price_asc"
  | "stock_asc"
  | "default";

function normalizedIdentity(value: string | null | undefined): string {
  return String(value ?? "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function sellerMatches(
  offer: CompetitorOfferItem,
  selectedOffer: CompetitorOfferItem,
): boolean {
  const offerSellerId = normalizedIdentity(offer.卖家ID);
  const selectedSellerId = normalizedIdentity(selectedOffer.卖家ID);
  if (offerSellerId && selectedSellerId) return offerSellerId === selectedSellerId;

  const offerSeller = normalizedIdentity(offer.卖家);
  const selectedSeller = normalizedIdentity(selectedOffer.卖家);
  const unknownSellerNames = new Set(["", "未知卖家", "unknown seller"]);
  return !unknownSellerNames.has(offerSeller)
    && !unknownSellerNames.has(selectedSeller)
    && offerSeller === selectedSeller;
}

function compatibleOfferScope(
  offer: CompetitorOfferItem,
  selectedOffer: CompetitorOfferItem,
): boolean {
  const comparableFields: Array<keyof CompetitorOfferItem> = [
    "SKU",
    "变体键",
    "条件",
  ];
  return comparableFields.every((field) => {
    const offerValue = normalizedIdentity(String(offer[field] ?? ""));
    const selectedValue = normalizedIdentity(String(selectedOffer[field] ?? ""));
    return !offerValue || !selectedValue || offerValue === selectedValue;
  });
}

export function findSnapshotOffer(
  offers: CompetitorOfferItem[],
  selectedOffer: CompetitorOfferItem,
): CompetitorOfferItem | null {
  const exactKeyMatch = offers.find((offer) => offer.报价键 === selectedOffer.报价键);
  if (exactKeyMatch) return exactKeyMatch;

  if (selectedOffer.offer_id) {
    const offerIdMatch = offers.find((offer) => offer.offer_id === selectedOffer.offer_id);
    if (offerIdMatch) return offerIdMatch;
  }

  const sellerMatchesInSnapshot = offers.filter(
    (offer) => sellerMatches(offer, selectedOffer)
      && compatibleOfferScope(offer, selectedOffer),
  );
  return sellerMatchesInSnapshot.length === 1 ? sellerMatchesInSnapshot[0]! : null;
}

export function buildCompetitorOfferHistory(
  history: CompetitorItem[],
  selectedOffer: CompetitorOfferItem | null,
): CompetitorOfferHistoryPoint[] {
  if (!selectedOffer) return [];
  return history.flatMap((snapshot) => {
    const offer = findSnapshotOffer(comparisonOffers(snapshot), selectedOffer);
    return offer ? [{ snapshot, offer }] : [];
  });
}

export function buildCompetitorOfferTrend(
  history: CompetitorItem[],
  selectedOffer: CompetitorOfferItem | null,
): CompetitorOfferTrendPoint[] {
  return buildCompetitorOfferHistory(history, selectedOffer)
    .map((point) => {
      const capturedAtMs = Date.parse(point.snapshot.采集时间);
      return {
        ...point,
        capturedAtMs: Number.isFinite(capturedAtMs) ? capturedAtMs : point.snapshot.快照ID,
        price: point.offer.价格,
        exactStock: point.offer.库存精确 ? point.offer.库存数量 : null,
        reviews: point.snapshot.评论数可用 === false ? null : point.snapshot.评论数,
      };
    })
    .sort((left, right) =>
      left.capturedAtMs - right.capturedAtMs
      || left.snapshot.快照ID - right.snapshot.快照ID,
    );
}

export function comparisonOffers(item: CompetitorItem): CompetitorOfferItem[] {
  return item.对比报价 ?? item.跟卖报价;
}

export function followerOffers(item: CompetitorItem): CompetitorOfferItem[] {
  if (item.来源 === "own_store") return item.跟卖报价;
  return item.跟卖报价.filter(
    (offer) => offer.是否跟卖 ?? !offer.是否变体主报价,
  );
}

export function groupCompetitorOffersBySeller(
  offers: CompetitorOfferItem[],
  sort: CompetitorOfferSort,
): CompetitorSellerOfferGroup[] {
  const groups = new Map<string, CompetitorSellerOfferGroup>();
  for (const offer of sortCompetitorOffers(offers, sort)) {
    const sellerName = String(offer.卖家 || "未知卖家").trim() || "未知卖家";
    const normalizedName = normalizedIdentity(sellerName);
    const knownName = !new Set(["", "未知卖家", "unknown seller"]).has(normalizedName)
      && !normalizedName.startsWith("卖家id ");
    const key = knownName
      ? `name:${normalizedName}`
      : normalizedIdentity(offer.卖家ID)
        ? `id:${normalizedIdentity(offer.卖家ID)}`
        : `offer:${offer.报价键}`;
    const existing = groups.get(key);
    if (existing) {
      existing.offers.push(offer);
    } else {
      groups.set(key, { key, sellerName, offers: [offer] });
    }
  }
  return [...groups.values()];
}

export function comparableOfferNetOutflow(offer: CompetitorOfferItem): number | null {
  if (!offer.库存可比 || offer.库存数量变化 === null) return null;
  return offer.库存数量变化 < 0 ? Math.abs(offer.库存数量变化) : 0;
}

function compareNullableNumbers(
  left: number | null,
  right: number | null,
  direction: "asc" | "desc",
): number {
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return direction === "asc" ? left - right : right - left;
}

export function sortCompetitorOffers(
  offers: CompetitorOfferItem[],
  sort: CompetitorOfferSort,
): CompetitorOfferItem[] {
  return offers
    .map((offer, index) => ({ offer, index }))
    .sort((left, right) => {
      let comparison = 0;
      if (sort === "net_outflow_desc") {
        comparison = compareNullableNumbers(
          comparableOfferNetOutflow(left.offer),
          comparableOfferNetOutflow(right.offer),
          "desc",
        );
      } else if (sort === "price_asc") {
        comparison = compareNullableNumbers(left.offer.价格, right.offer.价格, "asc");
      } else if (sort === "stock_asc") {
        comparison = compareNullableNumbers(
          left.offer.库存精确 ? left.offer.库存数量 : null,
          right.offer.库存精确 ? right.offer.库存数量 : null,
          "asc",
        );
      } else {
        comparison = Number(right.offer.是否主报价) - Number(left.offer.是否主报价);
      }
      if (comparison !== 0) return comparison;
      return left.index - right.index;
    })
    .map(({ offer }) => offer);
}
