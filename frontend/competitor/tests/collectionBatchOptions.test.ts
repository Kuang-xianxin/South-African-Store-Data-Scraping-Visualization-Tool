import assert from "node:assert/strict";
import test from "node:test";

import { canUpdateVisibleBrowserForBatch } from "../src/collectionBatchOptions.ts";

test("kxx controller can update the server-owned scheduled batch", () => {
  assert.equal(
    canUpdateVisibleBrowserForBatch(true, "kxx", {
      active: true,
      owner_username: "scheduled-task",
      source: "scheduled",
    }),
    true,
  );
  assert.equal(
    canUpdateVisibleBrowserForBatch(false, "admin.two", {
      active: true,
      owner_username: "scheduled-task",
      source: "scheduled",
    }),
    false,
  );
});

test("manual batch options remain limited to the owning controller account", () => {
  const status = {
    active: true,
    owner_username: "kxx",
    source: "manual" as const,
  };
  assert.equal(canUpdateVisibleBrowserForBatch(true, "KXX", status), true);
  assert.equal(canUpdateVisibleBrowserForBatch(true, "another.admin", status), false);
  assert.equal(
    canUpdateVisibleBrowserForBatch(true, "kxx", {
      ...status,
      active: false,
    }),
    true,
  );
});
