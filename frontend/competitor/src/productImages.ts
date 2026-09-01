import { withStoreContext } from "./storeContext";

export const PRODUCT_IMAGE_SIZE = {
  list: 192,
} as const;

export type ProductImageSize =
  (typeof PRODUCT_IMAGE_SIZE)[keyof typeof PRODUCT_IMAGE_SIZE];

export function productThumbnailUrl(
  source: string | null | undefined,
  size: ProductImageSize = PRODUCT_IMAGE_SIZE.list,
  storeCode?: string | null,
  retryAttempt = 0,
): string {
  const normalized = String(source ?? "").trim();
  if (!normalized) return "";
  const query = new URLSearchParams({
    image_url: normalized,
    size: String(size),
  });
  if (retryAttempt > 0) {
    query.set("image_retry", String(Math.trunc(retryAttempt)));
  }
  const normalizedStoreCode = String(storeCode ?? "").trim().toLowerCase();
  if (normalizedStoreCode) {
    query.set("store_code", normalizedStoreCode);
    return `/api/erp/product-thumbnail?${query.toString()}`;
  }
  return withStoreContext(`/api/erp/product-thumbnail?${query.toString()}`);
}
