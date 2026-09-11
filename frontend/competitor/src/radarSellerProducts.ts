import type { CompetitorItem } from "./types";
import { normalizeCompetitorSellerId } from "./competitorSellerFilter.ts";

export interface RadarSeller { key: string; name: string; id: string | null; storeCode: string | null }
const clean = (value: unknown) => String(value ?? "").trim();
export function radarCardSellers(item: CompetitorItem): RadarSeller[] {
  const result = new Map<string, RadarSeller>();
  const own = (item.自有报价 ?? []).map((offer) => ({ ...offer, 卖家ID: null, 卖家: offer.店铺, 报价来源: "seller_api" as const }));
  for (const offer of [...(item.对比报价 ?? []), ...(item.跟卖报价 ?? []), ...own]) {
    const id = normalizeCompetitorSellerId(offer.卖家ID) || null;
    // Existing Seller API cards encode internal store identity in 卖家ID.
    const storeCode = offer.报价来源 === "seller_api"
      ? clean(offer.store_code) || (id && /^(current|store-\d+)$/.test(id) ? id : null) : null;
    const name = clean(offer.卖家 || offer.店铺);
    if (!id && !storeCode && (!name || /^(未知卖家|unknown(?: seller)?)$/i.test(name))) continue;
    if (offer.报价来源 === "seller_api" && !id && !storeCode
      && [...result.values()].some((seller) => seller.storeCode && seller.name.toLowerCase() === name.toLowerCase())) continue;
    const key = storeCode ? `store:${storeCode}` : id ? `id:${id.toLowerCase()}` : `name:${name.toLowerCase()}`;
    result.set(key, { key, name: name || `卖家 ${id || storeCode}`, id: storeCode ? null : id, storeCode });
  }
  if (!result.size && item.当前卖家 && !/^(未知卖家|unknown(?: seller)?)$/i.test(item.当前卖家.trim())) {
    const name = item.当前卖家.trim();
    result.set(`name:${name.toLowerCase()}`, { key: `name:${name.toLowerCase()}`, name, id: null, storeCode: null });
  }
  return [...result.values()];
}
export function matchesRadarSeller(item: CompetitorItem, seller: RadarSeller): boolean {
  return radarCardSellers(item).some((candidate) => seller.storeCode
    ? candidate.storeCode === seller.storeCode
    : seller.id ? candidate.id?.toLowerCase() === seller.id.toLowerCase()
    : candidate.name.toLowerCase() === seller.name.toLowerCase());
}
export function radarBrandLabel(item: { 品牌?: string | null; brand?: string | null }): string {
  return clean(item.品牌 || item.brand) || "待采集";
}
