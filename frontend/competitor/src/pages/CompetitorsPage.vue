<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  ApiRequestError,
  collectCompetitor,
  fetchCompetitorBatchStatus,
  fetchCompetitorDetail,
  fetchCompetitorLinkHealth,
  fetchCompetitors,
  logCompetitorBatchEvent,
  type CompetitorBatchStatus,
} from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import type {
  CollectResult,
  CompetitorDateRange,
  CompetitorDetail,
  CompetitorItem,
  CompetitorLinkHealthItem,
} from "../types";
import { formatChinaDateTime } from "../time";

defineOptions({ name: "CompetitorsPage" });
const props = defineProps<{
  canOperate?: boolean;
  onPermissionDenied?: () => void;
}>();

const sampleUrls = [
  "https://www.takealot.com/laser-lipo-slimming-machine/PLID72189176",
  "https://www.takealot.com/multifunctional-led-modern-kitchen-sink-waterfall-push-button-te/PLID95526981",
  "https://www.takealot.com/adjustable-hinged-stabilizer-support-fitness-run-knee-brace/PLID96909926?size=Right",
  "https://www.takealot.com/cosmos-healing-enema-kit-medical-grade-silicone-2-litre/PLID94890093",
];

interface LinkValidationIssue {
  lineNumber: number;
  start: number;
  end: number;
  url: string;
  message: string;
}

interface CollectionQueueItem {
  index: number;
  url: string;
}

interface CollectionCheckpoint {
  version: 1 | 2 | 3 | 4 | 5;
  rawUrls: string;
  batchUrls: string[];
  attemptedIndexes: number[];
  failedIndexes: number[];
  terminalIndexes?: number[];
  results: CollectResult[];
  errors: string[];
  stopReason: string;
  withStockProbe: boolean;
  visibleBrowser: boolean;
  savedAt: string;
  batchId?: string;
  running?: boolean;
  activeIndex?: number | null;
  activeRequestId?: string | null;
  autoResumeAt?: string | null;
}

type CollectionRunMode =
  | "start"
  | "resume"
  | "auto_resume"
  | "scheduled_resume";

const collectionCheckpointKey = "takealot-competitor-collection-v1";
const collectionClientKey = "takealot-competitor-client-v1";
const automaticResumeDelayMs = 10 * 60 * 1_000;
const collectionClientId = restoreCollectionClientId();
const rawUrls = ref(sampleUrls.join("\n"));
const urlInput = ref<HTMLTextAreaElement | null>(null);
const linkValidationIssue = ref<LinkValidationIssue | null>(null);
const linkErrorPulse = ref(false);
const withStockProbe = ref(true);
const visibleBrowser = ref(false);
const competitors = ref<CompetitorItem[]>([]);
const selectedPlid = ref("");
const detail = ref<CompetitorDetail>({ history: [], reviews: [], variants: [] });
const detailModalOpen = ref(false);
const detailLoading = ref(false);
const detailError = ref("");
const loading = ref(true);
const collecting = ref(false);
const collectionClock = ref(Date.now());
const activeStartedAt = ref<number | null>(null);
const abortController = ref<AbortController | null>(null);
const completed = ref(0);
const total = ref(0);
const collectionResults = ref<CollectResult[]>([]);
const collectionErrors = ref<string[]>([]);
const collectionStopReason = ref("");
const collectionNoticeVersion = ref(0);
const batchUrls = ref<string[]>([]);
const attemptedIndexes = ref<number[]>([]);
const failedIndexes = ref<number[]>([]);
const terminalIndexes = ref<number[]>([]);
const batchId = ref("");
const activeIndex = ref<number | null>(null);
const activeRequestId = ref<string | null>(null);
const autoResumeAt = ref<number | null>(null);
const autoResumeAttempting = ref(false);
const restoredRunWasActive = ref(false);
const manualStopRequested = ref(false);
const sharedBatchStatus = ref<CompetitorBatchStatus>({
  active: false,
  batch_id: null,
  owner_username: null,
  owner_display_name: null,
  event: "idle",
  completed: 0,
  total: 0,
  pending: 0,
  succeeded: 0,
  failed: 0,
  terminal: 0,
  current_index: null,
  current_plid: null,
  current_request_id: null,
  reason: "",
  started_at: null,
  updated_at: null,
});
const linkHealth = ref<CompetitorLinkHealthItem[]>([]);
const pageError = ref("");
const reviewFilter = ref<"全部" | "好评" | "中评" | "差评">("全部");
const reviewStartDate = ref("");
const reviewEndDate = ref("");
const reviewSort = ref<
  "date_desc" | "date_asc" | "rating_desc" | "rating_asc"
>("date_desc");
const competitorQuery = ref("");
const competitorStockFilter = ref<"全部" | "有货" | "没货" | "未探测">("全部");
const competitorSignalFilter = ref("全部");
const rangeStartDate = ref("");
const rangeEndDate = ref("");
const appliedStartDate = ref("");
const appliedEndDate = ref("");
const competitorDateRange = ref<CompetitorDateRange>({
  available_start: null,
  available_end: null,
  selected_start: null,
  selected_end: null,
});
const failedCompetitorImages = ref<Set<string>>(new Set());

const selected = computed(
  () => competitors.value.find((item) => item.plid === selectedPlid.value) ?? null,
);
const sharedBatchMatchesCheckpoint = computed(
  () =>
    sharedBatchStatus.value.active
    && Boolean(batchId.value)
    && sharedBatchStatus.value.batch_id === batchId.value,
);
const anotherBatchIsActive = computed(
  () =>
    sharedBatchStatus.value.active
    && !collecting.value
    && !sharedBatchMatchesCheckpoint.value,
);
const sharedBatchOwner = computed(
  () =>
    sharedBatchStatus.value.owner_display_name
    || sharedBatchStatus.value.owner_username
    || "其他用户",
);
const competitorSignalOptions = computed(() =>
  [...new Set(competitors.value.map((item) => item.趋势判断).filter(Boolean))].sort(
    (first, second) => first.localeCompare(second, "zh-CN"),
  ),
);
const filteredCompetitors = computed(() => {
  const query = competitorQuery.value.trim().toLocaleLowerCase();
  return competitors.value.filter((item) => {
    if (
      query
      && ![
        item.商品,
        item.plid,
        item.当前卖家 ?? "",
        item.库存上限,
        item.趋势判断,
      ].some((value) => value.toLocaleLowerCase().includes(query))
    ) {
      return false;
    }
    if (
      competitorStockFilter.value !== "全部"
      && competitorStockState(item) !== competitorStockFilter.value
    ) {
      return false;
    }
    return (
      competitorSignalFilter.value === "全部"
      || item.趋势判断 === competitorSignalFilter.value
    );
  });
});
const competitorFiltersActive = computed(
  () =>
    Boolean(competitorQuery.value.trim())
    || competitorStockFilter.value !== "全部"
    || competitorSignalFilter.value !== "全部",
);
const exactStockCount = computed(
  () => competitors.value.filter((item) => item.库存精确).length,
);
const averageRating = computed(() => {
  const ratings = competitors.value
    .map((item) => item.评分)
    .filter((value): value is number => value !== null);
  if (!ratings.length) return "—";
  return (ratings.reduce((sum, value) => sum + value, 0) / ratings.length).toFixed(2);
});
const latestCollection = computed(() => {
  if (!competitors.value.length) return "尚未采集";
  return formatChinaDateTime(competitors.value[0].采集时间);
});
const activeRangeLabel = computed(() => {
  if (!appliedStartDate.value || !appliedEndDate.value) return "全部可用快照";
  return `${appliedStartDate.value} 至 ${appliedEndDate.value}`;
});
const reviewDates = computed(() =>
  detail.value.reviews
    .map((review) => reviewDateKey(review.评论日期))
    .filter((value): value is string => value !== null)
    .sort(),
);
const reviewMinDate = computed(() => reviewDates.value[0] ?? "");
const reviewMaxDate = computed(
  () => reviewDates.value[reviewDates.value.length - 1] ?? "",
);
const filteredReviews = computed(() => {
  const result = detail.value.reviews.filter((review) => {
    if (reviewFilter.value === "好评" && review.星级 < 4) return false;
    if (reviewFilter.value === "中评" && review.星级 !== 3) return false;
    if (reviewFilter.value === "差评" && review.星级 > 2) return false;

    const date = reviewDateKey(review.评论日期);
    if (reviewStartDate.value && (!date || date < reviewStartDate.value)) {
      return false;
    }
    if (reviewEndDate.value && (!date || date > reviewEndDate.value)) {
      return false;
    }
    return true;
  });
  return [...result].sort(compareReviews);
});
const progress = computed(() =>
  total.value ? Math.round((completed.value / total.value) * 100) : 0,
);
const successfulPlids = computed(
  () => new Set(collectionResults.value.map((result) => result.plid)),
);
const confirmedInvalidCount = computed(
  () =>
    linkHealth.value.filter((item) => item.status === "confirmed_invalid").length,
);
const suspectedInvalidCount = computed(
  () =>
    linkHealth.value.filter((item) => item.status === "suspected_invalid").length,
);
const resumeQueue = computed<CollectionQueueItem[]>(() => {
  if (!batchUrls.value.length) return [];
  const failedStart = failedIndexes.value.length
    ? Math.min(...failedIndexes.value)
    : Number.POSITIVE_INFINITY;
  const attempted = new Set(attemptedIndexes.value);
  const terminal = new Set(terminalIndexes.value);
  const firstUnattempted = batchUrls.value.findIndex(
    (_, index) => !attempted.has(index),
  );
  const start = Math.min(
    failedStart,
    firstUnattempted < 0 ? Number.POSITIVE_INFINITY : firstUnattempted,
  );
  if (!Number.isFinite(start)) return [];
  return batchUrls.value
    .map((url, index) => ({ index, url }))
    .filter(
      ({ index, url }) =>
        index >= start
        && !terminal.has(index)
        && !successfulPlids.value.has(plidFromUrl(url)),
    );
});
const pendingResumeCount = computed(() => resumeQueue.value.length);
const collectionNotices = computed(() =>
  collectionResults.value.filter((result) => result.message !== "采集成功"),
);
const activeCollectionStatus = computed(() => {
  void collectionClock.value;
  if (!collecting.value) return "";
  if (activeIndex.value === null) {
    return "正在登记采集任务或准备下一条商品，请稍候。";
  }
  const url = batchUrls.value[activeIndex.value] ?? "";
  const plid = plidFromUrl(url) || "未知";
  const elapsed = activeStartedAt.value === null
    ? 0
    : Math.max(0, Math.floor((Date.now() - activeStartedAt.value) / 1_000));
  return `正在检测第 ${activeIndex.value + 1}/${total.value} 条 · PLID${plid} · 已等待 ${elapsed} 秒`;
});
const activeCollectionHint = computed(() => {
  if (activeStartedAt.value === null) {
    return "正在建立任务状态，完成后会自动显示结果。";
  }
  const elapsed = Math.max(
    0,
    Math.floor((collectionClock.value - activeStartedAt.value) / 1_000),
  );
  return elapsed >= 90
    ? "当前商品耗时较长，系统可能正在枚举变体、探测库存或执行网络重试；可以继续等待，也可以点击“停止采集”保留断点。"
    : "单个商品的变体与库存探测通常需要几十秒，复杂商品可能需要1至3分钟。";
});
const autoResumeCountdown = computed(() => {
  if (autoResumeAt.value === null) return "";
  const remainingSeconds = Math.max(
    0,
    Math.ceil((autoResumeAt.value - collectionClock.value) / 1_000),
  );
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
});

onMounted(async () => {
  window.addEventListener("keydown", handleWindowKeydown);
  restoreCollectionCheckpoint();
  await Promise.all([loadOverview(), loadSharedBatchStatus()]);
  sharedBatchTimer = window.setInterval(
    () => void loadSharedBatchStatus(),
    2_000,
  );
  batchHeartbeatTimer = window.setInterval(() => {
    if (collecting.value) void recordBatchEvent("heartbeat");
  }, 10_000);
  collectionClockTimer = window.setInterval(() => {
    collectionClock.value = Date.now();
    void maybeAutoResumeScheduledCollection();
  }, 1_000);
  if (restoredRunWasActive.value) {
    void resumeInterruptedCollection();
  } else {
    void maybeAutoResumeScheduledCollection();
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleWindowKeydown);
  if (sharedBatchTimer !== null) window.clearInterval(sharedBatchTimer);
  if (batchHeartbeatTimer !== null) window.clearInterval(batchHeartbeatTimer);
  if (collectionClockTimer !== null) window.clearInterval(collectionClockTimer);
  document.body.style.overflow = "";
});

let sharedBatchTimer: number | null = null;
let batchHeartbeatTimer: number | null = null;
let collectionClockTimer: number | null = null;

let detailRequestId = 0;
watch([selectedPlid, appliedStartDate, appliedEndDate], async ([plid, start, end]) => {
  const requestId = ++detailRequestId;
  if (!plid) {
    detail.value = { history: [], reviews: [], variants: [] };
    detailLoading.value = false;
    detailError.value = "";
    return;
  }
  detailLoading.value = true;
  detailError.value = "";
  try {
    const result = await fetchCompetitorDetail(plid, start, end);
    if (requestId === detailRequestId) detail.value = result;
  } catch (error) {
    if (requestId === detailRequestId) {
      detailError.value = error instanceof Error ? error.message : "读取商品详情失败";
    }
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false;
  }
});

watch(detailModalOpen, (open) => {
  document.body.style.overflow = open ? "hidden" : "";
});

watch(reviewStartDate, (start) => {
  if (start && reviewEndDate.value && start > reviewEndDate.value) {
    reviewEndDate.value = start;
  }
});

watch(reviewEndDate, (end) => {
  if (end && reviewStartDate.value && end < reviewStartDate.value) {
    reviewStartDate.value = end;
  }
});

function reviewDateKey(value: string | null): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  const isoMatch = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (isoMatch) return `${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}`;

  const namedMatch = trimmed.match(
    /^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$/,
  );
  if (!namedMatch) return null;
  const month = {
    jan: 1,
    feb: 2,
    mar: 3,
    apr: 4,
    may: 5,
    jun: 6,
    jul: 7,
    aug: 8,
    sep: 9,
    oct: 10,
    nov: 11,
    dec: 12,
  }[namedMatch[2].slice(0, 3).toLowerCase()];
  const day = Number(namedMatch[1]);
  if (!month || day < 1 || day > 31) return null;
  return `${namedMatch[3]}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function compareReviewDates(
  first: string | null,
  second: string | null,
  ascending: boolean,
) {
  if (first === null && second === null) return 0;
  if (first === null) return 1;
  if (second === null) return -1;
  return ascending
    ? first.localeCompare(second)
    : second.localeCompare(first);
}

function compareReviews(
  first: CompetitorDetail["reviews"][number],
  second: CompetitorDetail["reviews"][number],
) {
  const firstDate = reviewDateKey(first.评论日期);
  const secondDate = reviewDateKey(second.评论日期);
  if (reviewSort.value === "date_asc") {
    return compareReviewDates(firstDate, secondDate, true);
  }
  if (reviewSort.value === "rating_desc") {
    return (
      second.星级 - first.星级 ||
      compareReviewDates(firstDate, secondDate, false)
    );
  }
  if (reviewSort.value === "rating_asc") {
    return (
      first.星级 - second.星级 ||
      compareReviewDates(firstDate, secondDate, false)
    );
  }
  return compareReviewDates(firstDate, secondDate, false);
}

function clearReviewDates() {
  reviewStartDate.value = "";
  reviewEndDate.value = "";
}

function competitorStockState(
  item: CompetitorItem,
): "有货" | "没货" | "未探测" {
  if (item.库存参考过期) return "未探测";
  if (item.库存数量 !== null) return item.库存数量 > 0 ? "有货" : "没货";
  const label = item.库存上限.trim();
  if (label.includes("没货") || label.includes("售罄")) return "没货";
  if (/\d/.test(label)) return "有货";
  return "未探测";
}

function canShowCompetitorImage(url: string | null | undefined): url is string {
  return Boolean(url && !failedCompetitorImages.value.has(url));
}

function competitorImageUrl(url: string | null | undefined): string {
  return canShowCompetitorImage(url)
    ? productThumbnailUrl(url, PRODUCT_IMAGE_SIZE.list)
    : "";
}

function markCompetitorImageFailed(url: string | null | undefined): void {
  if (!url || failedCompetitorImages.value.has(url)) return;
  failedCompetitorImages.value = new Set([
    ...failedCompetitorImages.value,
    url,
  ]);
}

function clearCompetitorFilters(): void {
  competitorQuery.value = "";
  competitorStockFilter.value = "全部";
  competitorSignalFilter.value = "全部";
}

async function applyDateRange(): Promise<void> {
  if (!rangeStartDate.value || !rangeEndDate.value) {
    pageError.value = "请选择完整的开始日期和结束日期";
    return;
  }
  if (rangeStartDate.value > rangeEndDate.value) {
    pageError.value = "开始日期不能晚于结束日期";
    return;
  }
  appliedStartDate.value = rangeStartDate.value;
  appliedEndDate.value = rangeEndDate.value;
  await loadOverview();
}

function openProductModal(item: CompetitorItem) {
  selectedPlid.value = item.plid;
  detailModalOpen.value = true;
}

function closeProductModal() {
  detailModalOpen.value = false;
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && detailModalOpen.value) closeProductModal();
}

async function loadOverview() {
  loading.value = true;
  pageError.value = "";
  try {
    const [overview, healthItems] = await Promise.all([
      fetchCompetitors(appliedStartDate.value, appliedEndDate.value),
      fetchCompetitorLinkHealth(),
    ]);
    competitors.value = overview.items;
    competitorDateRange.value = overview.date_range;
    if (!appliedStartDate.value && overview.date_range.selected_start) {
      appliedStartDate.value = overview.date_range.selected_start;
    }
    if (!appliedEndDate.value && overview.date_range.selected_end) {
      appliedEndDate.value = overview.date_range.selected_end;
    }
    if (!rangeStartDate.value) rangeStartDate.value = appliedStartDate.value;
    if (!rangeEndDate.value) rangeEndDate.value = appliedEndDate.value;
    linkHealth.value = healthItems;
    if (!competitors.value.some((item) => item.plid === selectedPlid.value)) {
      selectedPlid.value = competitors.value[0]?.plid ?? "";
    }
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : "读取竞品数据失败";
  } finally {
    loading.value = false;
  }
}

async function loadSharedBatchStatus() {
  try {
    sharedBatchStatus.value = await fetchCompetitorBatchStatus();
  } catch {
    // Keep the last shared progress during a short local-service interruption.
  }
}

function parseUrls(): {
  urls: string[];
  issue: LinkValidationIssue | null;
} {
  const unique = new Map<string, string>();
  const raw = rawUrls.value;
  const lines = raw.split(/\r\n|\n|\r/);
  let lineStart = 0;
  for (const [lineIndex, line] of lines.entries()) {
    const currentLineStart = lineStart;
    lineStart += line.length + lineBreakLength(raw, lineStart + line.length);
    const leadingWhitespace = line.match(/^\s*/)?.[0].length ?? 0;
    const url = line.trim();
    if (!url) continue;
    const validationMessage = validateCompetitorUrl(url);
    const match = url.match(/PLID(\d+)/i);
    if (validationMessage || !match) {
      return {
        urls: [...unique.values()],
        issue: {
          lineNumber: lineIndex + 1,
          start: currentLineStart + leadingWhitespace,
          end: currentLineStart + leadingWhitespace + url.length,
          url,
          message: validationMessage ?? "链接中未找到 Takealot PLID",
        },
      };
    }
    if (!unique.has(match[1])) unique.set(match[1], url);
  }
  return { urls: [...unique.values()], issue: null };
}

function validateCompetitorUrl(value: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "链接格式无效";
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    (hostname !== "takealot.com" && !hostname.endsWith(".takealot.com"))
  ) {
    return "不是 Takealot 商品链接";
  }
  if (!/PLID\d+/i.test(value)) return "链接中未找到 Takealot PLID";
  return null;
}

function lineBreakLength(value: string, position: number) {
  if (value.slice(position, position + 2) === "\r\n") return 2;
  return value[position] === "\n" || value[position] === "\r" ? 1 : 0;
}

function clearLinkValidation() {
  const hadValidationIssue = linkValidationIssue.value !== null;
  linkValidationIssue.value = null;
  linkErrorPulse.value = false;
  if (hadValidationIssue) {
    collectionErrors.value = collectionErrors.value.filter(
      (message) => !/^第 \d+ 行：/.test(message),
    );
  }
}

async function focusInvalidLink(issue: LinkValidationIssue) {
  linkValidationIssue.value = issue;
  linkErrorPulse.value = false;
  await nextTick();
  linkErrorPulse.value = true;

  const input = urlInput.value;
  if (!input) return;
  input.scrollIntoView({ behavior: "smooth", block: "center" });
  input.focus({ preventScroll: true });
  input.setSelectionRange(issue.start, issue.end);
  const lineHeight =
    Number.parseFloat(window.getComputedStyle(input).lineHeight) || 22;
  input.scrollTop = Math.max(
    0,
    (issue.lineNumber - 1) * lineHeight - input.clientHeight / 2,
  );
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function collectionId(prefix: "batch" | "request" | "client") {
  const randomId =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${randomId}`;
}

function restoreCollectionClientId(): string {
  try {
    const saved = sessionStorage.getItem(collectionClientKey);
    if (saved) return saved;
    const created = collectionId("client");
    sessionStorage.setItem(collectionClientKey, created);
    return created;
  } catch {
    return collectionId("client");
  }
}

function plidFromUrl(url: string) {
  return url.match(/PLID(\d+)/i)?.[1] ?? "";
}

function markAttempted(index: number) {
  if (!attemptedIndexes.value.includes(index)) {
    attemptedIndexes.value = [...attemptedIndexes.value, index].sort(
      (first, second) => first - second,
    );
  }
  completed.value = attemptedIndexes.value.length;
}

function markFailed(index: number, failed: boolean) {
  const indexes = new Set(failedIndexes.value);
  if (failed) indexes.add(index);
  else indexes.delete(index);
  failedIndexes.value = [...indexes].sort((first, second) => first - second);
}

function markTerminal(index: number, terminal: boolean) {
  const indexes = new Set(terminalIndexes.value);
  if (terminal) indexes.add(index);
  else indexes.delete(index);
  terminalIndexes.value = [...indexes].sort((first, second) => first - second);
}

function removeCollectionError(plid: string) {
  const prefix = `PLID${plid}：`;
  collectionErrors.value = collectionErrors.value.filter(
    (message) => !message.startsWith(prefix),
  );
}

function persistCollectionCheckpoint() {
  if (!batchUrls.value.length) return;
  const checkpoint: CollectionCheckpoint = {
    version: 5,
    rawUrls: rawUrls.value,
    batchUrls: batchUrls.value,
    attemptedIndexes: attemptedIndexes.value,
    failedIndexes: failedIndexes.value,
    terminalIndexes: terminalIndexes.value,
    results: collectionResults.value,
    errors: collectionErrors.value,
    stopReason: collectionStopReason.value,
    withStockProbe: withStockProbe.value,
    visibleBrowser: visibleBrowser.value,
    savedAt: new Date().toISOString(),
    batchId: batchId.value,
    running: collecting.value && !manualStopRequested.value,
    activeIndex: activeIndex.value,
    activeRequestId: activeRequestId.value,
    autoResumeAt:
      autoResumeAt.value === null
        ? null
        : new Date(autoResumeAt.value).toISOString(),
  };
  try {
    localStorage.setItem(collectionCheckpointKey, JSON.stringify(checkpoint));
  } catch {
    // Keep the live in-memory checkpoint when browser storage is unavailable.
  }
}

function restoreCollectionCheckpoint() {
  let checkpoint: CollectionCheckpoint;
  try {
    const raw = localStorage.getItem(collectionCheckpointKey);
    if (!raw) return;
    checkpoint = JSON.parse(raw) as CollectionCheckpoint;
  } catch {
    try {
      localStorage.removeItem(collectionCheckpointKey);
    } catch {
      // Ignore unavailable browser storage and continue with a fresh batch.
    }
    return;
  }
  if (
    ![1, 2, 3, 4, 5].includes(checkpoint.version)
    || !Array.isArray(checkpoint.batchUrls)
    || !Array.isArray(checkpoint.attemptedIndexes)
    || !Array.isArray(checkpoint.failedIndexes)
    || !Array.isArray(checkpoint.results)
    || !Array.isArray(checkpoint.errors)
  ) {
    try {
      localStorage.removeItem(collectionCheckpointKey);
    } catch {
      // Ignore unavailable browser storage and continue with a fresh batch.
    }
    return;
  }
  rawUrls.value = checkpoint.rawUrls;
  batchUrls.value = checkpoint.batchUrls;
  attemptedIndexes.value = checkpoint.attemptedIndexes;
  failedIndexes.value = checkpoint.failedIndexes;
  terminalIndexes.value = Array.isArray(checkpoint.terminalIndexes)
    ? checkpoint.terminalIndexes
    : [];
  collectionResults.value = checkpoint.results;
  collectionErrors.value = checkpoint.errors;
  collectionStopReason.value =
    checkpoint.stopReason
      === "连续 2 次无法连接 Takealot，已暂停剩余链接。请恢复梯子或代理后点击“继续失败/未完成”。"
      ? "上次批次被旧版页面判定为连接失败；该误判已修复，请点击“继续失败/未完成”重新检查剩余链接。"
      : checkpoint.stopReason;
  withStockProbe.value = checkpoint.withStockProbe;
  visibleBrowser.value = checkpoint.visibleBrowser;
  total.value = batchUrls.value.length;
  completed.value = attemptedIndexes.value.length;
  batchId.value = checkpoint.batchId || collectionId("batch");
  activeIndex.value =
    typeof checkpoint.activeIndex === "number" ? checkpoint.activeIndex : null;
  activeRequestId.value =
    typeof checkpoint.activeRequestId === "string"
      ? checkpoint.activeRequestId
      : null;
  const restoredAutoResumeAt =
    typeof checkpoint.autoResumeAt === "string"
      ? Date.parse(checkpoint.autoResumeAt)
      : Number.NaN;
  autoResumeAt.value = Number.isFinite(restoredAutoResumeAt)
    ? restoredAutoResumeAt
    : null;
  if (
    autoResumeAt.value === null
    && isAutomaticResumeReason(collectionStopReason.value)
    && resumeQueue.value.length > 0
  ) {
    autoResumeAt.value = Date.now() + automaticResumeDelayMs;
  }
  restoredRunWasActive.value =
    checkpoint.version >= 4
    && checkpoint.running === true
    && !checkpoint.stopReason
    && resumeQueue.value.length > 0;
  if (checkpoint.version < 5 && autoResumeAt.value !== null) {
    persistCollectionCheckpoint();
  }
}

async function startCollection() {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  if (sharedBatchStatus.value.active && !collecting.value) {
    if (sharedBatchMatchesCheckpoint.value && pendingResumeCount.value) {
      collectionStopReason.value =
        "检测到这是本页面刷新前启动的同一批次，正在自动接回断点……";
      await resumeCollection("auto_resume");
      return;
    }
    showCollectionNotice(activeBatchBlockedMessage());
    return;
  }
  try {
    clearLinkValidation();
    const { urls, issue } = parseUrls();
    if (issue) {
      collectionErrors.value = [
        `第 ${issue.lineNumber} 行：${issue.message}：${issue.url}`,
      ];
      await focusInvalidLink(issue);
      return;
    }
    if (!urls.length) throw new Error("请至少填写一个 Takealot 竞品链接");
    collectionResults.value = [];
    collectionErrors.value = [];
    collectionStopReason.value = "";
    autoResumeAt.value = null;
    completed.value = 0;
    batchUrls.value = urls;
    attemptedIndexes.value = [];
    failedIndexes.value = [];
    terminalIndexes.value = [];
    batchId.value = collectionId("batch");
    activeIndex.value = null;
    activeRequestId.value = null;
    restoredRunWasActive.value = false;
    manualStopRequested.value = false;
    total.value = urls.length;
    persistCollectionCheckpoint();
    await runCollection(
      urls.map((url, index) => ({ index, url })),
      "start",
    );
  } catch (error) {
    collectionErrors.value = [
      error instanceof Error ? error.message : "无法开始采集",
    ];
  }
}

async function resumeCollection(
  mode: "resume" | "auto_resume" | "scheduled_resume" = "resume",
) {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  if (collecting.value || !pendingResumeCount.value) return;
  if (
    sharedBatchStatus.value.active
    && !collecting.value
    && !sharedBatchMatchesCheckpoint.value
  ) {
    showCollectionNotice(activeBatchBlockedMessage());
    return;
  }
  clearLinkValidation();
  const { urls, issue } = parseUrls();
  if (issue) {
    collectionErrors.value = [
      `第 ${issue.lineNumber} 行：${issue.message}：${issue.url}`,
    ];
    await focusInvalidLink(issue);
    return;
  }
  const originalPlids = batchUrls.value.map(plidFromUrl);
  const currentPlids = urls.map(plidFromUrl);
  if (
    originalPlids.length !== currentPlids.length
    || originalPlids.some((plid, index) => plid !== currentPlids[index])
  ) {
    collectionStopReason.value =
      "链接列表或顺序已经变化，请点击“开始采集”建立一个新批次。";
    return;
  }
  batchUrls.value = urls;
  if (!batchId.value) batchId.value = collectionId("batch");
  collectionStopReason.value = "";
  manualStopRequested.value = false;
  await runCollection(resumeQueue.value, mode);
}

async function resumeInterruptedCollection() {
  if (
    !props.canOperate
    || collecting.value
    || !restoredRunWasActive.value
    || !pendingResumeCount.value
  ) {
    return;
  }
  restoredRunWasActive.value = false;
  collectionStopReason.value =
    "检测到页面刷新或会话中断，正在自动从断点恢复采集……";
  await delay(1_200);
  await resumeCollection("auto_resume");
}

function activeBatchBlockedMessage() {
  const status = sharedBatchStatus.value;
  const current = status.current_plid
    ? `，当前第 ${(status.current_index ?? 0) + 1} 条 PLID${status.current_plid}`
    : "，正在准备下一条商品";
  return `${sharedBatchOwner.value} 的竞品批次正在运行：已检查 ${status.completed}/${status.total}，待续爬 ${status.pending}${current}。请等待当前批次结束，或由发起人点击“停止采集”后再开始。`;
}

function showCollectionNotice(message: string) {
  collectionStopReason.value = message;
  collectionNoticeVersion.value += 1;
}

function isAutomaticResumeReason(reason: string) {
  return (
    reason.includes("连续 2 次发生真实连接失败")
    || reason.includes("网络或 Takealot 临时服务异常")
  );
}

function scheduleAutomaticResume(reason: string) {
  autoResumeAt.value = Date.now() + automaticResumeDelayMs;
  showCollectionNotice(
    `${reason} 系统将在10分钟后自动继续；如果仍然失败，会再间隔10分钟重试。`,
  );
  persistCollectionCheckpoint();
}

function clearAutomaticResumeSchedule() {
  autoResumeAt.value = null;
}

async function maybeAutoResumeScheduledCollection() {
  if (
    autoResumeAttempting.value
    || collecting.value
    || autoResumeAt.value === null
    || Date.now() < autoResumeAt.value
  ) {
    return;
  }
  if (!pendingResumeCount.value) {
    clearAutomaticResumeSchedule();
    persistCollectionCheckpoint();
    return;
  }
  if (!props.canOperate) {
    clearAutomaticResumeSchedule();
    persistCollectionCheckpoint();
    return;
  }
  if (anotherBatchIsActive.value) {
    scheduleAutomaticResume(
      `${sharedBatchOwner.value} 的另一批竞品采集正在占用服务，本轮自动续爬暂缓。`,
    );
    return;
  }

  autoResumeAttempting.value = true;
  clearAutomaticResumeSchedule();
  collectionStopReason.value = "网络恢复重试时间已到，正在自动继续失败和未完成链接……";
  persistCollectionCheckpoint();
  try {
    await resumeCollection("scheduled_resume");
  } finally {
    autoResumeAttempting.value = false;
  }
}

async function recordBatchEvent(
  event:
    | "start"
    | "resume"
    | "auto_resume"
    | "progress"
    | "heartbeat"
    | "paused"
    | "manual_stop"
    | "completed",
  reason = "",
  required = false,
) {
  if (!batchId.value) return;
  try {
    sharedBatchStatus.value = await logCompetitorBatchEvent({
      batchId: batchId.value,
      clientId: collectionClientId,
      event,
      completed: completed.value,
      total: total.value,
      pending: pendingResumeCount.value,
      succeeded: collectionResults.value.length,
      failed: failedIndexes.value.length,
      terminal: terminalIndexes.value.length,
      reason,
    });
  } catch (error) {
    if (required) throw error;
    // Collection must continue even when diagnostic logging is unavailable.
  }
}

async function runCollection(
  queue: CollectionQueueItem[],
  mode: CollectionRunMode,
) {
  const interruptedIndex = mode === "auto_resume" ? activeIndex.value : null;
  const interruptedRequestId =
    mode === "auto_resume" ? activeRequestId.value : null;
  collecting.value = true;
  manualStopRequested.value = false;
  total.value = batchUrls.value.length;
  const controller = new AbortController();
  abortController.value = controller;
  let consecutiveConnectionFailures = 0;
  persistCollectionCheckpoint();
  try {
    await recordBatchEvent(
      mode === "scheduled_resume" ? "resume" : mode,
      "",
      true,
    );
    clearAutomaticResumeSchedule();
    persistCollectionCheckpoint();
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "暂时无法取得竞品采集权";
    if (mode === "scheduled_resume") {
      scheduleAutomaticResume(`${message}，本轮自动续爬未能启动。`);
    } else {
      collectionStopReason.value = message;
    }
    collecting.value = false;
    abortController.value = null;
    persistCollectionCheckpoint();
    await loadSharedBatchStatus();
    return;
  }
  try {
    for (const { index, url } of queue) {
      if (controller.signal.aborted) break;
      const plid = plidFromUrl(url) || "未知商品";
      removeCollectionError(plid);
      const requestId =
        index === interruptedIndex && interruptedRequestId
          ? interruptedRequestId
          : collectionId("request");
      activeIndex.value = index;
      activeRequestId.value = requestId;
      activeStartedAt.value = Date.now();
      collectionClock.value = Date.now();
      persistCollectionCheckpoint();
      let settled = false;
      try {
        const result = await collectCompetitor(
          url,
          withStockProbe.value,
          visibleBrowser.value,
          controller.signal,
          {
            batchId: batchId.value,
            clientId: collectionClientId,
            requestId,
            itemIndex: index,
            totalItems: total.value,
          },
        );
        collectionResults.value = [
          ...collectionResults.value.filter((item) => item.plid !== result.plid),
          result,
        ];
        markFailed(index, false);
        markTerminal(index, false);
        consecutiveConnectionFailures = 0;
        settled = true;
      } catch (error) {
        if (controller.signal.aborted) break;
        const message = error instanceof Error ? error.message : "采集失败";
        collectionErrors.value.push(`PLID${plid}：${message}`);
        const confirmedInvalid =
          error instanceof ApiRequestError && error.status === 410;
        markFailed(index, !confirmedInvalid);
        markTerminal(index, confirmedInvalid);
        const isConnectionFailure =
          error instanceof ApiRequestError
          && (error.status === 0 || error.status >= 500);
        consecutiveConnectionFailures = isConnectionFailure
          ? consecutiveConnectionFailures + 1
          : 0;
        if (consecutiveConnectionFailures >= 2) {
          scheduleAutomaticResume(
            "连续 2 次发生真实连接失败或 Takealot 临时服务错误，已暂停剩余链接。",
          );
        }
        settled = true;
      } finally {
        if (settled) markAttempted(index);
        activeIndex.value = null;
        activeRequestId.value = null;
        activeStartedAt.value = null;
        persistCollectionCheckpoint();
      }
      await recordBatchEvent("progress");
      if (collectionStopReason.value) break;
      if (index !== queue[queue.length - 1]?.index) await delay(5_000);
    }
    await loadOverview();
  } finally {
    if (!manualStopRequested.value && collectionStopReason.value) {
      await recordBatchEvent("paused", collectionStopReason.value);
    } else if (!manualStopRequested.value && !controller.signal.aborted) {
      clearAutomaticResumeSchedule();
      await recordBatchEvent(
        "completed",
        pendingResumeCount.value
          ? `本轮结束，仍有 ${pendingResumeCount.value} 个失败或未完成链接`
          : "本批全部链接已检查",
      );
    }
    collecting.value = false;
    abortController.value = null;
    persistCollectionCheckpoint();
  }
}

function stopCollection() {
  manualStopRequested.value = true;
  clearAutomaticResumeSchedule();
  collectionStopReason.value =
    "已手动暂停；可以点击“继续失败/未完成”从断点恢复。";
  persistCollectionCheckpoint();
  void recordBatchEvent("manual_stop", collectionStopReason.value);
  abortController.value?.abort();
}

function formatCurrency(value: number | null) {
  return value === null
    ? "—"
    : new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value);
}

function reviewTone(stars: number) {
  if (stars >= 4) return "positive";
  if (stars === 3) return "neutral";
  return "negative";
}

function linkHealthLabel(status: CompetitorLinkHealthItem["status"]) {
  return status === "confirmed_invalid" ? "确认失效" : "疑似失效";
}
</script>

<template>
  <div class="competitor-module">
    <header class="hero">
      <div>
        <p class="eyebrow">TAKEALOT MARKET INTELLIGENCE</p>
        <h1>竞品雷达</h1>
        <p class="hero-copy">
          把库存、评论与销量信号放在同一条时间线上，让运营先看到变化，再决定动作。
        </p>
      </div>
      <div class="status-chip">
        <span class="status-dot"></span>
        本机数据 · MySQL
      </div>
    </header>

    <section class="collector panel">
      <div class="section-heading">
        <div>
          <p class="section-kicker">建立与刷新观察样本</p>
          <h2>批量采集竞品</h2>
        </div>
        <p class="section-note">每行一个链接，重复 PLID 会自动去重</p>
      </div>
      <div
        v-if="sharedBatchStatus.active"
        class="shared-collection-status"
        role="status"
        aria-live="polite"
      >
        <div>
          <strong>全员同步采集中 · {{ sharedBatchOwner }}</strong>
          <span>
            已检查 {{ sharedBatchStatus.completed }}/{{ sharedBatchStatus.total }}
            · 成功 {{ sharedBatchStatus.succeeded }}
            · 失败 {{ sharedBatchStatus.failed }}
            · 待续爬 {{ sharedBatchStatus.pending }}
          </span>
        </div>
        <span v-if="sharedBatchStatus.current_plid" class="shared-current-plid">
          当前第 {{ (sharedBatchStatus.current_index ?? 0) + 1 }} 条 ·
          PLID{{ sharedBatchStatus.current_plid }}
        </span>
        <span v-else>正在准备下一条商品</span>
      </div>
      <textarea
        ref="urlInput"
        v-model="rawUrls"
        aria-label="竞品链接"
        :aria-describedby="linkValidationIssue ? 'link-validation-error' : undefined"
        :aria-invalid="Boolean(linkValidationIssue)"
        :class="{
          'link-input-error': linkValidationIssue,
          'link-input-error-pulse': linkErrorPulse,
        }"
        :disabled="collecting || anotherBatchIsActive || !props.canOperate"
        spellcheck="false"
        @input="clearLinkValidation"
      ></textarea>
      <div
        v-if="linkValidationIssue"
        id="link-validation-error"
        class="link-diagnostic"
        role="alert"
      >
        <span class="link-diagnostic-location">
          第 {{ linkValidationIssue.lineNumber }} 行
        </span>
        <span class="link-diagnostic-marker" aria-hidden="true">×</span>
        <span class="link-diagnostic-content">
          <strong>{{ linkValidationIssue.message }}</strong>
          <code>{{ linkValidationIssue.url }}</code>
        </span>
      </div>
      <div class="collector-actions">
        <label class="switch-row">
          <input
            v-model="withStockProbe"
            type="checkbox"
            :disabled="collecting || anotherBatchIsActive || !props.canOperate"
          />
          <span class="switch"></span>
          <span>
            <strong>匿名购物车库存探测</strong>
            <small>逐个测试所有变体的当前卖家与 SKU，不进入结算</small>
          </span>
        </label>
        <label class="switch-row compact">
          <input
            v-model="visibleBrowser"
            type="checkbox"
            :disabled="
              collecting
              || anotherBatchIsActive
              || !withStockProbe
              || !props.canOperate
            "
          />
          <span class="switch"></span>
          <span><strong>显示检测浏览器</strong></span>
        </label>
        <button
          class="primary-button"
          @click="startCollection"
          v-if="!collecting"
        >
          开始采集
        </button>
        <button
          v-if="!collecting && pendingResumeCount"
          class="primary-button resume-button"
          @click="resumeCollection()"
        >
          继续失败/未完成（{{ pendingResumeCount }}）
        </button>
        <button
          class="primary-button stop-button"
          @click="stopCollection"
          v-if="collecting"
        >
          停止采集
        </button>
      </div>
      <div
        v-if="collectionStopReason"
        :key="collectionNoticeVersion"
        class="collection-action-alert"
        role="alert"
        aria-live="assertive"
      >
        <span class="collection-action-alert-icon" aria-hidden="true">!</span>
        <span>
          <strong>
            {{ sharedBatchStatus.active && !collecting
              ? sharedBatchMatchesCheckpoint
                ? "正在恢复刷新前的采集任务"
                : "当前已有竞品采集正在运行"
              : autoResumeAt
                ? "网络异常，已安排自动续爬"
              : "本次采集已暂停" }}
          </strong>
          <small>{{ collectionStopReason }}</small>
          <small v-if="autoResumeAt" class="collection-auto-resume-countdown">
            距离下次自动尝试：{{ autoResumeCountdown }}
          </small>
        </span>
      </div>
      <div v-if="collecting || completed" class="progress-track" aria-live="polite">
        <span :style="{ width: `${progress}%` }"></span>
      </div>
      <div
        v-if="collecting"
        class="collection-active-status"
        role="status"
        aria-live="polite"
      >
        <strong>{{ activeCollectionStatus }}</strong>
        <span>{{ activeCollectionHint }}</span>
      </div>
      <p v-if="collecting" class="method-note collection-persistence-note">
        采集正在后台继续；切换到其他页面后再返回，进度和结果仍会保留。
      </p>
      <div
        v-if="collectionResults.length || collectionErrors.length || batchUrls.length"
        class="result-strip"
      >
        <span v-if="collectionResults.length" class="result-good">
          成功 {{ collectionResults.length }} 个
        </span>
        <span v-if="collectionErrors.length" class="result-bad">
          失败 {{ collectionErrors.length }} 个
        </span>
        <span v-if="terminalIndexes.length" class="result-terminal">
          已确认失效 {{ terminalIndexes.length }} 个，本批后续自动跳过
        </span>
        <span v-if="batchUrls.length">
          本批已检查 {{ completed }}/{{ total }}，待续爬 {{ pendingResumeCount }} 个
        </span>
        <span v-for="notice in collectionNotices" :key="notice.plid">
          PLID{{ notice.plid }}：{{ notice.message }}
        </span>
        <span v-for="error in collectionErrors" :key="error">{{ error }}</span>
      </div>
    </section>

    <section class="metrics">
      <article>
        <span>已监控竞品</span>
        <strong>{{ competitors.length }}</strong>
        <small>当前活跃链接</small>
      </article>
      <article>
        <span>精确库存样本</span>
        <strong>{{ exactStockCount }}</strong>
        <small>当前最新快照</small>
      </article>
      <article>
        <span>平均评分</span>
        <strong>{{ averageRating }}</strong>
        <small>公开商品评分</small>
      </article>
      <article>
        <span>最近采集</span>
        <strong class="metric-date">{{ latestCollection }}</strong>
        <small>北京时间</small>
      </article>
    </section>

    <section v-if="linkHealth.length" class="panel link-health-panel">
      <div class="section-heading">
        <div>
          <p class="section-kicker">LINK REVIEW QUEUE</p>
          <h2>链接复核状态</h2>
        </div>
        <p class="section-note">
          疑似 {{ suspectedInvalidCount }} 个 · 确认失效 {{ confirmedInvalidCount }} 个
        </p>
      </div>
      <div class="table-wrap">
        <table class="link-health-table">
          <thead>
            <tr>
              <th>商品链接</th>
              <th>状态</th>
              <th>有效复核</th>
              <th>正常对照</th>
              <th>最近检查（北京时间）</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in linkHealth"
              :key="item.plid"
              v-memo="[item, failedCompetitorImages.has(item.图片 || '')]"
            >
              <td>
                <div class="competitor-product-cell">
                  <div class="competitor-product-image compact">
                    <img
                      v-if="canShowCompetitorImage(item.图片)"
                      :src="competitorImageUrl(item.图片)"
                      :alt="item.商品 ? `${item.商品} 商品图片` : `PLID${item.plid} 商品图片`"
                      width="192"
                      height="192"
                      loading="lazy"
                      decoding="async"
                      @error="markCompetitorImageFailed(item.图片)"
                    />
                    <span v-else>暂无图片</span>
                  </div>
                  <div>
                    <strong>{{ item.商品 || `PLID${item.plid}` }}</strong>
                    <a :href="item.url" target="_blank" rel="noreferrer">
                      PLID{{ item.plid }}
                    </a>
                  </div>
                </div>
              </td>
              <td>
                <span class="link-health-pill" :class="item.status">
                  {{ linkHealthLabel(item.status) }}
                </span>
              </td>
              <td>{{ item.confirmed_not_found_count }}/3</td>
              <td>
                {{
                  item.control_check_ok && item.control_plid
                    ? `PLID${item.control_plid}`
                    : "未取得有效对照"
                }}
              </td>
              <td>{{ formatChinaDateTime(item.last_checked_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="method-note">
        单次 404 不会删除链接。只有目标商品页为空、同一浏览器中的已知正常商品可打开，且至少间隔
        10 分钟累计 3 次，才会确认失效；确认失效只在当前断点续爬中自动跳过，重新点击“开始采集”仍可人工复核。
      </p>
    </section>

    <p v-if="pageError" class="error-banner">{{ pageError }}</p>
    <section class="panel overview">
      <div class="section-heading">
        <div>
          <p class="section-kicker">LATEST SNAPSHOT</p>
          <h2>竞品最新状态</h2>
        </div>
        <button class="quiet-button" @click="loadOverview">刷新页面数据</button>
      </div>
      <div v-if="loading" class="empty-state">正在读取本机数据……</div>
      <div v-else-if="!competitors.length" class="empty-state">
        <strong>还没有竞品快照</strong>
        <span>上方 4 个样本链接已经填好，点击“开始采集”即可建立第一条基线。</span>
      </div>
      <div v-else>
        <div class="competitor-list-filters" role="search" aria-label="筛选竞品最新状态">
          <label class="competitor-filter-field competitor-filter-search">
            <span>搜索商品</span>
            <input
              v-model="competitorQuery"
              type="search"
              placeholder="商品名称、PLID 或卖家"
            />
          </label>
          <label class="competitor-filter-field">
            <span>库存状态</span>
            <select v-model="competitorStockFilter">
              <option value="全部">全部库存</option>
              <option value="有货">有货</option>
              <option value="没货">没货</option>
              <option value="未探测">未探测</option>
            </select>
          </label>
          <label class="competitor-filter-field">
            <span>经营信号</span>
            <select v-model="competitorSignalFilter">
              <option value="全部">全部信号</option>
              <option v-for="signal in competitorSignalOptions" :key="signal" :value="signal">
                {{ signal }}
              </option>
            </select>
          </label>
          <label class="competitor-filter-field">
            <span>观察开始日期（北京时间）</span>
            <input
              v-model="rangeStartDate"
              type="date"
              :min="competitorDateRange.available_start || undefined"
              :max="rangeEndDate || competitorDateRange.available_end || undefined"
            />
          </label>
          <label class="competitor-filter-field">
            <span>观察结束日期（北京时间）</span>
            <input
              v-model="rangeEndDate"
              type="date"
              :min="rangeStartDate || competitorDateRange.available_start || undefined"
              :max="competitorDateRange.available_end || undefined"
            />
          </label>
          <div class="competitor-filter-summary">
            <span>
              {{ activeRangeLabel }} · 显示
              {{ filteredCompetitors.length }} / {{ competitors.length }} 个商品
            </span>
            <button type="button" class="quiet-button" @click="applyDateRange">
              按区间重算
            </button>
            <button
              v-if="competitorFiltersActive"
              type="button"
              class="quiet-button"
              @click="clearCompetitorFilters"
            >
              清除筛选
            </button>
          </div>
        </div>
        <p class="method-note">
          日期按北京时间自然日筛选。每个商品使用区间内最旧快照和最新快照重算库存净变化、
          新增评论与经营信号；只有首尾变体键、SKU、卖家集合一致且库存均为精确值时才比较库存。
        </p>
        <div v-if="!filteredCompetitors.length" class="empty-state competitor-filter-empty">
          <strong>没有符合条件的竞品</strong>
          <span>可以调整关键词、库存状态或经营信号。</span>
        </div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>竞品</th>
                <th>价格</th>
                <th>库存上限</th>
                <th>评论 / 评分</th>
                <th>观察期信号</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in filteredCompetitors"
                :key="item.plid"
                v-memo="[item, selectedPlid === item.plid, failedCompetitorImages.has(item.图片 || '')]"
                :class="{ selected: selectedPlid === item.plid }"
                tabindex="0"
                role="button"
                aria-haspopup="dialog"
                @click="openProductModal(item)"
                @keydown.enter="openProductModal(item)"
                @keydown.space.prevent="openProductModal(item)"
              >
                <td>
                  <div class="competitor-product-cell">
                    <div class="competitor-product-image">
                      <img
                        v-if="canShowCompetitorImage(item.图片)"
                        :src="competitorImageUrl(item.图片)"
                        :alt="`${item.商品} 商品图片`"
                        width="192"
                        height="192"
                        loading="lazy"
                        decoding="async"
                        @error="markCompetitorImageFailed(item.图片)"
                      />
                      <span v-else>暂无图片</span>
                    </div>
                    <div>
                      <strong>{{ item.商品 }}</strong>
                      <span>
                        PLID{{ item.plid }} · {{ item.当前卖家 || "未知卖家" }}
                      </span>
                    </div>
                  </div>
                </td>
                <td>{{ formatCurrency(item.价格) }}</td>
                <td>
                  <span
                    class="stock-pill"
                    :class="{
                      exact: item.库存精确,
                      unavailable: item.库存上限 === '没货',
                    }"
                  >
                    {{ item.库存上限 }}
                  </span>
                  <small v-if="item.库存参考过期 && item.上次成功库存">
                    本次未探测成功；上次成功 {{ item.上次成功库存 }}
                    · {{ formatChinaDateTime(item.上次成功库存时间) }}
                  </small>
                </td>
                <td>{{ item.评论数 }} 条 · {{ item.评分 ?? "—" }}</td>
                <td>
                  <span class="signal-label">{{ item.趋势判断 }}</span>
                  <small>{{ item.观察期销量信号 }}</small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <Teleport to="body">
      <div
        v-if="detailModalOpen && selected"
        class="competitor-modal-backdrop"
        @click.self="closeProductModal"
      >
        <section
          class="competitor-modal"
          role="dialog"
          aria-modal="true"
          :aria-label="`${selected.商品} 竞品详情`"
        >
          <header class="competitor-modal-header">
            <div class="competitor-modal-identity">
              <div class="competitor-product-image hero-image">
                <img
                  v-if="canShowCompetitorImage(selected.图片)"
                  :src="competitorImageUrl(selected.图片)"
                  :alt="`${selected.商品} 商品图片`"
                  width="192"
                  height="192"
                  decoding="async"
                  fetchpriority="high"
                  @error="markCompetitorImageFailed(selected.图片)"
                />
                <span v-else>暂无图片</span>
              </div>
              <div>
                <p class="section-kicker">COMPETITOR DETAIL</p>
                <h2>{{ selected.商品 }}</h2>
                <span>
                  PLID{{ selected.plid }} · {{ selected.当前卖家 || "未知卖家" }}
                </span>
              </div>
            </div>
            <button
              type="button"
              class="competitor-modal-close"
              aria-label="关闭商品详情"
              @click="closeProductModal"
            >
              ×
            </button>
          </header>

          <div v-if="detailLoading" class="empty-state slim">正在读取商品详情……</div>
          <p v-else-if="detailError" class="error-banner">{{ detailError }}</p>
          <template v-else>
            <div class="competitor-modal-metrics">
              <article>
                <small>当前价格</small>
                <strong>{{ formatCurrency(selected.价格) }}</strong>
              </article>
              <article>
                <small>平台仓库存</small>
                <strong>{{ selected.库存上限 }}</strong>
                <span v-if="selected.库存参考过期 && selected.上次成功库存">
                  本次未探测；上次成功 {{ selected.上次成功库存 }}
                  · {{ formatChinaDateTime(selected.上次成功库存时间) }}
                </span>
              </article>
              <article>
                <small>评论 / 评分</small>
                <strong>{{ selected.评论数 }} 条 · {{ selected.评分 ?? "—" }}</strong>
              </article>
              <article>
                <small>最近采集</small>
                <strong>{{ formatChinaDateTime(selected.采集时间) }}</strong>
              </article>
            </div>

            <div class="competitor-modal-content">
              <section class="detail-grid modal-detail-grid">
                <article class="panel decision-card">
                  <p class="section-kicker">OPERATING SIGNAL</p>
                  <h2>{{ selected.趋势判断 }}</h2>
                  <p>{{ selected.判断说明 }}</p>
                  <p class="method-note">
                    实际比较：
                    {{ formatChinaDateTime(selected.信号区间开始) }}
                    至 {{ formatChinaDateTime(selected.信号区间结束) }}
                    · {{ selected.区间快照数 ?? 0 }} 个快照
                  </p>
                  <div class="decision-stats">
                    <span>
                      <small>库存上限</small>
                      <strong>{{ selected.库存上限 }}</strong>
                      <em
                        v-if="selected.库存参考过期 && selected.上次成功库存"
                        class="stale-stock-note"
                      >
                        过期参考：{{ selected.上次成功库存 }}
                        · {{ formatChinaDateTime(selected.上次成功库存时间) }}
                      </em>
                    </span>
                    <span><small>累计评论</small><strong>{{ selected.评论数 }}</strong></span>
                    <span>
                      <small>区间库存净变化</small>
                      <strong>
                        {{
                          selected.库存可比 && selected.库存净变化 !== null
                            ? `${selected.库存净变化 > 0 ? "+" : ""}${selected.库存净变化}`
                            : "不可比"
                        }}
                      </strong>
                    </span>
                    <span>
                      <small>区间新增评论</small>
                      <strong>{{ selected.新增评论 ?? "—" }}</strong>
                    </span>
                    <span>
                      <small>观察期估算</small>
                      <strong>{{ selected.观察期销量信号 }}</strong>
                    </span>
                  </div>
                  <a :href="selected.链接" target="_blank" rel="noreferrer">
                    打开 Takealot 商品页
                  </a>
                </article>

                <article class="panel review-balance">
                  <p class="section-kicker">REVIEW BALANCE</p>
                  <h2>评论结构</h2>
                  <div class="balance-row positive">
                    <span>好评 4–5 星</span><strong>{{ selected.好评 }}</strong>
                    <i
                      :style="{
                        width: `${(selected.好评 / Math.max(1, selected.评论数)) * 100}%`,
                      }"
                    ></i>
                  </div>
                  <div class="balance-row neutral">
                    <span>中评 3 星</span><strong>{{ selected.中评 }}</strong>
                    <i
                      :style="{
                        width: `${(selected.中评 / Math.max(1, selected.评论数)) * 100}%`,
                      }"
                    ></i>
                  </div>
                  <div class="balance-row negative">
                    <span>差评 1–2 星</span><strong>{{ selected.差评 }}</strong>
                    <i
                      :style="{
                        width: `${(selected.差评 / Math.max(1, selected.评论数)) * 100}%`,
                      }"
                    ></i>
                  </div>
                </article>
              </section>

              <section class="panel variant-panel">
                <div class="section-heading">
                  <div>
                    <p class="section-kicker">VARIANT INVENTORY</p>
                    <h2>各变体库存</h2>
                  </div>
                  <span>{{ detail.variants.length }} 个变体 · 评论共用商品数据</span>
                </div>
                <div v-if="!detail.variants.length" class="empty-state slim">
                  这条历史快照尚无变体明细，重新采集后会逐个显示。
                </div>
                <div v-else class="table-wrap">
                  <table class="variant-table">
                    <thead>
                      <tr>
                        <th>变体</th>
                        <th>平台 SKU</th>
                        <th>卖家</th>
                        <th>价格</th>
                        <th>平台仓库存</th>
                        <th>说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="variant in detail.variants" :key="variant.变体键">
                        <td>
                          <div class="competitor-product-cell compact-row">
                            <div class="competitor-product-image compact">
                              <img
                                v-if="canShowCompetitorImage(variant.图片)"
                                :src="competitorImageUrl(variant.图片)"
                                :alt="`${variant.变体} 商品图片`"
                                width="192"
                                height="192"
                                loading="lazy"
                                decoding="async"
                                @error="markCompetitorImageFailed(variant.图片)"
                              />
                              <span v-else>暂无图片</span>
                            </div>
                            <a :href="variant.链接" target="_blank" rel="noreferrer">
                              {{ variant.变体 }}
                            </a>
                          </div>
                        </td>
                        <td>{{ variant.SKU || "—" }}</td>
                        <td>{{ variant.卖家 || "未知卖家" }}</td>
                        <td>{{ formatCurrency(variant.价格) }}</td>
                        <td>
                          <span
                            class="stock-pill"
                            :class="{
                              exact: variant.库存精确,
                              unavailable: variant.库存 === '没货',
                            }"
                          >
                            {{ variant.库存 }}
                          </span>
                        </td>
                        <td>
                          <small>{{ variant.库存说明 || "—" }}</small>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p class="method-note">
                  库存按变体分别探测；供应商调货、长时效到货和当前不可购买的变体按平台仓没货处理。
                  评论按 PLID 商品维度共用，不因变体重复采集或重复计数。
                </p>
              </section>

              <section class="panel history-panel">
                <div class="section-heading">
                  <div>
                    <p class="section-kicker">OBSERVATION HISTORY</p>
                    <h2>区间原始快照</h2>
                  </div>
                  <span>{{ detail.history.length }} 个时间点</span>
                </div>
                <div v-if="detail.history.length < 2" class="empty-state slim">
                  本区间不足两个快照，只能建立基线，不能计算首尾变化。
                </div>
                <div v-else class="timeline">
                  <article
                    v-for="item in detail.history"
                    :key="item.采集时间"
                    v-memo="[item, failedCompetitorImages.has(item.图片 || '')]"
                  >
                    <div class="timeline-product-head">
                      <div class="competitor-product-image compact">
                        <img
                          v-if="canShowCompetitorImage(item.图片)"
                          :src="competitorImageUrl(item.图片)"
                          :alt="`${item.商品} 商品图片`"
                          width="192"
                          height="192"
                          loading="lazy"
                          decoding="async"
                          @error="markCompetitorImageFailed(item.图片)"
                        />
                        <span v-else>暂无图片</span>
                      </div>
                      <div>
                        <time>{{ formatChinaDateTime(item.采集时间) }}</time>
                        <strong>原始观测值</strong>
                      </div>
                    </div>
                    <span>库存 {{ item.库存上限 }} · 评论 {{ item.评论数 }}</span>
                    <small>价格 {{ formatCurrency(item.价格) }} · 不在单个快照上重复判定区间信号</small>
                  </article>
                </div>
              </section>

              <section class="panel reviews-panel">
                <div class="section-heading">
                  <div>
                    <p class="section-kicker">VOICE OF CUSTOMER</p>
                    <h2>公开评论</h2>
                  </div>
                  <span class="review-result-count">
                    显示 {{ filteredReviews.length }} / {{ detail.reviews.length }} 条
                  </span>
                </div>
                <div class="review-filter-bar">
                  <div class="filter-tabs">
                    <button
                      v-for="filter in ['全部', '好评', '中评', '差评'] as const"
                      :key="filter"
                      :class="{ active: reviewFilter === filter }"
                      @click="reviewFilter = filter"
                    >
                      {{ filter }}
                    </button>
                  </div>
                  <div class="review-controls">
                    <label>
                      <span>开始日期</span>
                      <input
                        v-model="reviewStartDate"
                        type="date"
                        :min="reviewMinDate || undefined"
                        :max="reviewEndDate || reviewMaxDate || undefined"
                      />
                    </label>
                    <label>
                      <span>结束日期</span>
                      <input
                        v-model="reviewEndDate"
                        type="date"
                        :min="reviewStartDate || reviewMinDate || undefined"
                        :max="reviewMaxDate || undefined"
                      />
                    </label>
                    <label>
                      <span>展示排序</span>
                      <select v-model="reviewSort">
                        <option value="date_desc">最新评论优先</option>
                        <option value="date_asc">最早评论优先</option>
                        <option value="rating_desc">评分从高到低</option>
                        <option value="rating_asc">评分从低到高</option>
                      </select>
                    </label>
                    <button
                      v-if="reviewStartDate || reviewEndDate"
                      class="clear-review-dates"
                      @click="clearReviewDates"
                    >
                      清除时间
                    </button>
                  </div>
                </div>
                <div v-if="!filteredReviews.length" class="empty-state slim">
                  暂无对应评论。
                </div>
                <div v-else class="review-list">
                  <article
                    v-for="(review, reviewIndex) in filteredReviews"
                    :key="`${review.评论日期}-${review.标题}-${review.评论人}-${reviewIndex}`"
                  >
                    <span class="review-score" :class="reviewTone(review.星级)">
                      {{ review.星级 }} 星
                    </span>
                    <div>
                      <strong>{{ review.标题 || "未填写标题" }}</strong>
                      <p>{{ review.评论内容 || "未填写评论内容" }}</p>
                      <small>
                        {{ review.评论人 || "匿名用户" }}
                        · {{ review.评论日期 || "日期未知" }}
                      </small>
                    </div>
                  </article>
                </div>
              </section>
            </div>

            <div class="competitor-modal-actions">
              <a :href="selected.链接" target="_blank" rel="noreferrer">
                打开 Takealot 商品页
              </a>
              <button type="button" @click="closeProductModal">关闭</button>
            </div>
          </template>
        </section>
      </div>
    </Teleport>

    <footer class="module-footer">
      库存是各变体在隔离匿名会话中的购物车可售上限；评论按商品共用。所有估算均需结合连续快照判断。
    </footer>
  </div>
</template>
