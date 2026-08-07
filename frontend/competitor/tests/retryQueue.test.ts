import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_AUTOMATIC_RETRY_ATTEMPTS,
  mergeUniqueTargetUrls,
  retryGapForAttempt,
  scheduleRetryAfterGap,
} from "../src/retryQueue.ts";

test("unified collection keeps true competitors first and deduplicates own PLIDs", () => {
  assert.deepEqual(
    mergeUniqueTargetUrls(
      [
        "https://www.takealot.com/competitor/PLID11111111",
        "https://www.takealot.com/competitor/PLID22222222",
      ],
      [
        "https://www.takealot.com/p/PLID22222222",
        "https://www.takealot.com/p/PLID33333333",
      ],
    ),
    [
      "https://www.takealot.com/competitor/PLID11111111",
      "https://www.takealot.com/competitor/PLID22222222",
      "https://www.takealot.com/p/PLID33333333",
    ],
  );
});

test("retry gaps grow exponentially by intervening task count", () => {
  assert.equal(MAX_AUTOMATIC_RETRY_ATTEMPTS, 3);
  assert.deepEqual(
    [1, 2, 3].map(retryGapForAttempt),
    [1, 2, 4],
  );
});

test("a retry is inserted only after the requested number of pending tasks", () => {
  const firstQueue = ["next-1", "next-2", "next-3"];
  assert.deepEqual(
    scheduleRetryAfterGap(firstQueue, 0, "retry-1", 1),
    { scheduled: true, gap: 1, position: 1 },
  );
  assert.deepEqual(firstQueue, ["next-1", "retry-1", "next-2", "next-3"]);

  const secondQueue = ["done", "next-1", "next-2", "next-3"];
  assert.deepEqual(
    scheduleRetryAfterGap(secondQueue, 1, "retry-2", 2),
    { scheduled: true, gap: 2, position: 3 },
  );
  assert.deepEqual(secondQueue, ["done", "next-1", "next-2", "retry-2", "next-3"]);
});

test("a retry remains pending when the batch cannot satisfy its gap", () => {
  const queue = ["last-task"];
  assert.deepEqual(
    scheduleRetryAfterGap(queue, 0, "retry-4", 2),
    { scheduled: false, gap: 2, position: null },
  );
  assert.deepEqual(queue, ["last-task"]);
});
