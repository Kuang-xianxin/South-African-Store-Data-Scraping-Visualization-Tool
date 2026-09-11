import assert from "node:assert/strict";
import test from "node:test";
import { LiveUpdateQueue } from "../src/liveUpdateQueue.ts";

function harness(refresh: () => Promise<boolean | void> = async () => {}) {
  let now = 0;
  let id = 0;
  const timers = new Map<number, { due: number; callback: () => void }>();
  const state = { available: true, editing: false, busy: false, calls: 0, status: "" };
  const queue = new LiveUpdateQueue({
    refresh: async () => { state.calls++; return refresh(); },
    available: () => state.available,
    busy: () => state.busy,
    editing: () => state.editing,
    status: (message) => { state.status = message; },
    now: () => now,
    setTimer: ((callback: () => void, delay: number) => {
      timers.set(++id, { due: now + delay, callback }); return id;
    }) as unknown as typeof setTimeout,
    clearTimer: ((handle: number) => { timers.delete(handle); }) as unknown as typeof clearTimeout,
  });
  const advance = async (ms: number) => {
    const end = now + ms;
    for (;;) {
      const next = [...timers].sort((a, b) => a[1].due - b[1].due)[0];
      if (!next || next[1].due > end) break;
      now = next[1].due; timers.delete(next[0]); next[1].callback();
      for (let n = 0; n < 10; n++) await Promise.resolve();
    }
    now = end;
  };
  queue.observe("store-one", "first", true);
  return { queue, state, advance, timers };
}

test("unchanged versions perform no reads; bursts merge into one refresh", async () => {
  const { queue, state, advance } = harness();
  queue.observe("store-one", "first"); await advance(60_000);
  assert.equal(state.calls, 0);
  queue.observe("store-one", "second"); queue.observe("store-one", "third");
  await advance(1_500); assert.equal(state.calls, 1);
  queue.observe("store-one", "third"); await advance(60_000); assert.equal(state.calls, 1);
});

test("hidden or inactive pages retain changes without issuing requests", async () => {
  const { queue, state, advance, timers } = harness();
  state.available = false; queue.observe("store-one", "new");
  await advance(60_000); assert.equal(state.calls, 0); assert.equal(timers.size, 0);
  state.available = true; queue.wake(); await advance(1_500); assert.equal(state.calls, 1);
});

test("edits and existing requests defer refresh without dropping the change", async () => {
  const { queue, state, advance } = harness();
  state.editing = true; queue.observe("store-one", "new");
  await advance(10_000); assert.equal(state.calls, 0); assert.match(state.status, /完成编辑/);
  state.editing = false; state.busy = true; await advance(10_000); assert.equal(state.calls, 0);
  state.busy = false; await advance(2_000); assert.equal(state.calls, 1);
});

test("an update arriving during a request is refreshed afterwards without overlap", async () => {
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  const { queue, state, advance } = harness(() => pending);
  queue.observe("store-one", "second"); await advance(1_500); assert.equal(state.calls, 1);
  queue.observe("store-one", "third"); await advance(60_000); assert.equal(state.calls, 1);
  finish(); for (let n = 0; n < 10; n++) await Promise.resolve();
  await advance(1_500); assert.equal(state.calls, 2);
});

test("failed refreshes retain old data and retry with backoff", async () => {
  let fail = true;
  const { queue, state, advance } = harness(async () => !fail);
  queue.observe("store-one", "new"); await advance(1_500);
  assert.equal(state.calls, 1); assert.match(state.status, /稍后重试/);
  await advance(20_000); assert.equal(state.calls, 1);
  fail = false; await advance(60_000); assert.equal(state.calls, 2); assert.equal(state.status, "");
});

test("switching scope during a request does not acknowledge the new scope", async () => {
  let finish!: () => void;
  const { queue, state, advance } = harness(() => new Promise<void>((resolve) => { finish = resolve; }));
  queue.observe("store-one", "new"); await advance(1_500);
  queue.observe("store-two", "different"); finish();
  for (let n = 0; n < 10; n++) await Promise.resolve();
  await advance(15_000); assert.equal(state.calls, 2); queue.dispose();
});

test("disposing a page cancels its queued refresh", async () => {
  const { queue, state, advance, timers } = harness();
  queue.observe("store-one", "new"); queue.dispose();
  await advance(60_000); assert.equal(state.calls, 0); assert.equal(timers.size, 0);
});

test("returning from a detail view catches up the list only when its data changed", async () => {
  const { queue, state, advance } = harness();
  queue.observe("detail", "first", true);
  queue.observe("detail", "new"); await advance(1_500);
  assert.equal(state.calls, 1);
  queue.observe("store-one", "new"); await advance(15_000);
  assert.equal(state.calls, 2);
  queue.observe("detail", "new"); await advance(60_000);
  assert.equal(state.calls, 2);
});
