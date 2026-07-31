export const MAX_AUTOMATIC_RETRY_ATTEMPTS = 3;

export interface RetryScheduleResult {
  scheduled: boolean;
  gap: number;
  position: number | null;
}

export function retryGapForAttempt(attempt: number): number {
  if (!Number.isInteger(attempt) || attempt < 1) {
    throw new RangeError("Retry attempt must be a positive integer");
  }
  return 2 ** (attempt - 1);
}

export function scheduleRetryAfterGap<T>(
  queue: T[],
  cursor: number,
  item: T,
  attempt: number,
): RetryScheduleResult {
  const gap = retryGapForAttempt(attempt);
  const pendingCount = Math.max(0, queue.length - cursor);
  if (pendingCount < gap) {
    return { scheduled: false, gap, position: null };
  }
  const position = cursor + gap;
  queue.splice(position, 0, item);
  return { scheduled: true, gap, position };
}
