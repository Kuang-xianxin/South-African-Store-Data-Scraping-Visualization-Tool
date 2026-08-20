import type { CompetitorBatchStatus } from "./api";

type BatchOptionControlStatus = Pick<
  CompetitorBatchStatus,
  "active" | "owner_username" | "source"
>;

type ScheduledBatchResumeStatus = Pick<
  CompetitorBatchStatus,
  | "active"
  | "batch_id"
  | "event"
  | "source"
  | "scheduled_resume_available"
  | "scheduled_resume_pending"
>;

type BatchIdentityStatus = Pick<CompetitorBatchStatus, "active" | "batch_id">;

type ScheduledRetryWaitStatus = Pick<
  CompetitorBatchStatus,
  | "active"
  | "event"
  | "pending"
  | "scheduled_auto_resume_at"
  | "scheduled_retry_round"
  | "scheduled_retry_round_limit"
  | "scheduled_wait_kind"
  | "source"
>;

export interface LocalCollectionCheckpointResumeState {
  batchId?: string;
  running?: boolean;
  activeIndex?: number | null;
  activeRequestId?: string | null;
  autoResumeAt?: string | null;
  stopReason: string;
  savedAt: string;
}

export const SHARED_BATCH_CHECKPOINT_YIELD_REASON =
  "旧浏览器断点已让位给另一个共享批次；数据已保留且不会自动重启，待共享批次结束后可手动继续。";

export function activeSharedBatchSupersedesLocalCheckpoint(
  localBatchId: string | null | undefined,
  status: BatchIdentityStatus,
): boolean {
  return Boolean(
    status.active
    && status.batch_id
    && status.batch_id !== localBatchId,
  );
}

export function suspendCheckpointForActiveSharedBatch<
  T extends LocalCollectionCheckpointResumeState,
>(
  checkpoint: T,
  status: BatchIdentityStatus,
  savedAt: string,
): T {
  if (
    !activeSharedBatchSupersedesLocalCheckpoint(checkpoint.batchId, status)
  ) {
    return checkpoint;
  }
  if (
    checkpoint.running === false
    && checkpoint.activeIndex == null
    && checkpoint.activeRequestId == null
    && checkpoint.autoResumeAt == null
    && checkpoint.stopReason === SHARED_BATCH_CHECKPOINT_YIELD_REASON
  ) {
    return checkpoint;
  }
  return {
    ...checkpoint,
    running: false,
    activeIndex: null,
    activeRequestId: null,
    autoResumeAt: null,
    stopReason: SHARED_BATCH_CHECKPOINT_YIELD_REASON,
    savedAt,
  };
}

export function canUpdateVisibleBrowserForBatch(
  canControlCollection: boolean,
  currentUsername: string | null | undefined,
  status: BatchOptionControlStatus,
): boolean {
  if (!canControlCollection) return false;
  if (!status.active || status.source === "scheduled") return true;
  return Boolean(
    currentUsername
    && status.owner_username
    && currentUsername.toLowerCase() === status.owner_username.toLowerCase(),
  );
}

export function stoppedScheduledBatchResumeCount(
  canControlCollection: boolean,
  status: ScheduledBatchResumeStatus,
): number {
  if (
    !canControlCollection
    || status.active
    || status.source !== "scheduled"
    || !["manual_stop", "completed"].includes(status.event)
    || !status.batch_id
    || !status.scheduled_resume_available
  ) {
    return 0;
  }
  return Math.max(0, status.scheduled_resume_pending ?? 0);
}

export function scheduledRetryWaitLabel(
  status: ScheduledRetryWaitStatus,
  nowMs: number,
): string | null {
  if (
    !status.active
    || status.source !== "scheduled"
    || status.event !== "scheduled_pause"
    || status.scheduled_wait_kind !== "pending_retry"
  ) {
    return null;
  }
  const retryRound = Math.max(0, status.scheduled_retry_round ?? 0);
  const retryLimit = Math.max(0, status.scheduled_retry_round_limit ?? 0);
  const roundLabel = retryRound && retryLimit
    ? `等待第 ${retryRound}/${retryLimit} 轮安全重试`
    : "等待安全重试";
  const pendingLabel = `待重试 ${Math.max(0, status.pending)} 条`;
  const resumeAt = Date.parse(status.scheduled_auto_resume_at ?? "");
  if (!Number.isFinite(resumeAt)) return `${roundLabel} · ${pendingLabel}`;
  const remainingSeconds = Math.max(0, Math.ceil((resumeAt - nowMs) / 1_000));
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  const countdown = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${roundLabel} · ${pendingLabel} · 距离自动续爬 ${countdown}`;
}
