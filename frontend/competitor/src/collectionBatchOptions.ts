import type { CompetitorBatchStatus } from "./api";

type BatchOptionControlStatus = Pick<
  CompetitorBatchStatus,
  "active" | "owner_username" | "source"
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
