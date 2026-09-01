import type {
  CompetitorDateRange,
  CompetitorItem,
  CompetitorOfferItem,
  OwnStoreTrafficPoint,
  OwnStoreTrafficSeries,
} from "./types";
import { parseUtcDateTime } from "./time.ts";

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

export interface OwnStoreTrafficTrendPoint extends OwnStoreTrafficPoint {
  sourceIndex: number;
  capturedAtMs: number;
}

export interface AlignedOwnStoreTrafficTrendPoint extends OwnStoreTrafficTrendPoint {
  alignedCapturedAtMs: number;
  alignedOfferIndex: number | null;
}

export interface CompetitorOfferIntervalInventoryMovement {
  salesUnits: number;
  replenishmentUnits: number;
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

export function needsFullCompetitorHistory(
  startDate: string,
  endDate: string,
  available: Pick<CompetitorDateRange, "available_start" | "available_end">,
): boolean {
  return Boolean(
    (startDate && (!available.available_start || startDate > available.available_start))
    || (endDate && (!available.available_end || endDate < available.available_end)),
  );
}

export function getCompetitorHistoryDateBounds(
  history: CompetitorItem[],
): { start: string; end: string } | null {
  let first = Infinity;
  let last = -Infinity;
  for (const snapshot of history) {
    const timestamp = parseUtcDateTime(snapshot.采集时间);
    if (!Number.isFinite(timestamp)) continue;
    first = Math.min(first, timestamp);
    last = Math.max(last, timestamp);
  }
  if (!Number.isFinite(first)) return null;
  const chinaDate = (timestamp: number) =>
    new Date(timestamp + 8 * 60 * 60 * 1_000).toISOString().slice(0, 10);
  return { start: chinaDate(first), end: chinaDate(last) };
}

export function filterCompetitorHistoryByDate(
  history: CompetitorItem[],
  startDate: string,
  endDate: string,
): CompetitorItem[] {
  const [start, end] = startDate <= endDate ? [startDate, endDate] : [endDate, startDate];
  const startAt = Date.parse(`${start}T00:00:00+08:00`);
  const endExclusive = Date.parse(`${end}T00:00:00+08:00`) + 24 * 60 * 60 * 1_000;
  return history.filter((snapshot) => {
    const timestamp = parseUtcDateTime(snapshot.采集时间);
    return Number.isFinite(timestamp) && timestamp >= startAt && timestamp < endExclusive;
  });
}

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
      const capturedAtMs = parseUtcDateTime(point.snapshot.采集时间);
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

export function buildOwnStoreTrafficTrend(
  series: OwnStoreTrafficSeries | null,
): OwnStoreTrafficTrendPoint[] {
  if (!series) return [];
  return series.points
    .map((point, sourceIndex) => {
      const capturedAtMs = parseUtcDateTime(
        point.captured_at ?? `${point.date}T12:00:00+08:00`,
      );
      return {
        ...point,
        sourceIndex,
        capturedAtMs: Number.isFinite(capturedAtMs) ? capturedAtMs : sourceIndex,
      };
    })
    .sort((left, right) =>
      left.capturedAtMs - right.capturedAtMs
      || left.sourceIndex - right.sourceIndex,
    );
}

export function alignOwnStoreTrafficTrendToOfferTrend(
  trafficTrend: OwnStoreTrafficTrendPoint[],
  offerTrend: CompetitorOfferTrendPoint[],
): AlignedOwnStoreTrafficTrendPoint[] {
  const offerIndexByCapturedAt = new Map(
    offerTrend.map((point, index) => [point.capturedAtMs, index]),
  );
  const aligned = trafficTrend.flatMap((point) => {
    const alignedOfferIndex = offerIndexByCapturedAt.get(point.capturedAtMs);
    return alignedOfferIndex === undefined
      ? []
      : [{
          ...point,
          alignedCapturedAtMs: point.capturedAtMs,
          alignedOfferIndex,
        }];
  });
  const firstTitleIndex = aligned.findIndex((point) => Boolean(point.title));
  return aligned.map((point, index) => index === firstTitleIndex
    ? { ...point, title_changed: false, previous_title: null }
    : point);
}

export function nearestObservedOwnStoreTrafficPoint(
  trend: OwnStoreTrafficTrendPoint[],
  capturedAtMs: number | null,
): OwnStoreTrafficTrendPoint | null {
  const observed = trend.filter((point) => point.data_status === "observed");
  if (!observed.length) return null;
  if (capturedAtMs === null || !Number.isFinite(capturedAtMs)) {
    return observed[observed.length - 1] ?? null;
  }
  return observed.reduce((nearest, point) =>
    Math.abs(point.capturedAtMs - capturedAtMs)
      < Math.abs(nearest.capturedAtMs - capturedAtMs)
      ? point
      : nearest,
  );
}

function offerInventoryScopeKey(offer: CompetitorOfferItem): string {
  const scopeFields: Array<keyof CompetitorOfferItem> = [
    "卖家ID",
    "卖家",
    "SKU",
    "变体键",
    "条件",
  ];
  return scopeFields
    .map((field) => normalizedIdentity(String(offer[field] ?? "")))
    .join("\u001f");
}

export function offerIntervalInventoryMovement(
  history: CompetitorItem[],
  selectedOffer: CompetitorOfferItem | null,
): CompetitorOfferIntervalInventoryMovement | null {
  if (!selectedOffer) return null;

  const selectedSource = selectedOffer.报价来源;
  const sourceTimeline = history
    .slice()
    .sort((left, right) => {
      const leftTime = parseUtcDateTime(left.采集时间);
      const rightTime = parseUtcDateTime(right.采集时间);
      const safeLeftTime = Number.isFinite(leftTime) ? leftTime : left.快照ID;
      const safeRightTime = Number.isFinite(rightTime) ? rightTime : right.快照ID;
      return safeLeftTime - safeRightTime || left.快照ID - right.快照ID;
    })
    .filter((snapshot) => {
      const offers = comparisonOffers(snapshot);
      return selectedSource
        ? offers.some((offer) => offer.报价来源 === selectedSource)
        : offers.length > 0;
    })
    .map((snapshot) => ({
      snapshot,
      offer: findSnapshotOffer(comparisonOffers(snapshot), selectedOffer),
    }));

  const exactStockByScope = new Map<string, number[]>();
  for (const point of sourceTimeline) {
    const offer = point.offer;
    if (!offer?.库存精确 || offer.库存数量 === null) continue;
    const scopeKey = offerInventoryScopeKey(offer);
    const timeline = exactStockByScope.get(scopeKey) ?? [];
    timeline.push(offer.库存数量);
    exactStockByScope.set(scopeKey, timeline);
  }
  const comparableTimelines = [...exactStockByScope.values()]
    .filter((timeline) => timeline.length >= 2);
  if (comparableTimelines.length === 0) return null;

  let salesUnits = 0;
  let replenishmentUnits = 0;
  for (const timeline of comparableTimelines) {
    for (let index = 1; index < timeline.length; index += 1) {
      const previousStock = timeline[index - 1];
      const currentStock = timeline[index];
      if (previousStock === undefined || currentStock === undefined) continue;
      const change = currentStock - previousStock;
      if (change < 0) salesUnits += Math.abs(change);
      if (change > 0) replenishmentUnits += change;
    }
  }
  return { salesUnits, replenishmentUnits };
}

export function offerIntervalSalesUnits(
  history: CompetitorItem[],
  selectedOffer: CompetitorOfferItem | null,
): number | null {
  return offerIntervalInventoryMovement(history, selectedOffer)?.salesUnits ?? null;
}

export function offerIntervalReplenishmentUnits(
  history: CompetitorItem[],
  selectedOffer: CompetitorOfferItem | null,
): number | null {
  return offerIntervalInventoryMovement(history, selectedOffer)?.replenishmentUnits ?? null;
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
