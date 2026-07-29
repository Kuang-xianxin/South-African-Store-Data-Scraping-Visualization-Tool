export const PRODUCT_IMAGE_SIZE = {
  list: 192,
  detail: 640,
} as const;

export type ProductImageSize =
  (typeof PRODUCT_IMAGE_SIZE)[keyof typeof PRODUCT_IMAGE_SIZE];

export function productThumbnailUrl(
  source: string | null | undefined,
  size: ProductImageSize = PRODUCT_IMAGE_SIZE.list,
): string {
  const normalized = String(source ?? "").trim();
  if (!normalized) return "";
  const query = new URLSearchParams({
    image_url: normalized,
    size: String(size),
  });
  return `/api/erp/product-thumbnail?${query.toString()}`;
}
