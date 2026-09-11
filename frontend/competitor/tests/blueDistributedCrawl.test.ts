import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { computed, ref, shallowRef, createSSRApp } from "vue";
import * as vue from "vue";
import * as vueServer from "vue/server-renderer";
import { compileTemplate } from "@vue/compiler-sfc";
import * as blueHelpers from "../src/blueDistributedCrawl.ts";
import { blueBatchActive, blueBatchLabel, bluePendingBatchId, blueWorkerBacklog, isBlueDeployment, type BlueCrawlWorker,
  parseBlueTestPlids, readBluePending, type BlueCrawlBatch } from "../src/blueDistributedCrawl.ts";

test("full pending requests survive navigation with their exact idempotency identity", () => {
  const pending = { mode: "full" as const, request_id: "a".repeat(32), plids: [] };
  const restored = readBluePending(JSON.stringify(pending));
  assert.deepEqual(restored, pending);
  assert.equal(bluePendingBatchId(restored!), `blue-full-${pending.request_id}`);
  assert.equal(readBluePending(JSON.stringify({ ...pending, plids: ["100"] })), null);
  assert.equal(readBluePending("broken-json"), null);
});

test("test PLIDs retain integer precision, deduplicate, and reject malformed or oversized lists", () => {
  assert.deepEqual(parseBlueTestPlids("https://www.takealot.com/p/PLID1234567890123456789\nPLID100 100"),
    ["1234567890123456789", "100"]);
  assert.throws(() => parseBlueTestPlids("bad-link"));
  assert.throws(() => parseBlueTestPlids(Array.from({ length: 21 }, (_, i) => String(i + 1)).join("\n")));
});

test("green deployment stays outside the BLUE control and retry counts keep a batch active", () => {
  assert.equal(isBlueDeployment("blue-stage-laptop"), true);
  assert.equal(isBlueDeployment("green"), false);
  assert.equal(isBlueDeployment(null), false);
  const batch = { pending: 0, running: 0, retry: 1, succeeded: 2466, failed: 0, cancelled: 0 } as BlueCrawlBatch;
  assert.equal(blueBatchActive(batch), true);
  assert.equal(blueBatchLabel(batch), "等待重试");
  batch.retry = 0; batch.cancelled = 1;
  assert.equal(blueBatchActive(batch), false);
  assert.equal(blueBatchLabel(batch), "已停止");
});

test("saved results needing help remain visible even when the worker is idle", () => {
  const worker = { state: "idle", last_error: "durable-v1 full-v1 pending_uploads=2 blocked_uploads=1" } as BlueCrawlWorker;
  assert.equal(blueWorkerBacklog(worker), "2条结果待补交 · 1条结果需核对");
  assert.equal(blueWorkerBacklog(undefined), "");
});

async function componentHarness(overrides: Record<string, unknown> = {}) {
  const source = readFileSync(new URL("../src/components/BlueDistributedCrawl.vue", import.meta.url), "utf8")
    .split('<script setup lang="ts">')[1]!.split("</script>")[0]!;
  const code = ts.transpileModule(source + `\nexports.subject = {
    start, pending, canStart, control, refresh, activeBatch, batch, controlBatch, resumableBatch,
    expanded, selectedBatchId, stopConfirmId, available, preview, status, busy, fresh, loading,
    blockedReason, error, notice, nodes, items, itemLabels, toggleDetails,
    blueBatchLabel, blueWorkerLabel, blueWorkerBacklog, formatChinaDateTime, legacyActive: props.legacyActive,
  };`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const storage = new Map<string, string>();
  let mounted: () => Promise<void> = async () => {};
  const idle = () => ({ workers: [{ worker_id: "blue-main", online: 1 }],
    batches: [], items: [], full_crawl: true });
  class ApiRequestError extends Error { status = 500; }
  const api = { ApiRequestError, fetchBlueDeployment: async () => true,
    fetchBlueCrawlPreview: async () => ({ total: 31, competitors: 30, own: 1 }),
    fetchBlueCrawlStatus: async () => idle(), ...overrides };
  const result: { subject?: Record<string, any> } = {};
  runInNewContext(code, { exports: result, defineProps: () => ({ legacyActive: false, username: "kxx" }),
    defineEmits: () => () => {}, AbortController, crypto: webcrypto, document: { hidden: false },
    setTimeout: () => 1, clearTimeout: () => {},
    sessionStorage: { getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value), removeItem: (key: string) => storage.delete(key) },
    require: (name: string) => {
      if (name === "vue") return { computed, ref, shallowRef,
        onMounted: (callback: () => Promise<void>) => { mounted = callback; }, onBeforeUnmount: () => {} };
      if (name === "../api") return api;
      if (name === "../blueDistributedCrawl") return blueHelpers;
      if (name === "../time") return { formatChinaDateTime: () => "" };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  await mounted();
  return result.subject!;
}

test("rapid repeated clicks during preflight issue only one collection request", async () => {
  let release: () => void = () => {};
  const preflight = new Promise<void>(resolve => { release = resolve; });
  let reads = 0;
  let submissions = 0;
  const subject = await componentHarness({
    fetchBlueCrawlStatus: async () => {
      if (++reads === 2) await preflight;
      return { workers: [{ online: 1 }], batches: [], items: [], full_crawl: true };
    },
    startBlueCrawl: async () => { submissions += 1; return { batch_id: "accepted", total: 31 }; },
  });
  const first = subject.start();
  await subject.start();
  assert.equal(submissions, 0);
  release();
  await first;
  assert.equal(submissions, 1);
});

async function rendered(subject: Record<string, any>) {
  const source = readFileSync(new URL("../src/components/BlueDistributedCrawl.vue", import.meta.url), "utf8")
    .split("<template>")[1]!.split("</template>")[0]!;
  const compiled = compileTemplate({ source, filename: "BlueDistributedCrawl.vue", id: "blue-ui", ssr: true, ssrCssVars: [] });
  assert.deepEqual(compiled.errors, []);
  const code = ts.transpileModule(compiled.code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const result: { ssrRender?: (...args: any[]) => any } = {};
  runInNewContext(code, { exports: result, require: (name: string) => {
    if (name === "vue") return vue;
    assert.equal(name, "vue/server-renderer");
    return vueServer;
  } });
  return vueServer.renderToString(createSSRApp({ setup: () => subject, ssrRender: result.ssrRender }));
}

function batchFixture(batch_id: string, overrides: Record<string, unknown> = {}) {
  return { batch_id, total: 31, pending: "0", running: "0", retry: "0", succeeded: "31",
    failed: "0", cancelled: "0", updated_at: "2026-09-09T02:00:00Z", ...overrides };
}

test("collapsed unified controls stop the active batch even while inspecting old history", async () => {
  const active = batchFixture("blue-full-current", { pending: "20", running: "1", succeeded: "10" });
  const history = batchFixture("blue-full-old", { succeeded: "5", cancelled: "26" });
  const calls: unknown[] = [];
  const subject = await componentHarness({
    fetchBlueCrawlStatus: async () => ({ workers: [{ online: 1 }], batches: [active, history], items: [], full_crawl: true }),
    controlBlueCrawl: async (action: string, id: string) => {
      calls.push([action, id]);
      Object.assign(active, { pending: "0", running: "0", cancelled: "21" });
      return { message: "已停止" };
    },
  });
  subject.selectedBatchId.value = history.batch_id;
  assert.equal(subject.expanded.value, false);
  assert.equal(subject.controlBatch.value.batch_id, active.batch_id);
  const html = await rendered(subject);
  assert.match(html, />停止采集<\/button>/);
  assert.doesNotMatch(html, /id="blue-crawl-details"|开始新一轮采集|开始小批量双机测试/);
  await subject.control("resume", history.batch_id);
  assert.equal(calls.length, 0, "cannot resume history alongside an active batch");
  await subject.control("stop", subject.controlBatch.value.batch_id);
  assert.deepEqual(calls, [["stop", active.batch_id]]);
});

test("stopped batch resumes under its original identity and completed string counts never show resume", async () => {
  const batch = batchFixture("blue-full-preserved", { succeeded: "10", cancelled: "21" });
  const calls: unknown[] = [];
  const subject = await componentHarness({
    fetchBlueCrawlStatus: async () => ({ workers: [{ online: 1 }], batches: [batch], items: [], full_crawl: true }),
    controlBlueCrawl: async (action: string, id: string) => {
      calls.push([action, id]); Object.assign(batch, { pending: "21", cancelled: "0" });
      return { message: "已继续" };
    },
  });
  assert.match(await rendered(subject), /继续未完成（21）/);
  await subject.control("resume", subject.resumableBatch.value.batch_id);
  assert.deepEqual(calls, [["resume", batch.batch_id]]);
  assert.equal(subject.activeBatch.value.succeeded, "10");
  assert.equal(blueBatchLabel(subject.activeBatch.value), "等待领取");
  Object.assign(batch, { pending: "0", succeeded: "31" });
  await subject.refresh();
  assert.equal(subject.resumableBatch.value, undefined);
  assert.equal(blueBatchLabel(subject.batch.value), "已完成");
  assert.doesNotMatch(await rendered(subject), /继续未完成|停止采集/);
});

test("deployment is known before checkpoint restoration; BLUE and failed detection never auto-start legacy work", async () => {
  const page = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
  const start = page.indexOf("onMounted(async () => {");
  const end = page.indexOf("\nonBeforeUnmount", start);
  const callback = page.slice(start, end).replace("onMounted(async () => {", "exports.mount = async () => {").replace(/\}\);\s*$/, "};");
  const code = ts.transpileModule(callback, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  for (const deployment of [true, false, "error"] as const) {
    const calls: string[] = [];
    const result: { mount?: () => Promise<void> } = {};
    const context: Record<string, any> = { exports: result, props: {}, canViewGlobalManagement: ref(true),
      collectionDeploymentController: new AbortController(), blueCollectionAvailable: ref(false),
      collectionDeploymentReady: ref(false), collectionDeploymentError: ref(""),
      loading: ref(true), collecting: ref(false), restoredRunWasActive: ref(true), collectionClock: ref(0),
      sharedBatchTimer: null, batchHeartbeatTimer: null, collectionClockTimer: null,
      window: { addEventListener: () => {}, setInterval: () => 1 }, AUTH_SESSION_ENDING_EVENT: "end",
      fetchBlueDeployment: async () => { calls.push("deployment"); if (deployment === "error") throw Error(); return deployment; },
      readCollectionCheckpoint: () => { calls.push("checkpoint"); return { batchId: "old" }; },
    };
    for (const name of ["handleWindowKeydown", "closeCollectionClientChannel", "detachCollectionForSessionChange",
      "restoreCheckpointCollectionClientId", "ensureUniqueCollectionClientId", "loadOverview", "loadTargets",
      "loadSharedBatchStatus", "restoreCollectionCheckpoint", "resumeInterruptedCollection", "maybeAutoResumeScheduledCollection"]) {
      context[name] = () => { calls.push(name); };
    }
    runInNewContext(code, context);
    await result.mount!();
    assert.equal(calls.includes("loadOverview"), true);
    assert.equal(calls.includes("checkpoint"), deployment === false);
    assert.equal(calls.includes("resumeInterruptedCollection"), deployment === false);
    assert.equal(context.collectionDeploymentReady.value, deployment !== "error");
    if (deployment === false) assert.ok(calls.indexOf("deployment") < calls.indexOf("checkpoint"));
  }
});

test("a lost submission reply retries the saved request instead of generating a new batch", async () => {
  const identities: string[] = [];
  const subject = await componentHarness({ startBlueCrawl: async (request: { request_id: string }) => {
    identities.push(request.request_id);
    if (identities.length === 1) throw new Error("lost reply");
    return { batch_id: `blue-full-${request.request_id}`, total: 31 };
  } });
  await subject.start();
  assert.ok(subject.pending.value);
  await subject.start();
  assert.equal(identities.length, 2);
  assert.equal(identities[0], identities[1]);
  assert.equal(subject.pending.value, null);
});
