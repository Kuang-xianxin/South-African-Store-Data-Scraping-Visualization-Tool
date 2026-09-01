import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

import { formatCollectionTaskMessage } from "../src/collectionTaskMessages.ts";

const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

test("collection task details keep retry and confirmed-invalid rows separate", () => {
  assert.match(apiSource, /terminal_errors:/);
  assert.match(apiSource, /terminal_error_count\?: number/);
  assert.match(apiSource, /terminal_error_page/);
  assert.match(pageSource, /<strong>待重试任务<\/strong>/);
  assert.match(pageSource, /<strong>确认失效任务<\/strong>/);
  assert.match(pageSource, /v-for="error in visibleDisplayedCollectionErrors"/);
  assert.match(
    pageSource,
    /v-for="error in visibleDisplayedCollectionTerminalErrors"/,
  );
  assert.match(pageSource, /已排除自动重试/);
  assert.match(pageSource, /本批不会自动重试/);
});

test("scheduled continuous collection omits the legacy daily task label", () => {
  assert.doesNotMatch(pageSource, /每日 09:00 自动任务/);
  assert.match(
    pageSource,
    /<template v-if="sharedBatchStatus\.source !== 'scheduled'">\s*· \{\{ sharedBatchOwnerLabel \}\}\s*<\/template>/,
  );
});

test("collection task stock summary uses explicit variant and seller quote wording", () => {
  assert.equal(
    formatCollectionTaskMessage(
      "商品与评论快照已保存，但 1/4 个变体/卖家报价库存仍未探测；失败原因：超时",
    ),
    "商品与评论快照已保存，有1个变体/4个卖家报价，其中1个报价库存仍未探测；失败原因：超时",
  );
  assert.equal(formatCollectionTaskMessage("普通失败"), "普通失败");
  assert.equal(
    pageSource.match(/formatCollectionTaskMessage\(error\.message\)/g)?.length,
    2,
  );
});

type DetailQuery = {
  resultPage: number;
  errorPage: number;
  terminalErrorPage: number;
  pageSize: number;
};

const savedStatus = () => ({
  batch_id: "saved-batch",
  active: false,
  results: [{ plid: "101" }],
});

type TestStatus = ReturnType<typeof savedStatus>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function functionSource(source: string, name: string): string {
  const script = source.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)?.[1] ?? source;
  const parsed = ts.createSourceFile("source.ts", script, ts.ScriptTarget.ESNext, true);
  const declaration = parsed.statements.find(
    (statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === name,
  );
  assert.ok(declaration, `Missing production function ${name}`);
  return declaration.getText(parsed);
}

function detailRefreshHarness(
  fetchStatus: (detail?: DetailQuery, signal?: AbortSignal) => Promise<TestStatus>,
) {
  const state = {
    collectionDetailsOpen: { value: true },
    collectionDetailsLoading: { value: false },
    collectionDetailsError: { value: "" },
    collectionResultPage: { value: 1 },
    collectionErrorPage: { value: 1 },
    collectionTerminalErrorPage: { value: 1 },
    sharedBatchStatus: { value: savedStatus() },
  };
  const compiled = ts.transpileModule(
    `let sharedBatchStatusRequestId = 0;
     let sharedBatchStatusController = null;
     let sharedBatchTimer = null;
     ${functionSource(pageSource, "isAbortError")}
     ${functionSource(pageSource, "loadSharedBatchStatus")}
     ${functionSource(pageSource, "detachCollectionForSessionChange")}
     ({ refresh: loadSharedBatchStatus, detach: detachCollectionForSessionChange });`,
    { compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.None } },
  ).outputText;
  const actions = runInNewContext(compiled, {
    ...state,
    AbortController,
    DOMException,
    Error,
    collectionDetailPageSize: 50,
    fetchCompetitorBatchStatus: fetchStatus,
    suspendLoadedCollectionCheckpoint: () => {},
    readCollectionCheckpoint: () => null,
    mergeQueuedTargetsIntoLocalBatch: () => {},
    collecting: { value: false },
    collectionPreparing: { value: false },
    window: { clearInterval: () => {} },
  }) as {
    refresh: (includeDetails?: boolean, background?: boolean) => Promise<void>;
    detach: () => void;
  };
  return { state, ...actions };
}

test("background detail failures preserve rows and a stable error across retries", async () => {
  let pending = deferred<TestStatus>();
  const harness = detailRefreshHarness(() => pending.promise);
  const previousStatus = harness.state.sharedBatchStatus.value;
  const message = "无法连接本机 ERP 服务，请确认服务正在运行";

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const refresh = harness.refresh(true, true);
    assert.equal(harness.state.collectionDetailsLoading.value, false);
    assert.equal(harness.state.collectionDetailsError.value, attempt ? message : "");
    pending.reject(new Error(message));
    await refresh;
    assert.equal(harness.state.collectionDetailsError.value, message);
    assert.equal(harness.state.sharedBatchStatus.value, previousStatus);
    pending = deferred<TestStatus>();
  }

  const recovered = { ...savedStatus(), results: [{ plid: "102" }] };
  const refresh = harness.refresh(true, true);
  assert.equal(harness.state.collectionDetailsError.value, message);
  assert.equal(harness.state.collectionDetailsLoading.value, false);
  pending.resolve(recovered);
  await refresh;
  assert.equal(harness.state.collectionDetailsError.value, "");
  assert.equal(harness.state.sharedBatchStatus.value, recovered);
});

test("slow background detail requests do not overlap the next polling tick", async () => {
  const pending = deferred<TestStatus>();
  let requests = 0;
  const harness = detailRefreshHarness(() => {
    requests += 1;
    return pending.promise;
  });

  const first = harness.refresh(true, true);
  const second = harness.refresh(true, true);
  assert.equal(requests, 1);
  pending.resolve(savedStatus());
  await Promise.all([first, second]);
  await harness.refresh(true, true);
  assert.equal(requests, 2);
});

for (const staleResult of ["success", "failure"] as const) {
  test(`a foreground page request wins over an older background ${staleResult}`, async () => {
    const older = deferred<TestStatus>();
    const newer = deferred<TestStatus>();
    const requests: Array<{ detail?: DetailQuery; signal?: AbortSignal }> = [];
    const harness = detailRefreshHarness((detail, signal) => {
      requests.push({ detail, signal });
      return requests.length === 1 ? older.promise : newer.promise;
    });
    const background = harness.refresh(true, true);
    harness.state.collectionResultPage.value = 2;
    const foreground = harness.refresh(true);
    assert.equal(harness.state.collectionDetailsLoading.value, true);
    assert.equal(requests[0]?.signal?.aborted, true);
    assert.equal(requests[1]?.detail?.resultPage, 2);

    if (staleResult === "success") {
      older.resolve({ ...savedStatus(), results: [{ plid: "stale" }] });
      await background;
      assert.equal(harness.state.collectionDetailsLoading.value, true);
      assert.equal(harness.state.sharedBatchStatus.value.results[0]?.plid, "101");
    }
    const latestStatus = { ...savedStatus(), results: [{ plid: "202" }] };
    newer.resolve(latestStatus);
    await foreground;
    if (staleResult === "failure") older.reject(new Error("late connection failure"));
    await background;
    assert.equal(harness.state.sharedBatchStatus.value, latestStatus);
    assert.equal(harness.state.collectionDetailsError.value, "");
    assert.equal(harness.state.collectionDetailsLoading.value, false);
  });
}

test("leaving the page cancels status reads without replacing the saved batch", async () => {
  const pending = deferred<TestStatus>();
  let requestSignal: AbortSignal | undefined;
  const harness = detailRefreshHarness((_detail, signal) => {
    requestSignal = signal;
    return pending.promise;
  });
  const previousStatus = harness.state.sharedBatchStatus.value;
  const refresh = harness.refresh(true);
  harness.detach();
  assert.equal(requestSignal?.aborted, true);
  pending.resolve({ ...savedStatus(), results: [{ plid: "late" }] });
  await refresh;
  assert.equal(harness.state.sharedBatchStatus.value, previousStatus);
  assert.equal(harness.state.collectionDetailsLoading.value, false);
  assert.equal(harness.state.collectionDetailsError.value, "");
});

test("the polling timer uses background mode and the API forwards read cancellation", async () => {
  assert.ok(
    /\(\) => void loadSharedBatchStatus\(undefined, true\)/.test(pageSource),
    "The two-second timer must use background refresh mode",
  );
  let requestOptions: RequestInit | undefined;
  const fetchStatus = runInNewContext(
    ts.transpileModule(
      `${functionSource(apiSource, "fetchCompetitorBatchStatus")}\nfetchCompetitorBatchStatus;`,
      { compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.CommonJS } },
    ).outputText,
    {
      exports: {},
      URLSearchParams,
      request: async (_url: string, options?: RequestInit) => {
        requestOptions = options;
        return savedStatus();
      },
    },
  ) as (detail?: DetailQuery, signal?: AbortSignal) => Promise<TestStatus>;
  const controller = new AbortController();
  await fetchStatus(undefined, controller.signal);
  assert.equal(requestOptions?.signal, controller.signal);
});
