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
