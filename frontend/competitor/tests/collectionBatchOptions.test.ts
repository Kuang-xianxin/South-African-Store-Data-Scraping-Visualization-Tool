import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  SHARED_BATCH_CHECKPOINT_YIELD_REASON,
  activeSharedBatchSupersedesLocalCheckpoint,
  canResumeScheduledNetworkPause,
  canUpdateVisibleBrowserForBatch,
  scheduledRetryWaitLabel,
  stoppedScheduledBatchResumeCount,
  suspendCheckpointForActiveSharedBatch,
} from "../src/collectionBatchOptions.ts";

const competitorsPageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

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

test("only a controller sees a resumable scheduled checkpoint count", () => {
  const status = {
    active: false,
    batch_id: "scheduled-20260812-example",
    event: "manual_stop",
    source: "scheduled" as const,
    scheduled_resume_available: true,
    scheduled_resume_pending: 51,
  };
  assert.equal(stoppedScheduledBatchResumeCount(true, status), 51);
  assert.equal(
    stoppedScheduledBatchResumeCount(true, { ...status, event: "completed" }),
    51,
  );
  assert.equal(stoppedScheduledBatchResumeCount(false, status), 0);
  assert.equal(
    stoppedScheduledBatchResumeCount(true, { ...status, active: true }),
    0,
  );
  assert.equal(
    stoppedScheduledBatchResumeCount(true, {
      ...status,
      scheduled_resume_available: false,
    }),
    0,
  );
});

test("scheduled retry wait shows round, pending count, and live countdown", () => {
  const status = {
    active: true,
    source: "scheduled" as const,
    event: "scheduled_pause",
    pending: 24,
    scheduled_wait_kind: "pending_retry" as const,
    scheduled_auto_resume_at: "2026-08-19T02:10:00.000Z",
    scheduled_retry_round: 2,
    scheduled_retry_round_limit: 3,
  };

  assert.equal(
    scheduledRetryWaitLabel(status, Date.parse("2026-08-19T02:02:33.000Z")),
    "等待第 2/3 轮安全重试 · 待重试 24 条 · 距离自动续爬 07:27",
  );
  assert.equal(
    scheduledRetryWaitLabel(
      { ...status, event: "progress" },
      Date.parse("2026-08-19T02:02:33.000Z"),
    ),
    null,
  );
});

test("only kxx sees manual continue during a scheduled network pause", () => {
  const status = {
    active: true,
    batch_id: "scheduled-20260901-example",
    event: "scheduled_pause",
    source: "scheduled" as const,
    scheduled_wait_kind: "network" as const,
    scheduled_network_resume_available: true,
  };

  assert.equal(canResumeScheduledNetworkPause(true, status), true);
  assert.equal(canResumeScheduledNetworkPause(false, status), false);
  assert.equal(
    canResumeScheduledNetworkPause(true, {
      active: status.active,
      batch_id: status.batch_id,
      event: status.event,
      source: status.source,
      scheduled_wait_kind: status.scheduled_wait_kind,
    }),
    false,
  );
  assert.equal(
    canResumeScheduledNetworkPause(true, {
      ...status,
      scheduled_wait_kind: "pending_retry",
    }),
    false,
  );
  assert.equal(
    canResumeScheduledNetworkPause(true, {
      ...status,
      scheduled_network_resume_available: false,
    }),
    false,
  );
});

test("a different active shared batch supersedes browser-local automatic resume", () => {
  const activeScheduledBatch = {
    active: true,
    batch_id: "scheduled-20260818-current",
  };
  assert.equal(
    activeSharedBatchSupersedesLocalCheckpoint(
      "batch-old-browser-checkpoint",
      activeScheduledBatch,
    ),
    true,
  );
  assert.equal(
    activeSharedBatchSupersedesLocalCheckpoint("", activeScheduledBatch),
    true,
  );
  assert.equal(
    activeSharedBatchSupersedesLocalCheckpoint(
      activeScheduledBatch.batch_id,
      activeScheduledBatch,
    ),
    false,
  );
  assert.equal(
    activeSharedBatchSupersedesLocalCheckpoint(
      "batch-old-browser-checkpoint",
      { ...activeScheduledBatch, active: false },
    ),
    false,
  );
  assert.equal(
    activeSharedBatchSupersedesLocalCheckpoint(
      "batch-old-browser-checkpoint",
      { active: true, batch_id: null },
    ),
    false,
  );
});

test("superseded local checkpoint remains available only for explicit resume", () => {
  const checkpoint = {
    batchId: "batch-old-browser-checkpoint",
    running: true,
    activeIndex: 30,
    activeRequestId: "request-old-browser",
    autoResumeAt: "2026-08-18T10:27:00.000Z",
    stopReason: "网络或 Takealot 临时服务异常，系统将在10分钟后自动继续。",
    savedAt: "2026-08-18T10:17:00.000Z",
    untouchedQueue: [31, 32, 33],
  };
  const status = {
    active: true,
    batch_id: "scheduled-20260818-current",
  };
  const suspended = suspendCheckpointForActiveSharedBatch(
    checkpoint,
    status,
    "2026-08-18T10:18:00.000Z",
  );

  assert.notEqual(suspended, checkpoint);
  assert.equal(suspended.running, false);
  assert.equal(suspended.activeIndex, null);
  assert.equal(suspended.activeRequestId, null);
  assert.equal(suspended.autoResumeAt, null);
  assert.equal(suspended.stopReason, SHARED_BATCH_CHECKPOINT_YIELD_REASON);
  assert.equal(suspended.savedAt, "2026-08-18T10:18:00.000Z");
  assert.deepEqual(suspended.untouchedQueue, [31, 32, 33]);
  assert.equal(checkpoint.running, true);
  assert.equal(
    suspendCheckpointForActiveSharedBatch(
      suspended,
      status,
      "2026-08-18T10:19:00.000Z",
    ),
    suspended,
  );
  assert.equal(
    suspendCheckpointForActiveSharedBatch(
      checkpoint,
      { active: true, batch_id: checkpoint.batchId },
      "2026-08-18T10:19:00.000Z",
    ),
    checkpoint,
  );
});

test("competitor admin control resumes the server checkpoint instead of starting over", () => {
  assert.match(competitorsPageSource, /resumeStoppedScheduledCompetitorBatch/);
  assert.match(competitorsPageSource, /继续 09:00 自动批次/);
  assert.match(competitorsPageSource, /stoppedScheduledResumeCount === 0/);
  assert.equal(
    competitorsPageSource.match(/&& stoppedScheduledResumeCount === 0/g)?.length,
    3,
  );
  assert.match(
    competitorsPageSource,
    /已有可续服务端断点，请先继续同一批次/,
  );
  assert.match(
    competitorsPageSource,
    /继续原批次待重试项/,
  );
  assert.match(competitorsPageSource, /全员同步等待重试/);
  assert.match(competitorsPageSource, /正在等待安全复核间隔，届时自动续爬/);
  assert.match(competitorsPageSource, /@click="resumePausedScheduledCollection"/);
  assert.match(competitorsPageSource, /立即继续采集/);
  assert.match(competitorsPageSource, /原自动续爬倒计时已取消/);
  assert.doesNotMatch(competitorsPageSource, /手动提前继续网络暂停自动批次/);
  assert.match(competitorsPageSource, /showLocalCollectionAlert/);
  assert.match(competitorsPageSource, /suspendStoredCollectionCheckpoint/);
  assert.match(competitorsPageSource, /v-if="showLocalCollectionAlert"/);
  assert.doesNotMatch(competitorsPageSource, /本轮自动续爬暂缓/);
});
