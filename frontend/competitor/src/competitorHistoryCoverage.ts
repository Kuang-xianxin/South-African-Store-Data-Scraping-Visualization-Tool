import type { CompetitorItem, CompetitorOfferItem } from "./types";
import { parseUtcDateTime } from "./time.ts";
import {
  buildCompetitorOfferTrend,
  comparisonOffers,
  findSnapshotOffer,
  type CompetitorOfferTrendPoint,
} from "./competitorOfferHistory.ts";

export interface DetailOfferOption {
  offer: CompetitorOfferItem;
  historical: boolean;
  observedAt: string;
}

export interface OfferObservationPoint extends Omit<CompetitorOfferTrendPoint, "offer"> {
  offer: CompetitorOfferItem | null;
  observationLabel: string;
}

export interface MissingObservationMarker {
  index: number;
  x: number;
  y: number;
}

// Marker coordinates are visual navigation only, never a replacement observation.
// Interior gaps follow the existing bridge; unbounded gaps use a separate baseline.
export function buildMissingObservationMarkers(
  points: Array<{ index: number; x: number; y: number | null }>,
  missingBaselineY: number,
): MissingObservationMarker[] {
  const markers: MissingObservationMarker[] = [];
  let previous: { x: number; y: number } | null = null;
  let pending: Array<{ index: number; x: number }> = [];
  const flush = (next: { x: number; y: number } | null) => {
    for (const point of pending) {
      const y = previous && next && next.x > previous.x
        ? previous.y + (point.x - previous.x) / (next.x - previous.x) * (next.y - previous.y)
        : missingBaselineY;
      markers.push({ ...point, y });
    }
    pending = [];
  };
  for (const point of points) {
    if (point.y === null) {
      pending.push({ index: point.index, x: point.x });
    } else {
      const known = { x: point.x, y: point.y };
      flush(known);
      previous = known;
    }
  }
  flush(null);
  return markers;
}

export function hasOfferIdentity(offer: CompetitorOfferItem): boolean {
  const seller = String(offer.卖家 ?? "").trim().toLocaleLowerCase();
  return Boolean(offer.offer_id || offer.卖家ID
    || (seller && !["未知卖家", "unknown seller", "unknown"].includes(seller)));
}

function sameSource(left: CompetitorOfferItem, right: CompetitorOfferItem): boolean {
  return (left.报价来源 === "seller_api") === (right.报价来源 === "seller_api");
}

export function buildDetailOfferOptions(
  current: CompetitorItem | null,
  history: CompetitorItem[],
): DetailOfferOption[] {
  if (!current) return [];
  const options = comparisonOffers(current).map((offer) => ({
    offer, historical: false, observedAt: current.采集时间,
  }));
  const newestFirst = history.filter((item) => item.plid === current.plid)
    .slice().sort((a, b) => parseUtcDateTime(b.采集时间) - parseUtcDateTime(a.采集时间)
      || b.快照ID - a.快照ID);
  for (const snapshot of newestFirst) {
    for (const offer of comparisonOffers(snapshot)) {
      // Anonymous product availability is not a historical seller identity.
      // Retired private Offers retain their existing dedicated lifecycle UI.
      if (!hasOfferIdentity(offer) || offer.报价来源 === "seller_api") continue;
      const existing = options.filter((item) => sameSource(item.offer, offer)).map((item) => item.offer);
      if (findSnapshotOffer(existing, offer)) continue;
      options.push({ offer, historical: true, observedAt: snapshot.采集时间 });
    }
  }
  return options;
}

export function selectDefaultDetailOffer(options: DetailOfferOption[]): CompetitorOfferItem | null {
  return options.find((item) => !item.historical && hasOfferIdentity(item.offer) && item.offer.是否主报价)?.offer
    ?? options.find((item) => !item.historical && hasOfferIdentity(item.offer))?.offer
    ?? options.find((item) => item.historical)?.offer
    ?? options[0]?.offer ?? null;
}

function missingOfferLabel(snapshot: CompetitorItem): string {
  const offers = comparisonOffers(snapshot).filter((offer) => offer.报价来源 !== "seller_api");
  if (offers.length && offers.every((offer) => /supplier out of stock/i.test(offer.库存原始状态 ?? ""))) {
    return "商品显示供应商缺货；未返回该卖家报价";
  }
  return "已采集商品；未返回该卖家报价";
}

export function buildOfferObservationTrend(
  history: CompetitorItem[],
  selected: CompetitorOfferItem | null,
): OfferObservationPoint[] {
  if (!selected) return [];
  if (selected.报价来源 === "seller_api") {
    const ownHistory = history.filter((snapshot) => snapshot.plid === selected.plid).map((snapshot) => ({
      ...snapshot,
      对比报价: comparisonOffers(snapshot).filter((item) => item.报价来源 === "seller_api"),
    }));
    return buildCompetitorOfferTrend(ownHistory, selected).map((point) => ({
      ...point, observationLabel: point.exactStock === null ? "该次未取得精确库存" : "已取得报价与精确库存",
    }));
  }
  return history.filter((snapshot) => snapshot.plid === selected.plid
    && (snapshot.来源 !== "own_store"
      || comparisonOffers(snapshot).some((offer) => offer.报价来源 !== "seller_api")))
    .map((snapshot) => {
      const offer = findSnapshotOffer(
        comparisonOffers(snapshot).filter((item) => sameSource(item, selected)), selected,
      );
      const capturedAtMs = parseUtcDateTime(snapshot.采集时间);
      return {
        snapshot, offer,
        capturedAtMs: Number.isFinite(capturedAtMs) ? capturedAtMs : snapshot.快照ID,
        price: offer?.价格 ?? null,
        exactStock: offer?.库存精确 ? offer.库存数量 : null,
        reviews: snapshot.评论数可用 === false ? null : snapshot.评论数,
        observationLabel: !offer ? missingOfferLabel(snapshot)
          : !hasOfferIdentity(offer) ? "商品状态记录；卖家身份未返回"
            : !offer.库存精确 || offer.库存数量 === null ? "该次未取得精确库存"
              : "已取得报价与精确库存",
      };
    }).sort((a, b) => a.capturedAtMs - b.capturedAtMs || a.snapshot.快照ID - b.snapshot.快照ID);
}
