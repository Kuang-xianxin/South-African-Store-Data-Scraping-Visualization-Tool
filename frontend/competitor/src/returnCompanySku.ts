import type { SellerReturnItem } from "./types";

export interface CompanySkuReturnSummary {
  recordCount: number;
  returnUnits: number;
  storeCount: number;
}

export interface CompanySkuOwnLink {
  plid: string;
  productTitle: string;
  imageUrl: string | null;
  storeCode: string;
  storeName: string;
}

function normalizedCompanySku(value: string | null | undefined): string {
  return String(value ?? "").trim().toLocaleLowerCase();
}

export function filterReturnsForCompanySku(
  items: readonly SellerReturnItem[],
  companySku: string,
): SellerReturnItem[] {
  const expected = normalizedCompanySku(companySku);
  if (!expected) return [];
  const unique = new Map<string, SellerReturnItem>();
  for (const item of items) {
    if (normalizedCompanySku(item.company_sku) !== expected) continue;
    const key = item.store_scope_key || `${item.store_code}:${item.seller_return_id}`;
    if (!unique.has(key)) unique.set(key, item);
  }
  return [...unique.values()];
}

export function summarizeCompanySkuReturns(
  items: readonly SellerReturnItem[],
): CompanySkuReturnSummary {
  return {
    recordCount: items.length,
    returnUnits: items.reduce(
      (total, item) => total + Math.max(0, Number(item.quantity) || 0),
      0,
    ),
    storeCount: new Set(items.map((item) => item.store_code).filter(Boolean)).size,
  };
}

export function companySkuOwnLinks(
  items: readonly SellerReturnItem[],
): CompanySkuOwnLink[] {
  const links = new Map<string, CompanySkuOwnLink>();
  for (const item of items) {
    const plid = String(item.productline_id ?? "").trim();
    if (!plid) continue;
    const imageUrl = String(item.image_url ?? "").trim() || null;
    const existing = links.get(plid);
    if (existing) {
      if (!existing.imageUrl && imageUrl) {
        links.set(plid, {
          ...existing,
          imageUrl,
          storeCode: String(item.store_code || "").trim(),
          storeName: String(item.store_name || item.store_code || "当前店铺").trim(),
        });
      }
      continue;
    }
    links.set(plid, {
      plid,
      productTitle: String(
        item.product_title || item.company_product_name || item.sku || `PLID ${plid}`,
      ).trim(),
      imageUrl,
      storeCode: String(item.store_code || "").trim(),
      storeName: String(item.store_name || item.store_code || "当前店铺").trim(),
    });
  }
  return [...links.values()];
}
