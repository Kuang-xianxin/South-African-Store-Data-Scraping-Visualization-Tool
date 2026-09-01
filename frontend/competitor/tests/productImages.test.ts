import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";

test("thumbnail URLs carry the active store for native image requests", async (t) => {
  const vite = await createServer({ server: { middlewareMode: true } });
  t.after(() => vite.close());
  const { setActiveStoreCode } = await vite.ssrLoadModule("/src/storeContext.ts");
  const { PRODUCT_IMAGE_SIZE, productThumbnailUrl } = await vite.ssrLoadModule(
    "/src/productImages.ts",
  );
  setActiveStoreCode(" Store-03 ");

  const source =
    "http://takealot.s3.amazonaws.com/covers_images/example/s.file";
  const thumbnail = new URL(
    productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list),
    "http://erp.local",
  );

  assert.equal(thumbnail.origin, "http://erp.local");
  assert.equal(thumbnail.pathname, "/api/erp/product-thumbnail");
  assert.equal(thumbnail.searchParams.get("image_url"), source);
  assert.equal(thumbnail.searchParams.get("size"), "192");
  assert.equal(thumbnail.searchParams.get("store_code"), "store-03");
  assert.equal(thumbnail.searchParams.get("image_retry"), null);

  const rowScopedThumbnail = new URL(
    productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list, " Store-05 "),
    "http://erp.local",
  );
  assert.equal(rowScopedThumbnail.searchParams.get("size"), "192");
  assert.equal(rowScopedThumbnail.searchParams.get("store_code"), "store-05");

  const retriedThumbnail = new URL(
    productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list, undefined, 2),
    "http://erp.local",
  );
  assert.equal(retriedThumbnail.searchParams.get("image_retry"), "2");
  assert.equal(retriedThumbnail.searchParams.get("store_code"), "store-03");

  setActiveStoreCode("current");
  assert.equal(productThumbnailUrl(""), "");
  assert.equal(productThumbnailUrl(null), "");
});
