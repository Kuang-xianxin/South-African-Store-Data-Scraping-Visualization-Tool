import assert from "node:assert/strict";
import test from "node:test";

import {
  CSS_ASSET_CACHE_EPOCH,
  frontendAssetFileName,
} from "../vite.config.ts";

test("all CSS assets receive the explicit cache epoch", () => {
  assert.equal(CSS_ASSET_CACHE_EPOCH, "css-mime-v2");
  assert.equal(
    frontendAssetFileName({ name: "OverviewPage.css" }),
    "assets/[name]-[hash]-css-mime-v2[extname]",
  );
  assert.equal(
    frontendAssetFileName({ names: ["CompetitorsPage.css"] }),
    "assets/[name]-[hash]-css-mime-v2[extname]",
  );
});

test("non-CSS assets keep the normal content-hashed pattern", () => {
  assert.equal(
    frontendAssetFileName({ name: "OverviewPage.js" }),
    "assets/[name]-[hash][extname]",
  );
  assert.equal(
    frontendAssetFileName({ name: "brand.svg" }),
    "assets/[name]-[hash][extname]",
  );
});
