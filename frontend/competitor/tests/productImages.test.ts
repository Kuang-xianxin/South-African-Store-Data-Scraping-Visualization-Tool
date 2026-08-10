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
    productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.detail),
    "http://erp.local",
  );

  assert.equal(thumbnail.origin, "http://erp.local");
  assert.equal(thumbnail.pathname, "/api/erp/product-thumbnail");
  assert.equal(thumbnail.searchParams.get("image_url"), source);
  assert.equal(thumbnail.searchParams.get("size"), "640");
  assert.equal(thumbnail.searchParams.get("store_code"), "store-03");

  setActiveStoreCode("current");
  assert.equal(productThumbnailUrl(""), "");
  assert.equal(productThumbnailUrl(null), "");
});
