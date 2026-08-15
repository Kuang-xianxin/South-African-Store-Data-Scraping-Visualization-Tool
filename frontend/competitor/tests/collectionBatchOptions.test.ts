import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  canUpdateVisibleBrowserForBatch,
  stoppedScheduledBatchResumeCount,
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
    /不会重新读取清单、创建新批次或重采本批已成功、已确认失效链接/,
  );
  assert.match(competitorsPageSource, /全员同步等待重试/);
  assert.match(competitorsPageSource, /正在等待安全复核间隔，届时自动续爬/);
});
