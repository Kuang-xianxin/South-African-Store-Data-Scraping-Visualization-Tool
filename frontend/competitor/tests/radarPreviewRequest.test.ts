import assert from "node:assert/strict";
import test from "node:test";
import { readRadarWithPreview } from "../src/radarPreviewRequest.ts";

test("complete preview is visible before the fresh response finishes", async () => {
  const calls: boolean[] = [];
  const visible: number[] = [];
  let finish!: (value: { value: number; refreshing: boolean; generatedAt: string }) => void;
  const pending = readRadarWithPreview(async (preferCached) => {
    calls.push(preferCached);
    if (preferCached) return { value: 1, refreshing: true, generatedAt: "2026-09-07" };
    return new Promise((resolve) => { finish = resolve; });
  }, (value) => visible.push(value), () => true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(visible, [1]);
  assert.deepEqual(calls, [true, false]);
  finish({ value: 2, refreshing: false, generatedAt: "2026-09-07" });
  assert.equal(await pending, 2);
});

test("fresh read does not issue another request", async () => {
  let calls = 0;
  const value = await readRadarWithPreview(async () => {
    calls++;
    return { value: 3, refreshing: false, generatedAt: "" };
  }, () => assert.fail("fresh response is not an old preview"), () => true);
  assert.equal(value, 3);
  assert.equal(calls, 1);
});

test("session changes cannot display a previous user's preview", async () => {
  await assert.rejects(readRadarWithPreview(async () => ({
    value: "private", refreshing: true, generatedAt: "",
  }), () => assert.fail("must not display old user data"), () => false), { name: "AbortError" });
});

test("failed update keeps the displayed preview and reports the failure", async () => {
  const visible: string[] = [];
  await assert.rejects(readRadarWithPreview(async (preferCached) => {
    if (preferCached) return { value: "old", refreshing: true, generatedAt: "" };
    throw new Error("offline");
  }, (value) => visible.push(value), () => true), /offline/);
  assert.deepEqual(visible, ["old"]);
});
