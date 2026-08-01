<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  ApiRequestError,
  collectCompetitor,
  createCompetitorTarget,
  deleteCompetitorTarget,
  fetchCompetitorBatchStatus,
  fetchCompetitorDetail,
  fetchCompetitorLinkHealth,
  fetchCompetitorTargetAudits,
  fetchCompetitorTargets,
  fetchCompetitors,
  logCompetitorBatchEvent,
  prioritizeCompetitorTarget,
  updateCompetitorTarget,
  type CompetitorBatchStatus,
} from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import {
  MAX_AUTOMATIC_RETRY_ATTEMPTS,
  scheduleRetryAfterGap,
} from "../retryQueue";
import type {
  CollectResult,
  CompetitorDateRange,
  CompetitorDetail,
  CompetitorItem,
  CompetitorLinkHealthItem,
  CompetitorOfferItem,
  CompetitorTargetAuditItem,
  CompetitorTargetItem,
  CompetitorVariantItem,
} from "../types";
import { formatChinaDateTime } from "../time";

defineOptions({ name: "CompetitorsPage" });
const props = defineProps<{
  canOperate?: boolean;
  canControlCollection?: boolean;
  onPermissionDenied?: () => void;
}>();

interface CollectionQueueItem {
  index: number;
  url: string;
  priority?: boolean;
}

interface CollectionErrorItem {
  plid: string;
  url: string;
  message: string;
}

interface CompetitorTargetGroup {
  groupPlid: string;
  members: CompetitorTargetItem[];
}

interface CollectionCheckpoint {
  version: 1 | 2 | 3 | 4 | 5 | 6 | 7;
  rawUrls: string;
  batchUrls: string[];
  attemptedIndexes: number[];
  failedIndexes: number[];
  terminalIndexes?: number[];
  results: CollectResult[];
  errors: Array<string | CollectionErrorItem>;
  stopReason: string;
  withStockProbe: boolean;
  visibleBrowser: boolean;
  savedAt: string;
  batchId?: string;
  running?: boolean;
  activeIndex?: number | null;
  activeRequestId?: string | null;
  autoResumeAt?: string | null;
  stockUnprobedIndexes?: number[];
}

type CollectionRunMode =
  | "start"
  | "resume"
  | "auto_resume"
  | "scheduled_resume";
type TargetActionSource = "default" | "manual_retry";

const collectionCheckpointKey = "takealot-competitor-collection-v1";
const collectionClientKey = "takealot-competitor-client-v1";
const automaticResumeDelayMs = 10 * 60 * 1_000;
const collectionClientId = restoreCollectionClientId();
const rawUrls = ref("");
const targets = ref<CompetitorTargetItem[]>([]);
const targetQuery = ref("");
const targetPage = ref(1);
const targetPageSize = ref(20);
const targetPageSizeOptions = [20, 50, 100] as const;
const targetListOpen = ref(false);
const expandedTargetGroupPlids = ref<Set<string>>(new Set());
const targetListTrigger = ref<HTMLButtonElement | null>(null);
const targetActionOpen = ref(false);
const targetActionPlid = ref("");
const targetActionFallbackUrl = ref("");
const targetActionSource = ref<TargetActionSource>("default");
const newTargetUrl = ref("");
const targetManagerBusy = ref("");
const targetManagerError = ref("");
const targetManagerNotice = ref("");
const duplicateTarget = ref<{
  plid: string;
  hasHistory: boolean;
} | null>(null);
const editingTargetPlid = ref("");
const editingTargetUrl = ref("");
const targetAuditItems = ref<CompetitorTargetAuditItem[]>([]);
const targetAuditLoading = ref(false);
const targetAuditError = ref("");
const targetAuditOpen = ref(false);
const targetAuditTrigger = ref<HTMLButtonElement | null>(null);
const targetAuditLoaded = ref(false);
const targetAuditTotal = ref(0);
const targetAuditPage = ref(1);
const targetAuditPageSize = 20;
const targetAuditStartDate = ref(localDateInput(30));
const targetAuditEndDate = ref(localDateInput(0));
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
const collectionErrors = ref<CollectionErrorItem[]>([]);
const collectionStopReason = ref("");
const collectionNoticeVersion = ref(0);
const collectionActivityNotice = ref("");
const batchUrls = ref<string[]>([]);
const attemptedIndexes = ref<number[]>([]);
const failedIndexes = ref<number[]>([]);
const terminalIndexes = ref<number[]>([]);
const stockUnprobedIndexes = ref<number[]>([]);
const pendingPriorityTargets = ref<CompetitorBatchStatus["priority_targets"]>([]);
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
  current_stage: null,
  reason: "",
  started_at: null,
  updated_at: null,
  queued_targets: [],
  priority_targets: [],
  prioritized_targets: [],
});
const linkHealth = ref<CompetitorLinkHealthItem[]>([]);
const linkHealthOpen = ref(false);
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
const competitorPage = ref(1);
const competitorPageSize = ref(20);
const competitorPageSizeOptions = [20, 50, 100] as const;
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
const competitorsByPlid = computed(
  () => new Map(competitors.value.map((item) => [item.plid, item])),
);
const selectedTarget = computed(
  () => targets.value.find((target) => target.plid === selectedPlid.value) ?? null,
);
const targetActionTarget = computed(
  () => targets.value.find((target) => target.plid === targetActionPlid.value) ?? null,
);
const targetsWithHistoryCount = computed(
  () => targets.value.filter((target) => target.has_history).length,
);
const targetsPendingFirstCaptureCount = computed(
  () => targets.value.length - targetsWithHistoryCount.value,
);
const targetGroups = computed<CompetitorTargetGroup[]>(() => {
  const groups = new Map<string, CompetitorTargetItem[]>();
  for (const target of targets.value) {
    const groupPlid = target.offer_group_plid || target.plid;
    const members = groups.get(groupPlid) ?? [];
    members.push(target);
    groups.set(groupPlid, members);
  }
  return [...groups.entries()].map(([groupPlid, members]) => ({
    groupPlid,
    members,
  }));
});
const filteredTargetGroups = computed(() => {
  const query = competitorSearchTerm(targetQuery.value);
  if (!query) return targetGroups.value;
  return targetGroups.value.filter((group) =>
    group.members.some((target) =>
      [
        target.plid,
        target.title ?? "",
        target.url,
        ...targetOffers(target).flatMap((offer) => [
          offer.offer_id ?? "",
          offer.卖家ID ?? "",
          offer.卖家,
          offer.SKU ?? "",
          offer.库存状态,
          offer.库存信号,
        ]),
      ].some((value) => value.toLocaleLowerCase().includes(query)),
    ),
  );
});
const targetPageCount = computed(() =>
  Math.max(1, Math.ceil(filteredTargetGroups.value.length / targetPageSize.value)),
);
const pagedTargetGroups = computed(() => {
  const start = (targetPage.value - 1) * targetPageSize.value;
  return filteredTargetGroups.value.slice(start, start + targetPageSize.value);
});
const targetAuditPageCount = computed(() =>
  Math.max(1, Math.ceil(targetAuditTotal.value / targetAuditPageSize)),
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
const prioritizedTargetStates = computed(
  () =>
    new Map(
      (sharedBatchStatus.value.prioritized_targets ?? []).map((item) => [
        item.plid,
        item,
      ]),
    ),
);
const pendingPriorityTargetPlids = computed(
  () =>
    new Set(
      (sharedBatchStatus.value.priority_targets ?? []).map((item) => item.plid),
    ),
);
const targetActionIsManualRetry = computed(
  () => targetActionSource.value === "manual_retry",
);
const competitorSignalOptions = computed(() =>
  [
    ...new Set(
      competitors.value
        .flatMap((item) => [
          item.趋势判断,
          item.价格信号,
          ...item.跟卖报价.flatMap((offer) => [offer.价格信号, offer.库存信号]),
        ])
        .filter(Boolean),
    ),
  ].sort(
    (first, second) => first.localeCompare(second, "zh-CN"),
  ),
);
const filteredCompetitors = computed(() => {
  const query = competitorSearchTerm(competitorQuery.value);
  return competitors.value.filter((item) => {
    if (
      query
      && ![
        item.商品,
        item.plid,
        item.当前卖家 ?? "",
        item.库存上限,
        item.趋势判断,
        item.价格信号,
        ...item.跟卖报价.flatMap((offer) => [
          offer.offer_id ?? "",
          offer.卖家ID ?? "",
          offer.卖家,
          offer.SKU ?? "",
          offer.变体,
          offer.库存状态,
          offer.价格信号,
          offer.库存信号,
        ]),
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
      || item.价格信号 === competitorSignalFilter.value
      || item.跟卖报价.some(
        (offer) =>
          offer.价格信号 === competitorSignalFilter.value
          || offer.库存信号 === competitorSignalFilter.value,
      )
    );
  });
});
const competitorPageCount = computed(() =>
  Math.max(1, Math.ceil(filteredCompetitors.value.length / competitorPageSize.value)),
);
const pagedCompetitors = computed(() => {
  const start = (competitorPage.value - 1) * competitorPageSize.value;
  return filteredCompetitors.value.slice(start, start + competitorPageSize.value);
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
const variantsBySnapshot = computed(() => {
  const grouped = new Map<number, CompetitorVariantItem[]>();
  for (const variant of detail.value.variants) {
    const variants = grouped.get(variant.快照ID) ?? [];
    variants.push(variant);
    grouped.set(variant.快照ID, variants);
  }
  return grouped;
});
const latestVariants = computed(() => {
  const snapshotId = selected.value?.快照ID;
  return snapshotId === undefined
    ? []
    : variantsBySnapshot.value.get(snapshotId) ?? [];
});
function snapshotVariants(snapshotId: number) {
  return variantsBySnapshot.value.get(snapshotId) ?? [];
}
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
const displayedBatchCompleted = computed(() =>
  sharedBatchStatus.value.active
    ? sharedBatchStatus.value.completed
    : completed.value,
);
const displayedBatchTotal = computed(() =>
  sharedBatchStatus.value.active ? sharedBatchStatus.value.total : total.value,
);
const displayedBatchSucceeded = computed(() =>
  sharedBatchStatus.value.active
    ? sharedBatchStatus.value.succeeded
    : collectionResults.value.length,
);
const displayedBatchFailed = computed(() =>
  sharedBatchStatus.value.active
    ? sharedBatchStatus.value.failed
    : failedIndexes.value.length,
);
const hasDisplayedBatchProgress = computed(
  () =>
    sharedBatchStatus.value.active
    || Boolean(
      collectionResults.value.length
      || collectionErrors.value.length
      || batchUrls.value.length,
    ),
);
const progress = computed(() =>
  displayedBatchTotal.value
    ? Math.round(
      (displayedBatchCompleted.value / displayedBatchTotal.value) * 100,
    )
    : 0,
);
const successfulPlids = computed(
  () => new Set(collectionResults.value.map((result) => result.plid)),
);
const confirmedInvalidCount = computed(
  () =>
    linkHealth.value.filter((item) => item.status === "confirmed_invalid").length,
);
const retainedConfirmedInvalidCount = computed(() => {
  const confirmedPlids = new Set(
    linkHealth.value
      .filter((item) => item.status === "confirmed_invalid")
      .map((item) => item.plid),
  );
  for (const index of terminalIndexes.value) {
    const plid = plidFromUrl(batchUrls.value[index] ?? "");
    if (plid) confirmedPlids.add(plid);
  }
  return Math.max(confirmedPlids.size, sharedBatchStatus.value.terminal);
});
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
  const failed = new Set(failedIndexes.value);
  const terminal = new Set(terminalIndexes.value);
  const firstUnattempted = batchUrls.value.findIndex(
    (_, index) => !attempted.has(index),
  );
  const start = Math.min(
    failedStart,
    firstUnattempted < 0 ? Number.POSITIVE_INFINITY : firstUnattempted,
  );
  if (!Number.isFinite(start)) return [];
  const stockUnprobed = new Set(stockUnprobedIndexes.value);
  return batchUrls.value
    .map((url, index) => ({ index, url }))
    .filter(
      ({ index, url }) =>
        index >= start
        && !terminal.has(index)
        && (
          failed.has(index)
          || !successfulPlids.value.has(plidFromUrl(url))
          || !attempted.has(index)
        ),
    )
    .sort((first, second) => {
      const deferredDifference =
        Number(stockUnprobed.has(first.index))
        - Number(stockUnprobed.has(second.index));
      return deferredDifference || first.index - second.index;
    });
});
const pendingResumeCount = computed(() => resumeQueue.value.length);
const displayedBatchPending = computed(() =>
  sharedBatchStatus.value.active
    ? sharedBatchStatus.value.pending
    : pendingResumeCount.value,
);
const showLocalCollectionDetails = computed(
  () => !sharedBatchStatus.value.active || sharedBatchMatchesCheckpoint.value,
);
const activeCollectionStatus = computed(() => {
  void collectionClock.value;
  const shared = sharedBatchStatus.value;
  const sharedStage = shared.current_stage?.trim() || "";
  if (!collecting.value && shared.active) {
    const current = shared.current_plid ? ` · PLID${shared.current_plid}` : "";
    const stage = sharedStage ? ` · ${sharedStage}` : "";
    const position =
      shared.current_index !== null && shared.total
        ? `第 ${shared.current_index + 1}/${shared.total} 条`
        : "正在准备下一条商品";
    return `${position}${current}${stage}`;
  }
  if (!collecting.value) return "";
  if (activeIndex.value === null) {
    return "正在登记采集任务或准备下一条商品，请稍候。";
  }
  const url = batchUrls.value[activeIndex.value] ?? "";
  const plid = plidFromUrl(url) || "未知";
  const elapsed = activeStartedAt.value === null
    ? 0
    : Math.max(0, Math.floor((Date.now() - activeStartedAt.value) / 1_000));
  const stage =
    shared.current_request_id === activeRequestId.value && sharedStage
      ? ` · ${sharedStage}`
      : "";
  return `正在检测第 ${activeIndex.value + 1}/${total.value} 条 · PLID${plid}${stage} · 已等待 ${elapsed} 秒`;
});
const activeCollectionHint = computed(() => {
  const stage = sharedBatchStatus.value.current_stage ?? "";
  if (
    stage.includes("后台数据")
    || stage.includes("商品与变体")
    || stage.includes("评论")
  ) {
    return "当前正在后台读取公开数据，这一阶段不会显示库存检测窗口；完成后才会打开可见浏览器。";
  }
  if (stage.includes("库存探测")) {
    return "正在进行购物车库存探测；商品有多个变体时会在同一个检测窗口内依次处理。";
  }
  if (stage.includes("保存")) {
    return "检测窗口已经关闭，正在写入商品、评论和各变体库存快照。";
  }
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
  await Promise.all([
    loadOverview(),
    loadTargets(),
    loadSharedBatchStatus(),
  ]);
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

watch([targetQuery, targetPageSize], () => {
  targetPage.value = 1;
});

watch(targetPageCount, (pageCount) => {
  if (targetPage.value > pageCount) targetPage.value = pageCount;
});

watch(
  [
    competitorQuery,
    competitorStockFilter,
    competitorSignalFilter,
    competitorPageSize,
  ],
  () => {
    competitorPage.value = 1;
  },
);

watch(competitorPageCount, (pageCount) => {
  if (competitorPage.value > pageCount) competitorPage.value = pageCount;
});

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

watch(
  [detailModalOpen, targetListOpen, targetAuditOpen, targetActionOpen],
  ([detailOpen, targetManagerOpen, targetAuditDialogOpen, targetActionDialogOpen]) => {
    document.body.style.overflow =
      detailOpen || targetManagerOpen || targetAuditDialogOpen || targetActionDialogOpen
        ? "hidden"
        : "";
  },
);

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
  if (editingTargetPlid.value && editingTargetPlid.value !== item.plid) {
    cancelEditTarget();
  }
  clearTargetManagerFeedback();
  detailModalOpen.value = true;
}

function closeProductModal() {
  detailModalOpen.value = false;
  if (editingTargetPlid.value === selectedPlid.value) cancelEditTarget();
  clearTargetManagerFeedback();
}

async function addSelectedTarget() {
  if (!selected.value) return;
  newTargetUrl.value = selected.value.链接;
  await addTarget();
}

function targetUrlForPlid(plid: string) {
  return (
    targets.value.find((target) => target.plid === plid)?.url
    || batchUrls.value.find((url) => plidFromUrl(url) === plid)
    || ""
  );
}

function openTargetActionForLink(
  plid: string,
  url: string,
  source: TargetActionSource = "default",
) {
  const resolvedUrl = url || targetUrlForPlid(plid);
  if (!plid || !resolvedUrl) {
    showCollectionNotice("这条任务没有可识别的 PLID 或链接，暂时无法打开队列操作页。");
    return;
  }
  if (editingTargetPlid.value && editingTargetPlid.value !== plid) {
    cancelEditTarget();
  }
  clearTargetManagerFeedback();
  targetActionPlid.value = plid;
  targetActionFallbackUrl.value = resolvedUrl;
  targetActionSource.value = source;
  targetActionOpen.value = true;
}

function openTargetAction(item: CompetitorLinkHealthItem) {
  openTargetActionForLink(item.plid, item.url);
}

function closeTargetAction() {
  targetActionOpen.value = false;
  if (editingTargetPlid.value === targetActionPlid.value) cancelEditTarget();
  clearTargetManagerFeedback();
  targetActionPlid.value = "";
  targetActionFallbackUrl.value = "";
  targetActionSource.value = "default";
}

async function addTargetActionTarget() {
  if (!targetActionFallbackUrl.value) return;
  const manualRetry = targetActionIsManualRetry.value;
  const plid = targetActionPlid.value;
  newTargetUrl.value = targetActionFallbackUrl.value;
  await addTarget();
  const addedTarget = targets.value.find((target) => target.plid === plid);
  if (manualRetry && addedTarget && sharedBatchStatus.value.active) {
    await prioritizeTarget(addedTarget, true);
  }
}

function openTargetList() {
  targetListOpen.value = true;
}

function closeTargetList() {
  targetListOpen.value = false;
  void nextTick(() => targetListTrigger.value?.focus());
}

function openTargetAudit() {
  targetAuditOpen.value = true;
  if (!targetAuditLoaded.value) void loadTargetAudits(1);
}

function closeTargetAudit() {
  targetAuditOpen.value = false;
  void nextTick(() => targetAuditTrigger.value?.focus());
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape") return;
  if (targetActionOpen.value) {
    closeTargetAction();
    return;
  }
  if (targetAuditOpen.value) {
    closeTargetAudit();
    return;
  }
  if (targetListOpen.value) {
    closeTargetList();
    return;
  }
  if (detailModalOpen.value) closeProductModal();
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
    const status = await fetchCompetitorBatchStatus();
    sharedBatchStatus.value = status;
    mergeQueuedTargetsIntoLocalBatch(status);
  } catch {
    // Keep the last shared progress during a short local-service interruption.
  }
}

async function loadTargets() {
  try {
    targets.value = await fetchCompetitorTargets();
    if (!batchUrls.value.length) {
      rawUrls.value = targets.value.map((target) => target.url).join("\n");
    }
  } catch (error) {
    targetManagerError.value =
      error instanceof Error ? error.message : "读取竞品链接清单失败";
  }
}

async function addTarget() {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  clearTargetManagerFeedback();
  duplicateTarget.value = null;
  const url = newTargetUrl.value.trim();
  const issue = validateCompetitorUrl(url);
  if (issue) {
    targetManagerError.value = issue;
    return;
  }
  const plid = plidFromUrl(url);
  const existingTarget = targets.value.find((target) => target.plid === plid);
  if (existingTarget) {
    showDuplicateTarget(existingTarget);
    return;
  }
  targetManagerBusy.value = "add";
  try {
    const result = await createCompetitorTarget(url);
    newTargetUrl.value = "";
    await Promise.all([loadTargets(), loadSharedBatchStatus()]);
    targetManagerNotice.value = result.queued_to_active_batch
      ? `PLID${result.item.plid} 已保存，并加入当前运行批次队头；当前商品结束后优先探测。`
      : `PLID${result.item.plid} 已保存，将进入下一次采集清单。`;
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 409 && plid) {
      await loadTargets();
      const duplicate = targets.value.find((target) => target.plid === plid);
      if (duplicate) {
        showDuplicateTarget(duplicate);
        return;
      }
    }
    targetManagerError.value =
      error instanceof Error ? error.message : "新增竞品链接失败";
  } finally {
    targetManagerBusy.value = "";
  }
}

function clearTargetManagerFeedback() {
  targetManagerError.value = "";
  targetManagerNotice.value = "";
  duplicateTarget.value = null;
}

function showDuplicateTarget(target: CompetitorTargetItem) {
  targetManagerError.value = "";
  targetManagerNotice.value = "";
  duplicateTarget.value = {
    plid: target.plid,
    hasHistory: target.has_history,
  };
}

async function jumpToDuplicateTarget() {
  const duplicate = duplicateTarget.value;
  if (!duplicate?.hasHistory) return;
  competitorQuery.value = "";
  competitorStockFilter.value = "全部";
  competitorSignalFilter.value = "全部";
  if (!competitors.value.some((item) => item.plid === duplicate.plid)) {
    const availableStart = competitorDateRange.value.available_start;
    const availableEnd = competitorDateRange.value.available_end;
    rangeStartDate.value = availableStart ?? "";
    rangeEndDate.value = availableEnd ?? "";
    appliedStartDate.value = rangeStartDate.value;
    appliedEndDate.value = rangeEndDate.value;
    await loadOverview();
  }
  await nextTick();
  selectedPlid.value = duplicate.plid;
  const duplicateIndex = filteredCompetitors.value.findIndex(
    (item) => item.plid === duplicate.plid,
  );
  if (duplicateIndex >= 0) {
    competitorPage.value =
      Math.floor(duplicateIndex / competitorPageSize.value) + 1;
  }
  await nextTick();
  const row = document.getElementById(`competitor-row-${duplicate.plid}`);
  if (!row) {
    targetManagerError.value = "已有历史记录，但当前观察区间没有可显示的商品卡片";
    return;
  }
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.focus({ preventScroll: true });
}

function beginEditTarget(target: CompetitorTargetItem) {
  editingTargetPlid.value = target.plid;
  editingTargetUrl.value = target.url;
  clearTargetManagerFeedback();
}

function cancelEditTarget() {
  editingTargetPlid.value = "";
  editingTargetUrl.value = "";
}

async function saveTargetEdit(plid: string) {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  clearTargetManagerFeedback();
  const url = editingTargetUrl.value.trim();
  const issue = validateCompetitorUrl(url);
  if (issue) {
    targetManagerError.value = issue;
    return;
  }
  targetManagerBusy.value = plid;
  try {
    await updateCompetitorTarget(plid, url);
    cancelEditTarget();
    await loadTargets();
    targetManagerNotice.value = `PLID${plid} 的链接已更新。`;
  } catch (error) {
    targetManagerError.value =
      error instanceof Error ? error.message : "修改竞品链接失败";
  } finally {
    targetManagerBusy.value = "";
  }
}

async function removeTarget(target: CompetitorTargetItem) {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  if (
    !window.confirm(
      `确定从后续监控清单移除 PLID${target.plid} 吗？历史快照与操作记录会保留。`,
    )
  ) {
    return;
  }
  clearTargetManagerFeedback();
  targetManagerBusy.value = target.plid;
  try {
    await deleteCompetitorTarget(target.plid);
    if (editingTargetPlid.value === target.plid) cancelEditTarget();
    await loadTargets();
    targetManagerNotice.value =
      `PLID${target.plid} 已从后续监控移除，历史快照仍保留。`;
  } catch (error) {
    targetManagerError.value =
      error instanceof Error ? error.message : "删除竞品链接失败";
  } finally {
    targetManagerBusy.value = "";
  }
}

async function prioritizeTarget(
  target: CompetitorTargetItem,
  manualRetry = false,
) {
  if (!props.canOperate) {
    props.onPermissionDenied?.();
    return;
  }
  clearTargetManagerFeedback();
  targetManagerBusy.value = `priority:${target.plid}`;
  try {
    const priorityResult = await prioritizeCompetitorTarget(
      target.plid,
      manualRetry ? "manual_retry" : "manual",
    );
    const status = priorityResult.status;
    sharedBatchStatus.value = status;
    mergeQueuedTargetsIntoLocalBatch(status);
    targetManagerNotice.value = priorityResult.accepted
      ? manualRetry
        ? `PLID${target.plid} 的人工重试已记录并插队，等待当前商品完成后优先探测。`
        : `PLID${target.plid} 已插队，等待当前商品完成后优先探测；原队列位置继续保留。`
      : manualRetry
        ? `PLID${target.plid} 已在人工重试插队队列中，无需重复提交。`
        : `PLID${target.plid} 本批已经插队，无需重复提交。`;
  } catch (error) {
    targetManagerError.value =
      error instanceof Error ? error.message : "竞品链接插队失败";
  } finally {
    targetManagerBusy.value = "";
  }
}

function targetPriorityLabel(plid: string) {
  const state = prioritizedTargetStates.value.get(plid);
  if (!state) return "插队";
  return state.source === "automatic" ? "已自动插队" : "已插队";
}

function targetActionPriorityLabel(plid: string) {
  if (!targetActionIsManualRetry.value) return targetPriorityLabel(plid);
  return pendingPriorityTargetPlids.value.has(plid)
    ? "人工重试已插队"
    : "人工重试并插队";
}

async function loadTargetAudits(page = 1) {
  if (
    targetAuditStartDate.value
    && targetAuditEndDate.value
    && targetAuditStartDate.value > targetAuditEndDate.value
  ) {
    targetAuditError.value = "开始日期不能晚于结束日期";
    return;
  }
  targetAuditLoading.value = true;
  targetAuditError.value = "";
  try {
    const result = await fetchCompetitorTargetAudits(
      targetAuditStartDate.value,
      targetAuditEndDate.value,
      page,
      targetAuditPageSize,
    );
    targetAuditItems.value = result.items;
    targetAuditTotal.value = result.total;
    targetAuditPage.value = result.page;
    targetAuditLoaded.value = true;
  } catch (error) {
    targetAuditError.value =
      error instanceof Error ? error.message : "读取链接操作记录失败";
  } finally {
    targetAuditLoading.value = false;
  }
}

function mergeQueuedTargetsIntoLocalBatch(status: CompetitorBatchStatus) {
  pendingPriorityTargets.value =
    status.active && batchId.value && status.batch_id === batchId.value
      ? [...(status.priority_targets ?? [])]
      : [];
  if (!status.active || !batchId.value || status.batch_id !== batchId.value) {
    return;
  }
  const knownPlids = new Set(batchUrls.value.map(plidFromUrl));
  let appended = 0;
  for (const target of status.queued_targets ?? []) {
    if (knownPlids.has(target.plid)) continue;
    batchUrls.value.push(target.url);
    knownPlids.add(target.plid);
    appended += 1;
  }
  if (!appended) return;
  rawUrls.value = batchUrls.value.join("\n");
  total.value = batchUrls.value.length;
  showCollectionActivityNotice(
    `监控清单新增了 ${appended} 个链接，已加入当前批次队头；当前商品结束后优先探测。`,
  );
  persistCollectionCheckpoint();
}

function applyQueuedTargetsToRunQueue(
  queue: CollectionQueueItem[],
  cursor: number,
  knownIndexes: Set<number>,
) {
  if (activeIndex.value !== null && activeRequestId.value) return;
  let insertionCursor = cursor;
  for (const target of sharedBatchStatus.value.queued_targets ?? []) {
    const index = batchUrls.value.findIndex(
      (url) => plidFromUrl(url) === target.plid,
    );
    if (index < 0) continue;
    const existingPosition = queue.findIndex(
      (item, position) =>
        position >= cursor && item.index === index && !item.priority,
    );
    if (existingPosition >= 0) {
      const [item] = queue.splice(existingPosition, 1);
      if (item) {
        queue.splice(insertionCursor, 0, item);
        insertionCursor += 1;
      }
      knownIndexes.add(index);
      continue;
    }
    if (attemptedIndexes.value.includes(index) || knownIndexes.has(index)) {
      continue;
    }
    const pendingItem = resumeQueue.value.find((item) => item.index === index);
    if (!pendingItem) continue;
    queue.splice(insertionCursor, 0, pendingItem);
    insertionCursor += 1;
    knownIndexes.add(index);
  }
}

function applyPriorityTargetsToRunQueue(
  queue: CollectionQueueItem[],
  cursor: number,
  knownIndexes: Set<number>,
) {
  if (!pendingPriorityTargets.value.length) return;
  const prioritized: CollectionQueueItem[] = [];
  const prioritizedIndexes = new Set<number>();
  for (const target of pendingPriorityTargets.value) {
    let index = batchUrls.value.findIndex(
      (url) => plidFromUrl(url) === target.plid,
    );
    if (index < 0) {
      batchUrls.value.push(target.url);
      index = batchUrls.value.length - 1;
      rawUrls.value = batchUrls.value.join("\n");
      total.value = batchUrls.value.length;
    }
    const originalStillQueued = queue.some(
      (item, position) =>
        position >= cursor && item.index === index && !item.priority,
    );
    if (
      !originalStillQueued
      && !attemptedIndexes.value.includes(index)
      && !knownIndexes.has(index)
    ) {
      queue.push({ index, url: batchUrls.value[index]! });
      knownIndexes.add(index);
    }
    if (prioritizedIndexes.has(index)) continue;
    const priorityAlreadyQueued = queue.some(
      (item, position) =>
        position >= cursor && item.index === index && item.priority,
    );
    if (priorityAlreadyQueued) continue;
    prioritized.push({
      index,
      url: batchUrls.value[index]!,
      priority: true,
    });
    prioritizedIndexes.add(index);
  }
  if (!prioritized.length) return;
  queue.splice(cursor, 0, ...prioritized);
  pendingPriorityTargets.value = [];
  persistCollectionCheckpoint();
}

function appendPendingItemsToRunQueue(
  queue: CollectionQueueItem[],
  knownIndexes: Set<number>,
  cursor: number,
) {
  const stockUnprobed = new Set(stockUnprobedIndexes.value);
  for (const item of resumeQueue.value) {
    if (knownIndexes.has(item.index)) continue;
    const firstDeferredPosition = queue.findIndex(
      (queuedItem, position) =>
        position >= cursor && stockUnprobed.has(queuedItem.index),
    );
    if (!stockUnprobed.has(item.index) && firstDeferredPosition >= 0) {
      queue.splice(firstDeferredPosition, 0, item);
    } else {
      queue.push(item);
    }
    knownIndexes.add(item.index);
  }
}

function validateCompetitorUrl(value: string): string | null {
  if (!value) return "请输入 Takealot 商品链接";
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

function localDateInput(daysAgo: number) {
  const value = new Date();
  value.setDate(value.getDate() - daysAgo);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function targetAuditActionLabel(action: CompetitorTargetAuditItem["action"]) {
  return {
    add: "新增",
    update: "修改",
    delete: "删除",
    manual_retry: "人工重试",
    auto_discover: "自动发现跟卖",
  }[action];
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

function competitorSearchTerm(value: string) {
  const trimmed = value.trim();
  return (plidFromUrl(trimmed) || trimmed).toLocaleLowerCase();
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

function markStockUnprobed(index: number, unprobed: boolean) {
  const indexes = new Set(stockUnprobedIndexes.value);
  if (unprobed) indexes.add(index);
  else indexes.delete(index);
  stockUnprobedIndexes.value = [...indexes].sort((first, second) => first - second);
}

function removeCollectionError(plid: string) {
  collectionErrors.value = collectionErrors.value.filter(
    (item) => item.plid !== plid,
  );
}

function normalizeCollectionError(
  item: unknown,
): CollectionErrorItem {
  if (typeof item === "object" && item !== null) {
    const candidate = item as Partial<CollectionErrorItem>;
    const plid = typeof candidate.plid === "string" ? candidate.plid : "";
    return {
      plid,
      url:
        (typeof candidate.url === "string" ? candidate.url : "")
        || targetUrlForPlid(plid),
      message:
        typeof candidate.message === "string"
          ? candidate.message
          : "旧版待重试详情格式不完整",
    };
  }
  if (typeof item !== "string") {
    return { plid: "", url: "", message: "旧版待重试详情格式不完整" };
  }
  const matched = item.match(/^PLID(\d+)：?(.*)$/s);
  const plid = matched?.[1] ?? "";
  return {
    plid,
    url: plid ? targetUrlForPlid(plid) : "",
    message: matched?.[2] || item,
  };
}

function persistCollectionCheckpoint() {
  if (!batchUrls.value.length) return;
  const checkpoint: CollectionCheckpoint = {
    version: 7,
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
    stockUnprobedIndexes: stockUnprobedIndexes.value,
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
    ![1, 2, 3, 4, 5, 6, 7].includes(checkpoint.version)
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
  stockUnprobedIndexes.value = Array.isArray(checkpoint.stockUnprobedIndexes)
    ? checkpoint.stockUnprobedIndexes
    : [];
  collectionResults.value = checkpoint.results.map((result) => ({
    ...result,
    url: result.url || targetUrlForPlid(result.plid),
  }));
  collectionErrors.value = checkpoint.errors.map(normalizeCollectionError);
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
  if (checkpoint.version < 7) {
    persistCollectionCheckpoint();
  }
}

async function startCollection() {
  if (!props.canControlCollection) {
    showCollectionNotice(
      "竞品批次的开始、继续和停止仅限 kxx 账号；当前账号仍可新增链接和插队。",
    );
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
    const urls = targets.value.map((target) => target.url);
    if (!urls.length) throw new Error("请先在监控链接清单中新增至少一个商品");
    collectionResults.value = [];
    collectionErrors.value = [];
    collectionStopReason.value = "";
    collectionActivityNotice.value = "";
    autoResumeAt.value = null;
    completed.value = 0;
    batchUrls.value = urls;
    rawUrls.value = urls.join("\n");
    attemptedIndexes.value = [];
    failedIndexes.value = [];
    terminalIndexes.value = [];
    stockUnprobedIndexes.value = [];
    pendingPriorityTargets.value = [];
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
      {
        plid: "",
        url: "",
        message: error instanceof Error ? error.message : "无法开始采集",
      },
    ];
  }
}

async function resumeCollection(
  mode: "resume" | "auto_resume" | "scheduled_resume" = "resume",
) {
  if (!props.canControlCollection) {
    showCollectionNotice(
      "竞品批次的开始、继续和停止仅限 kxx 账号；当前账号仍可新增链接和插队。",
    );
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
  if (!batchId.value) batchId.value = collectionId("batch");
  if (
    mode === "auto_resume"
    && sharedBatchMatchesCheckpoint.value
    && typeof sharedBatchStatus.value.current_index === "number"
    && typeof sharedBatchStatus.value.current_request_id === "string"
  ) {
    activeIndex.value = sharedBatchStatus.value.current_index;
    activeRequestId.value = sharedBatchStatus.value.current_request_id;
  }
  const queue = [...resumeQueue.value];
  if (
    mode === "auto_resume"
    && activeIndex.value !== null
    && activeRequestId.value
  ) {
    const interruptedQueueIndex = queue.findIndex(
      (item) => item.index === activeIndex.value,
    );
    if (interruptedQueueIndex > 0) {
      const [interruptedItem] = queue.splice(interruptedQueueIndex, 1);
      if (interruptedItem) queue.unshift(interruptedItem);
    } else if (interruptedQueueIndex < 0) {
      const interruptedUrl = batchUrls.value[activeIndex.value];
      if (interruptedUrl) {
        queue.unshift({ index: activeIndex.value, url: interruptedUrl });
      }
    }
  }
  collectionStopReason.value = "";
  collectionActivityNotice.value = "";
  manualStopRequested.value = false;
  persistCollectionCheckpoint();
  await runCollection(queue, mode);
}

async function resumeInterruptedCollection() {
  if (
    !props.canControlCollection
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

function showCollectionActivityNotice(message: string) {
  collectionActivityNotice.value = message;
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
  if (!props.canControlCollection) {
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
    const status = await logCompetitorBatchEvent({
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
    sharedBatchStatus.value = status;
    mergeQueuedTargetsIntoLocalBatch(status);
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
  let consecutiveConnectionFailureDetails: string[] = [];
  let batchLeaseConflict = false;
  let cursor = 0;
  const knownIndexes = new Set(queue.map((item) => item.index));
  const stockProbeRetryCounts = new Map<number, number>();
  const ordinaryRetryCounts = new Map<number, number>();
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
    while (!controller.signal.aborted && !collectionStopReason.value) {
      applyQueuedTargetsToRunQueue(queue, cursor, knownIndexes);
      applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
      while (cursor < queue.length) {
        if (controller.signal.aborted) break;
        applyQueuedTargetsToRunQueue(queue, cursor, knownIndexes);
        applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
        const { index, url, priority = false } = queue[cursor]!;
        cursor += 1;
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
          const resultWithUrl = { ...result, url };
          collectionResults.value = [
            ...collectionResults.value.filter((item) => item.plid !== result.plid),
            resultWithUrl,
          ];
          if ((result.added_target_count ?? 0) > 0) {
            await loadTargets();
            showCollectionActivityNotice(
              `PLID${result.plid} 自动发现并加入 ${result.added_target_count} 条跟卖链接；已合并到同一下拉组。`,
            );
          }
          markFailed(index, false);
          markTerminal(index, false);
          markStockUnprobed(index, false);
          stockProbeRetryCounts.delete(index);
          ordinaryRetryCounts.delete(index);
          consecutiveConnectionFailures = 0;
          consecutiveConnectionFailureDetails = [];
          settled = true;
        } catch (error) {
          if (controller.signal.aborted) break;
          const message = error instanceof Error ? error.message : "采集失败";
          const leaseConflict =
            error instanceof ApiRequestError && error.status === 423;
          if (leaseConflict) {
            batchLeaseConflict = true;
            showCollectionActivityNotice(
              `${message}。本页面已停止重复续爬，服务端现有采集会继续运行。`,
            );
          } else {
            collectionErrors.value.push({ plid, url, message });
            const confirmedInvalid =
              error instanceof ApiRequestError && error.status === 410;
            const stockUnprobed =
              error instanceof ApiRequestError && error.status === 424;
            if (!priority) {
              markFailed(index, !confirmedInvalid);
              markTerminal(index, confirmedInvalid);
              markStockUnprobed(index, stockUnprobed);
            }
            if (stockUnprobed && !priority) {
              const retryCount = (stockProbeRetryCounts.get(index) ?? 0) + 1;
              stockProbeRetryCounts.set(index, retryCount);
              if (retryCount <= 2) {
                const retrySchedule = scheduleRetryAfterGap(
                  queue,
                  cursor,
                  { index, url },
                  retryCount,
                );
                showCollectionActivityNotice(
                  retrySchedule.scheduled
                    ? `PLID${plid}：${message}；第 ${retryCount} 次库存复探已安排在间隔 ${retrySchedule.gap} 个任务后。`
                    : `PLID${plid}：${message}；本轮剩余任务不足以间隔 ${retrySchedule.gap} 个任务，已保留待重试。`,
                );
              } else {
                showCollectionActivityNotice(
                  `PLID${plid}：${message}；连续 3 次库存未探测，已保留在待重试，本轮不再占住整批。`,
                );
              }
            } else {
              stockProbeRetryCounts.delete(index);
            }
            const tailRetryable =
              !priority
              && !confirmedInvalid
              && !stockUnprobed
              && error instanceof ApiRequestError
              && (
                error.status === 0
                || error.status === 404
                || error.status === 409
                || error.status >= 500
              );
            if (tailRetryable) {
              const retryCount = (ordinaryRetryCounts.get(index) ?? 0) + 1;
              ordinaryRetryCounts.set(index, retryCount);
              if (retryCount <= MAX_AUTOMATIC_RETRY_ATTEMPTS) {
                const retrySchedule = scheduleRetryAfterGap(
                  queue,
                  cursor,
                  { index, url },
                  retryCount,
                );
                showCollectionActivityNotice(
                  retrySchedule.scheduled
                    ? `PLID${plid}：${message}；第 ${retryCount} 次自动重试已安排在间隔 ${retrySchedule.gap} 个任务后。`
                    : `PLID${plid}：${message}；本轮剩余任务不足以间隔 ${retrySchedule.gap} 个任务，已保留待重试，可从详情中人工重试。`,
                );
              } else {
                showCollectionActivityNotice(
                  `PLID${plid}：${message}；本轮 1/2/4 间隔自动重试仍未成功，已保留在待重试。`,
                );
              }
            } else if (!stockUnprobed) {
              ordinaryRetryCounts.delete(index);
            }
            const isConnectionFailure =
              error instanceof ApiRequestError
              && (error.status === 0 || error.status >= 500);
            if (isConnectionFailure) {
              consecutiveConnectionFailures += 1;
              const statusLabel =
                error.status === 0 ? "浏览器连接失败" : `HTTP ${error.status}`;
              consecutiveConnectionFailureDetails = [
                ...consecutiveConnectionFailureDetails,
                `PLID${plid}（${statusLabel}）：${message}`,
              ].slice(-2);
            } else {
              consecutiveConnectionFailures = 0;
              consecutiveConnectionFailureDetails = [];
            }
            if (consecutiveConnectionFailures >= 2) {
              scheduleAutomaticResume(
                `连续 2 次发生真实连接失败或 Takealot 临时服务错误：${consecutiveConnectionFailureDetails.join("；")}。已暂停剩余链接。`,
              );
            }
            settled = true;
          }
        } finally {
          if (settled && !priority) markAttempted(index);
          activeIndex.value = null;
          activeRequestId.value = null;
          activeStartedAt.value = null;
          persistCollectionCheckpoint();
        }
        if (batchLeaseConflict) break;
        await recordBatchEvent("progress");
        appendPendingItemsToRunQueue(queue, knownIndexes, cursor);
        if (collectionStopReason.value || controller.signal.aborted) break;
        if (cursor < queue.length) await delay(1_000);
      }
      if (
        batchLeaseConflict
        || controller.signal.aborted
        || collectionStopReason.value
      ) break;
      await loadSharedBatchStatus();
      appendPendingItemsToRunQueue(queue, knownIndexes, cursor);
      applyQueuedTargetsToRunQueue(queue, cursor, knownIndexes);
      applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
      if (cursor >= queue.length) break;
    }
    await Promise.all([loadOverview(), loadTargets()]);
  } finally {
    if (batchLeaseConflict) {
      await loadSharedBatchStatus();
    } else if (!manualStopRequested.value && collectionStopReason.value) {
      await recordBatchEvent("paused", collectionStopReason.value);
    } else if (!manualStopRequested.value && !controller.signal.aborted) {
      clearAutomaticResumeSchedule();
      await recordBatchEvent(
        "completed",
        pendingResumeCount.value
          ? `本轮结束，仍有 ${pendingResumeCount.value} 个待重试或未完成链接`
          : "本批全部链接已检查",
      );
    }
    collecting.value = false;
    abortController.value = null;
    persistCollectionCheckpoint();
  }
}

function stopCollection() {
  if (!props.canControlCollection) {
    showCollectionNotice(
      "竞品批次的开始、继续和停止仅限 kxx 账号；当前账号仍可新增链接和插队。",
    );
    return;
  }
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

function formatSignedCurrency(value: number | null) {
  if (value === null) return "—";
  return `${value > 0 ? "+" : ""}${formatCurrency(value)}`;
}

function priceSignalClass(signal: string) {
  return {
    "price-down": signal === "降价",
    "price-up": signal === "涨价",
    "price-flat": signal === "价格不变",
  };
}

function formatSignedQuantity(value: number | null) {
  if (value === null) return "—";
  return `${value > 0 ? "+" : ""}${value} 件`;
}

function offerStockDisplay(offer: CompetitorOfferItem) {
  if (offer.库存数量 === null) {
    return `${offer.库存状态 || "未知"}（数量未返回）`;
  }
  return offer.库存精确
    ? `${offer.库存数量} 件`
    : `至少 ${offer.库存数量} 件`;
}

function offerStockEvidenceLabel(offer: CompetitorOfferItem) {
  if (offer.库存数量 === null) {
    return offer.库存方式
      ? `${offer.库存方式} · 未取得数量`
      : "未取得库存数量";
  }
  if (offer.库存精确) return "精确库存证据";
  return offer.库存方式
    ? `${offer.库存方式} · 非精确下限`
    : "非精确库存下限";
}

function offerStockSignalClass(signal: string) {
  return {
    "stock-increase": signal === "库存增加" || signal === "恢复有货",
    "stock-decrease": signal === "库存减少" || signal === "转为没货",
    "stock-flat": signal === "库存数量不变" || signal === "库存状态不变",
  };
}

function competitorOfferPriceRange(item: CompetitorItem) {
  const prices = item.跟卖报价
    .map((offer) => offer.价格)
    .filter((price): price is number => price !== null)
    .sort((first, second) => first - second);
  if (!prices.length) return formatCurrency(item.价格);
  const lowest = prices[0]!;
  const highest = prices[prices.length - 1]!;
  return lowest === highest
    ? formatCurrency(lowest)
    : `${formatCurrency(lowest)} – ${formatCurrency(highest)}`;
}

function targetSnapshot(target: CompetitorTargetItem) {
  return competitorsByPlid.value.get(target.plid) ?? null;
}

function targetOffers(target: CompetitorTargetItem) {
  return targetSnapshot(target)?.跟卖报价 ?? [];
}

function targetGroupTitle(group: CompetitorTargetGroup) {
  const primary =
    group.members.find((target) => target.plid === group.groupPlid)
    ?? group.members[0];
  return primary?.title || `PLID${primary?.plid ?? group.groupPlid}`;
}

function targetGroupPriceSummary(group: CompetitorTargetGroup) {
  const prices = group.members
    .flatMap((target) => {
      const offerPrices = targetOffers(target).map((offer) => offer.价格);
      return offerPrices.length
        ? offerPrices
        : [targetSnapshot(target)?.价格 ?? null];
    })
    .filter((price): price is number => price !== null)
    .sort((first, second) => first - second);
  if (!prices.length) return "价格待采集";
  const lowest = prices[0]!;
  const highest = prices[prices.length - 1]!;
  return lowest === highest
    ? formatCurrency(lowest)
    : `${formatCurrency(lowest)} – ${formatCurrency(highest)}`;
}

function targetGroupOfferCount(group: CompetitorTargetGroup) {
  return group.members.reduce(
    (total, target) => total + targetOffers(target).length,
    0,
  );
}

function toggleTargetGroup(groupPlid: string) {
  const next = new Set(expandedTargetGroupPlids.value);
  if (next.has(groupPlid)) next.delete(groupPlid);
  else next.add(groupPlid);
  expandedTargetGroupPlids.value = next;
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
          <p class="section-kicker">持久化监控清单</p>
          <h2>竞品链接管理</h2>
        </div>
        <p class="section-note">
          新增链接会自动加入当前运行批次队头；插队只增加优先探测，不移除原位置
        </p>
      </div>
      <form class="target-add-row" @submit.prevent="addTarget">
        <input
          v-model="newTargetUrl"
          type="url"
          aria-label="新增 Takealot 竞品链接"
          placeholder="粘贴 Takealot 商品链接，例如 https://www.takealot.com/.../PLID12345678"
          :disabled="targetManagerBusy === 'add' || !props.canOperate"
          @input="clearTargetManagerFeedback"
        />
        <button
          class="primary-button"
          type="submit"
          :disabled="targetManagerBusy === 'add' || !props.canOperate"
        >
          {{ targetManagerBusy === "add" ? "正在新增…" : "新增链接" }}
        </button>
      </form>
      <p v-if="targetManagerError" class="target-manager-message error" role="alert">
        {{ targetManagerError }}
      </p>
      <div
        v-if="duplicateTarget"
        class="target-duplicate-notice"
        role="status"
      >
        <span>
          {{
            duplicateTarget.hasHistory
              ? "该链接已在监控清单中，并已纳入每日采集且已有历史记录。"
              : "链接表单中已有这个链接。"
          }}
        </span>
        <button
          v-if="duplicateTarget.hasHistory"
          class="secondary-button"
          type="button"
          @click="jumpToDuplicateTarget"
        >
          查看对应商品
        </button>
      </div>
      <p v-if="targetManagerNotice" class="target-manager-message success" role="status">
        {{ targetManagerNotice }}
      </p>
      <template v-if="targets.length">
        <div class="target-list-panel">
          <button
            ref="targetListTrigger"
            class="target-list-open-button"
            type="button"
            aria-haspopup="dialog"
            @click="openTargetList"
          >
            <span>
              <strong>监控链接汇总</strong>
              <small>
                共 {{ targetGroups.length }} 组、{{ targets.length }} 条链接 · 已有历史 {{ targetsWithHistoryCount }} 条 ·
                待首次采集 {{ targetsPendingFirstCaptureCount }} 条
              </small>
            </span>
            <span>打开管理</span>
          </button>
        </div>
        <Teleport to="body">
          <div
            v-if="targetListOpen"
            class="competitor-modal-backdrop target-list-modal-backdrop"
            @click.self="closeTargetList"
          >
            <section
              class="competitor-modal target-list-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="target-list-modal-title"
            >
              <header class="competitor-modal-header target-list-modal-header">
                <div>
                  <p class="section-kicker">MONITORING LINKS</p>
                  <h2 id="target-list-modal-title">管理监控链接</h2>
                  <span>
                    共 {{ targetGroups.length }} 组、{{ targets.length }} 条链接 · 已有历史
                    {{ targetsWithHistoryCount }} 条 · 待首次采集
                    {{ targetsPendingFirstCaptureCount }} 条
                  </span>
                </div>
                <button
                  class="competitor-modal-close"
                  type="button"
                  aria-label="关闭监控链接管理"
                  @click="closeTargetList"
                >
                  ×
                </button>
              </header>
              <div class="target-list-content">
                <div class="target-list-toolbar">
                  <label>
                    <span>查找链接</span>
                    <input
                      v-model="targetQuery"
                      type="search"
                      placeholder="商品名称、PLID 或完整链接"
                    />
                  </label>
                  <label class="target-page-size-field">
                    <span>每页显示</span>
                    <select v-model.number="targetPageSize">
                      <option
                        v-for="size in targetPageSizeOptions"
                        :key="size"
                        :value="size"
                      >
                        {{ size }} 组
                      </option>
                    </select>
                  </label>
                  <span>
                    显示 {{ filteredTargetGroups.length }} 组、{{ targets.length }} 条链接，
                    每页 {{ targetPageSize }} 组
                  </span>
                </div>
                <div v-if="pagedTargetGroups.length" class="target-list">
                  <section
                    v-for="group in pagedTargetGroups"
                    :key="group.groupPlid"
                    class="target-group"
                  >
                    <button
                      class="target-group-toggle"
                      type="button"
                      :aria-expanded="expandedTargetGroupPlids.has(group.groupPlid)"
                      @click="toggleTargetGroup(group.groupPlid)"
                    >
                      <span>
                        <strong>{{ targetGroupTitle(group) }}</strong>
                        <small>
                          {{ group.members.length }} 条商品链接 ·
                          {{ targetGroupOfferCount(group) }} 个卖家报价 ·
                          {{ targetGroupPriceSummary(group) }}
                        </small>
                      </span>
                      <span aria-hidden="true">
                        {{ expandedTargetGroupPlids.has(group.groupPlid) ? "收起" : "展开" }}
                        {{ expandedTargetGroupPlids.has(group.groupPlid) ? "▴" : "▾" }}
                      </span>
                    </button>
                    <div
                      v-if="expandedTargetGroupPlids.has(group.groupPlid)"
                      class="target-group-members"
                    >
                      <article
                        v-for="target in group.members"
                        :key="target.plid"
                        class="target-row"
                      >
                        <div class="target-identity">
                          <strong>PLID{{ target.plid }}</strong>
                          <span v-if="targetSnapshot(target)">
                            {{ targetSnapshot(target)?.当前卖家 || "未知卖家" }} ·
                            {{ formatCurrency(targetSnapshot(target)?.价格 ?? null) }}
                          </span>
                          <span v-else>待首次采集价格</span>
                        </div>
                        <template v-if="editingTargetPlid === target.plid">
                          <input
                            v-model="editingTargetUrl"
                            class="target-edit-input"
                            type="url"
                            :aria-label="`修改 PLID${target.plid} 链接`"
                            :disabled="targetManagerBusy === target.plid"
                          />
                          <div class="target-row-actions">
                            <button
                              class="secondary-button"
                              type="button"
                              :disabled="targetManagerBusy === target.plid"
                              @click="saveTargetEdit(target.plid)"
                            >保存</button>
                            <button
                              class="secondary-button"
                              type="button"
                              :disabled="targetManagerBusy === target.plid"
                              @click="cancelEditTarget"
                            >取消</button>
                          </div>
                        </template>
                        <template v-else>
                          <a :href="target.url" target="_blank" rel="noreferrer">
                            {{ target.url }}
                          </a>
                          <div class="target-row-actions">
                            <button
                              class="secondary-button priority"
                              type="button"
                              :disabled="
                                Boolean(targetManagerBusy)
                                || !props.canOperate
                                || !sharedBatchStatus.active
                                || sharedBatchStatus.current_plid === target.plid
                                || prioritizedTargetStates.has(target.plid)
                              "
                              :title="
                                prioritizedTargetStates.has(target.plid)
                                  ? targetPriorityLabel(target.plid)
                                  : sharedBatchStatus.active
                                    ? '额外插到当前商品之后，原队列位置继续保留'
                                    : '当前没有运行中的竞品批次'
                              "
                              @click="prioritizeTarget(target)"
                            >
                              {{
                                targetManagerBusy === `priority:${target.plid}`
                                  ? "插队中…"
                                  : targetPriorityLabel(target.plid)
                              }}
                            </button>
                            <button
                              class="secondary-button"
                              type="button"
                              :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                              @click="beginEditTarget(target)"
                            >修改</button>
                            <button
                              class="secondary-button danger"
                              type="button"
                              :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                              @click="removeTarget(target)"
                            >删除</button>
                          </div>
                        </template>
                        <div
                          v-if="!editingTargetPlid || editingTargetPlid !== target.plid"
                          class="target-offer-list"
                        >
                          <p class="target-offer-list-heading">
                            <strong>原链接及跟卖报价</strong>
                            <span>同一 PLID 只入队一次，价格和库存均按 Offer ID 区分</span>
                          </p>
                          <article
                            v-for="offer in targetOffers(target)"
                            :key="offer.报价键"
                            class="target-offer-row"
                          >
                            <div>
                              <strong>
                                {{ offer.卖家 }}
                                <span v-if="offer.是否主报价" class="offer-selected-badge">
                                  当前主报价
                                </span>
                              </strong>
                              <small>
                                {{
                                  offer.offer_id
                                    ? `Offer ${offer.offer_id}`
                                    : "Offer ID 未公开（按卖家/SKU跟踪）"
                                }}
                                <template v-if="offer.SKU"> · SKU {{ offer.SKU }}</template>
                                <template v-if="offer.变体 && offer.变体 !== '默认款'">
                                  · {{ offer.变体 }}
                                </template>
                                <template v-if="offer.条件"> · {{ offer.条件 }}</template>
                              </small>
                            </div>
                            <div class="target-offer-price">
                              <strong>{{ formatCurrency(offer.价格) }}</strong>
                              <small
                                class="price-signal"
                                :class="priceSignalClass(offer.价格信号)"
                              >
                                {{ offer.价格信号 }}
                                <template v-if="offer.价格变化 !== null">
                                  · {{ formatSignedCurrency(offer.价格变化) }}
                                </template>
                              </small>
                            </div>
                            <div
                              class="target-offer-stock"
                              :title="offer.库存说明 || offer.库存原始状态"
                            >
                              <strong>{{ offerStockDisplay(offer) }}</strong>
                              <small :class="offerStockSignalClass(offer.库存信号)">
                                {{ offer.库存信号 }}
                                <template v-if="offer.库存数量变化 !== null">
                                  · {{ formatSignedQuantity(offer.库存数量变化) }}
                                </template>
                              </small>
                            </div>
                            <a
                              :href="offer.链接 || target.url"
                              target="_blank"
                              rel="noreferrer"
                            >打开商品页</a>
                          </article>
                          <p
                            v-if="!targetOffers(target).length"
                            class="target-offer-empty"
                          >
                            {{
                              target.has_history
                                ? "最近快照未返回可区分的公开卖家报价，原链接仍保留。"
                                : "首次采集后将在这里显示全部公开卖家报价。"
                            }}
                          </p>
                        </div>
                      </article>
                    </div>
                  </section>
                </div>
                <p v-else class="target-empty">没有匹配的监控链接。</p>
                <div
                  v-if="filteredTargetGroups.length > targetPageSize"
                  class="compact-pagination"
                >
                  <button
                    class="secondary-button"
                    type="button"
                    :disabled="targetPage <= 1"
                    @click="targetPage -= 1"
                  >
                    上一页
                  </button>
                  <span>第 {{ targetPage }} / {{ targetPageCount }} 页</span>
                  <button
                    class="secondary-button"
                    type="button"
                    :disabled="targetPage >= targetPageCount"
                    @click="targetPage += 1"
                  >
                    下一页
                  </button>
                </div>
              </div>
              <div class="competitor-modal-actions">
                <button type="button" @click="closeTargetList">关闭</button>
              </div>
            </section>
          </div>
        </Teleport>
      </template>
      <p v-else class="target-empty">
        监控清单为空。新增第一条链接后即可开始采集。
      </p>
      <div class="target-audit-panel">
        <button
          ref="targetAuditTrigger"
          class="target-audit-open-button"
          type="button"
          aria-haspopup="dialog"
          @click="openTargetAudit"
        >
          <span>
            <strong>链接操作记录</strong>
            <small>默认收起，需要时按日期查看用户增删改留痕</small>
          </span>
          <span>打开记录</span>
        </button>
      </div>
      <Teleport to="body">
        <div
          v-if="targetAuditOpen"
          class="competitor-modal-backdrop target-audit-modal-backdrop"
          @click.self="closeTargetAudit"
        >
          <section
            class="competitor-modal target-audit-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="target-audit-modal-title"
          >
            <header class="competitor-modal-header target-audit-modal-header">
              <div>
                <p class="section-kicker">LINK AUDIT</p>
                <h2 id="target-audit-modal-title">链接操作记录</h2>
                <span>按北京时间日期查看用户对监控链接的增删改留痕</span>
              </div>
              <button
                class="competitor-modal-close"
                type="button"
                aria-label="关闭链接操作记录"
                @click="closeTargetAudit"
              >
                ×
              </button>
            </header>
            <div class="target-audit-content">
              <div class="target-audit-filters">
                <label>
                  开始日期
                  <input v-model="targetAuditStartDate" type="date" />
                </label>
                <label>
                  结束日期
                  <input v-model="targetAuditEndDate" type="date" />
                </label>
                <button
                  class="secondary-button"
                  type="button"
                  :disabled="targetAuditLoading"
                  @click="loadTargetAudits(1)"
                >
                  {{ targetAuditLoading ? "查询中…" : "查询记录" }}
                </button>
              </div>
              <p
                v-if="targetAuditError"
                class="target-manager-message error"
                role="alert"
              >
                {{ targetAuditError }}
              </p>
              <div v-if="targetAuditItems.length" class="target-audit-table-wrap">
                <p class="target-audit-summary">
                  共 {{ targetAuditTotal }} 条操作记录，当前显示第
                  {{ targetAuditPage }} 页
                </p>
                <table class="target-audit-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用户</th>
                      <th>动作</th>
                      <th>PLID</th>
                      <th>变更内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="audit in targetAuditItems" :key="audit.id">
                      <td>{{ formatChinaDateTime(audit.changed_at) }}</td>
                      <td>{{ audit.actor_display_name || audit.actor_username }}</td>
                      <td>
                        <span :class="['audit-action', `is-${audit.action}`]">
                          {{ targetAuditActionLabel(audit.action) }}
                        </span>
                      </td>
                      <td>PLID{{ audit.plid }}</td>
                      <td>
                        <span v-if="audit.old_url" class="audit-url old">
                          原：{{ audit.old_url }}
                        </span>
                        <span v-if="audit.new_url" class="audit-url">
                          新：{{ audit.new_url }}
                        </span>
                      </td>
                    </tr>
                  </tbody>
                </table>
                <div
                  v-if="targetAuditTotal > targetAuditPageSize"
                  class="compact-pagination"
                >
                  <button
                    class="secondary-button"
                    type="button"
                    :disabled="targetAuditLoading || targetAuditPage <= 1"
                    @click="loadTargetAudits(targetAuditPage - 1)"
                  >
                    上一页
                  </button>
                  <span>第 {{ targetAuditPage }} / {{ targetAuditPageCount }} 页</span>
                  <button
                    class="secondary-button"
                    type="button"
                    :disabled="
                      targetAuditLoading || targetAuditPage >= targetAuditPageCount
                    "
                    @click="loadTargetAudits(targetAuditPage + 1)"
                  >
                    下一页
                  </button>
                </div>
              </div>
              <p
                v-else-if="!targetAuditLoading && !targetAuditError"
                class="target-empty"
              >
                所选日期区间没有链接操作记录。
              </p>
            </div>
            <div class="competitor-modal-actions">
              <button type="button" @click="closeTargetAudit">关闭</button>
            </div>
          </section>
        </div>
      </Teleport>
      <div class="collector-run-heading">
        <div>
          <p class="section-kicker">建立与刷新观察样本</p>
          <h3>批量采集当前监控清单</h3>
        </div>
        <span>共 {{ targets.length }} 条活跃链接</span>
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
            · 待重试 {{ sharedBatchStatus.failed }}
            · 确认失效 {{ retainedConfirmedInvalidCount }}
            · 待续爬 {{ sharedBatchStatus.pending }}
          </span>
        </div>
        <span v-if="sharedBatchStatus.current_plid" class="shared-current-plid">
          当前第 {{ (sharedBatchStatus.current_index ?? 0) + 1 }} 条 ·
          PLID{{ sharedBatchStatus.current_plid }}
        </span>
        <span v-else>正在准备下一条商品</span>
      </div>
      <div class="collector-actions">
        <label class="switch-row">
          <input
            v-model="withStockProbe"
            type="checkbox"
            :disabled="collecting || anotherBatchIsActive || !props.canControlCollection"
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
            @change="persistCollectionCheckpoint"
            :disabled="
              !withStockProbe
              || !props.canControlCollection
            "
          />
          <span class="switch"></span>
          <span>
            <strong>显示检测浏览器</strong>
            <small>运行中可切换，从下一条任务链接开始生效</small>
          </span>
        </label>
        <button
          class="primary-button"
          @click="startCollection"
          v-if="props.canControlCollection && !collecting"
          :disabled="
            (!targets.length && !pendingResumeCount)
            || anotherBatchIsActive
          "
        >
          开始采集
        </button>
        <button
          v-if="props.canControlCollection && !collecting && pendingResumeCount"
          class="primary-button resume-button"
          @click="resumeCollection()"
        >
          继续失败/未完成（{{ pendingResumeCount }}）
        </button>
        <button
          class="primary-button stop-button"
          @click="stopCollection"
          v-if="props.canControlCollection && collecting"
        >
          停止采集
        </button>
        <p
          v-if="props.canOperate && !props.canControlCollection"
          class="section-note"
        >
          当前账号可新增链接和插队；批次开始、继续与停止仅限 kxx 账号。
        </p>
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
      <div
        v-if="sharedBatchStatus.active || collecting || completed"
        class="progress-track"
        aria-live="polite"
      >
        <span :style="{ width: `${progress}%` }"></span>
      </div>
      <div
        v-if="sharedBatchStatus.active || collecting"
        class="collection-active-status"
        role="status"
        aria-live="polite"
      >
        <strong>{{ activeCollectionStatus }}</strong>
        <span>{{ activeCollectionHint }}</span>
        <span v-if="collectionActivityNotice">{{ collectionActivityNotice }}</span>
      </div>
      <p v-if="collecting" class="method-note collection-persistence-note">
        采集正在后台继续；切换到其他页面后再返回，进度和结果仍会保留。
      </p>
      <div
        v-if="hasDisplayedBatchProgress"
        class="result-strip"
      >
        <span v-if="displayedBatchSucceeded" class="result-good">
          成功 {{ displayedBatchSucceeded }} 个
        </span>
        <span v-if="displayedBatchFailed" class="result-bad">
          待重试 {{ displayedBatchFailed }} 个
        </span>
        <span v-if="retainedConfirmedInvalidCount" class="result-terminal">
          确认失效 {{ retainedConfirmedInvalidCount }} 个，长期保留
        </span>
        <span v-if="displayedBatchTotal">
          本批已检查 {{ displayedBatchCompleted }}/{{ displayedBatchTotal }}，待续爬
          {{ displayedBatchPending }} 个
        </span>
      </div>
      <details
        v-if="
          showLocalCollectionDetails
          && (collectionResults.length || collectionErrors.length)
        "
        class="collection-task-detail collection-task-detail-panel"
      >
        <summary>
          <span>
            <strong>任务爬取详情</strong>
            <small>成功与待重试任务在同一面板内分组查看</small>
          </span>
          <b>{{ collectionResults.length + collectionErrors.length }}</b>
        </summary>
        <div class="collection-task-detail-groups">
          <section v-if="collectionResults.length" class="collection-task-detail-group success">
            <header>
              <strong>成功任务</strong>
              <span>{{ collectionResults.length }} 个</span>
            </header>
            <div class="collection-task-detail-list">
              <article
                v-for="result in collectionResults"
                :key="result.plid"
                class="collection-task-link-action"
                tabindex="0"
                role="button"
                aria-haspopup="dialog"
                @click="
                  openTargetActionForLink(
                    result.plid,
                    result.url || targetUrlForPlid(result.plid),
                  )
                "
                @keydown.enter="
                  openTargetActionForLink(
                    result.plid,
                    result.url || targetUrlForPlid(result.plid),
                  )
                "
                @keydown.space.prevent="
                  openTargetActionForLink(
                    result.plid,
                    result.url || targetUrlForPlid(result.plid),
                  )
                "
              >
                <strong>PLID{{ result.plid }}</strong>
                <span>{{ result.title || "未取得商品名称" }}</span>
                <small>{{ result.message }}</small>
              </article>
            </div>
          </section>
          <section v-if="collectionErrors.length" class="collection-task-detail-group retry">
            <header>
              <strong>待重试任务</strong>
              <span>{{ collectionErrors.length }} 个</span>
            </header>
            <div class="collection-task-detail-list">
              <article
                v-for="error in collectionErrors"
                :key="`${error.plid}-${error.message}`"
                :class="{ 'collection-task-link-action': Boolean(error.plid && error.url) }"
                :tabindex="error.plid && error.url ? 0 : undefined"
                :role="error.plid && error.url ? 'button' : undefined"
                :aria-haspopup="error.plid && error.url ? 'dialog' : undefined"
                @click="
                  error.plid
                    && error.url
                    && openTargetActionForLink(error.plid, error.url, 'manual_retry')
                "
                @keydown.enter="
                  error.plid
                    && error.url
                    && openTargetActionForLink(error.plid, error.url, 'manual_retry')
                "
                @keydown.space.prevent="
                  error.plid
                    && error.url
                    && openTargetActionForLink(error.plid, error.url, 'manual_retry')
                "
              >
                <strong>{{ error.plid ? `PLID${error.plid}` : "采集任务" }}</strong>
                <span>{{ error.message }}</span>
                <small v-if="error.plid && error.url">点击可修改队列或发起人工重试</small>
              </article>
            </div>
          </section>
        </div>
      </details>
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

    <section
      v-if="linkHealth.length"
      class="panel link-health-panel"
      :class="{ 'is-collapsed': !linkHealthOpen }"
    >
      <div class="section-heading">
        <div>
          <p class="section-kicker">LINK REVIEW QUEUE</p>
          <h2>链接复核状态</h2>
        </div>
        <div class="link-health-heading-actions">
          <p class="section-note">
            疑似 {{ suspectedInvalidCount }} 个 · 确认失效 {{ confirmedInvalidCount }} 个
          </p>
          <button
            type="button"
            class="quiet-button link-health-toggle"
            :aria-expanded="linkHealthOpen"
            aria-controls="competitor-link-health-details"
            @click="linkHealthOpen = !linkHealthOpen"
          >
            {{ linkHealthOpen ? "收起状态" : "展开状态" }}
          </button>
        </div>
      </div>
      <div v-if="linkHealthOpen" id="competitor-link-health-details">
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
                class="link-health-row-action"
                tabindex="0"
                role="button"
                aria-haspopup="dialog"
                @click="openTargetAction(item)"
                @keydown.enter="openTargetAction(item)"
                @keydown.space.prevent="openTargetAction(item)"
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
                      <a
                        :href="item.url"
                        target="_blank"
                        rel="noopener noreferrer"
                        @click.stop
                      >
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
      </div>
    </section>

    <Teleport to="body">
      <div
        v-if="targetActionOpen"
        class="competitor-modal-backdrop target-action-modal-backdrop"
        @click.self="closeTargetAction"
      >
        <section
          class="competitor-modal target-action-modal"
          role="dialog"
          aria-modal="true"
          :aria-label="`PLID${targetActionPlid} 监控队列操作`"
        >
          <header class="competitor-modal-header">
            <div>
              <p class="section-kicker">MONITORING LINK</p>
              <h2>PLID{{ targetActionPlid }} 监控队列操作</h2>
              <span>
                {{
                  targetActionIsManualRetry
                    ? "从待重试任务打开；人工插队会记录操作者、时间和链接"
                    : targetActionTarget
                    ? "当前链接已在后续采集队列"
                    : "当前仅保留复核记录，链接不在后续采集队列"
                }}
              </span>
            </div>
            <button
              type="button"
              class="competitor-modal-close"
              aria-label="关闭监控队列操作"
              @click="closeTargetAction"
            >
              ×
            </button>
          </header>
          <div class="target-action-modal-content">
            <section class="panel competitor-target-action-card">
              <template v-if="targetActionTarget">
                <template v-if="editingTargetPlid === targetActionTarget.plid">
                  <input
                    v-model="editingTargetUrl"
                    class="target-edit-input"
                    type="url"
                    :aria-label="`修改 PLID${targetActionTarget.plid} 链接`"
                    :disabled="targetManagerBusy === targetActionTarget.plid"
                  />
                  <div class="competitor-target-action-buttons">
                    <button
                      class="primary-button"
                      type="button"
                      :disabled="targetManagerBusy === targetActionTarget.plid"
                      @click="saveTargetEdit(targetActionTarget.plid)"
                    >
                      {{
                        targetManagerBusy === targetActionTarget.plid
                          ? "保存中…"
                          : "保存修改"
                      }}
                    </button>
                    <button
                      class="quiet-button"
                      type="button"
                      :disabled="targetManagerBusy === targetActionTarget.plid"
                      @click="cancelEditTarget"
                    >
                      取消修改
                    </button>
                  </div>
                </template>
                <template v-else>
                  <a
                    class="competitor-target-current-url"
                    :href="targetActionTarget.url"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {{ targetActionTarget.url }}
                  </a>
                  <div class="competitor-target-action-buttons">
                    <button
                      class="secondary-button priority"
                      type="button"
                      :disabled="
                        Boolean(targetManagerBusy)
                        || !props.canOperate
                        || !sharedBatchStatus.active
                        || sharedBatchStatus.current_plid === targetActionTarget.plid
                        || (
                          targetActionIsManualRetry
                            ? pendingPriorityTargetPlids.has(targetActionTarget.plid)
                            : prioritizedTargetStates.has(targetActionTarget.plid)
                        )
                      "
                      @click="
                        prioritizeTarget(targetActionTarget, targetActionIsManualRetry)
                      "
                    >
                      {{
                        targetManagerBusy === `priority:${targetActionTarget.plid}`
                          ? targetActionIsManualRetry
                            ? "人工重试插队中…"
                            : "插队中…"
                          : targetActionPriorityLabel(targetActionTarget.plid)
                      }}
                    </button>
                    <button
                      class="secondary-button"
                      type="button"
                      :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                      @click="beginEditTarget(targetActionTarget)"
                    >
                      修改链接
                    </button>
                    <button
                      class="secondary-button danger"
                      type="button"
                      :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                      @click="removeTarget(targetActionTarget)"
                    >
                      删除链接
                    </button>
                  </div>
                </template>
              </template>
              <template v-else>
                <a
                  class="competitor-target-current-url"
                  :href="targetActionFallbackUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {{ targetActionFallbackUrl }}
                </a>
                <div class="competitor-target-action-buttons">
                  <button
                    class="primary-button"
                    type="button"
                    :disabled="targetManagerBusy === 'add' || !props.canOperate"
                    @click="addTargetActionTarget"
                  >
                    {{
                      targetManagerBusy === "add"
                        ? "加入中…"
                        : targetActionIsManualRetry
                          ? "重新加入并人工重试"
                          : "重新加入监控队列"
                    }}
                  </button>
                </div>
              </template>
              <p
                v-if="targetManagerError"
                class="target-manager-message error"
                role="alert"
              >
                {{ targetManagerError }}
              </p>
              <p
                v-if="targetManagerNotice"
                class="target-manager-message success"
                role="status"
              >
                {{ targetManagerNotice }}
              </p>
            </section>
          </div>
          <div class="competitor-modal-actions">
            <button type="button" @click="closeTargetAction">关闭</button>
          </div>
        </section>
      </div>
    </Teleport>

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
              placeholder="商品名称、PLID、完整链接或卖家"
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
            <span>每页显示</span>
            <select v-model.number="competitorPageSize">
              <option
                v-for="size in competitorPageSizeOptions"
                :key="size"
                :value="size"
              >
                {{ size }} 条
              </option>
            </select>
          </label>
          <div class="competitor-date-range">
            <div class="competitor-date-range-copy">
              <strong>观察区间（北京时间）</strong>
              <span>
                {{ activeRangeLabel }} · 显示
                {{ filteredCompetitors.length }} / {{ competitors.length }} 个商品
              </span>
            </div>
            <div class="competitor-date-range-controls">
              <label class="competitor-filter-field">
                <span>开始日期</span>
                <input
                  v-model="rangeStartDate"
                  type="date"
                  :min="competitorDateRange.available_start || undefined"
                  :max="rangeEndDate || competitorDateRange.available_end || undefined"
                />
              </label>
              <span class="competitor-date-range-separator" aria-hidden="true">至</span>
              <label class="competitor-filter-field">
                <span>结束日期</span>
                <input
                  v-model="rangeEndDate"
                  type="date"
                  :min="rangeStartDate || competitorDateRange.available_start || undefined"
                  :max="competitorDateRange.available_end || undefined"
                />
              </label>
              <button type="button" class="primary-button" @click="applyDateRange">
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
        </div>
        <p class="method-note">
          日期按北京时间自然日筛选。每个商品使用区间内最旧快照和最新快照重算价格涨跌、
          库存净变化、新增评论与经营信号；只有首尾变体键、SKU、卖家集合一致且库存均为精确值时才比较库存。
        </p>
        <div v-if="!filteredCompetitors.length" class="empty-state competitor-filter-empty">
          <strong>没有符合条件的竞品</strong>
          <span>可以调整关键词、库存状态或经营信号。</span>
        </div>
        <div v-else class="competitor-status-list">
          <article
            v-for="item in pagedCompetitors"
            :key="item.plid"
            :id="`competitor-row-${item.plid}`"
            v-memo="[item, selectedPlid === item.plid, failedCompetitorImages.has(item.图片 || '')]"
            class="competitor-status-card"
            :class="{ selected: selectedPlid === item.plid }"
            tabindex="0"
            role="button"
            aria-haspopup="dialog"
            :aria-label="`查看 ${item.商品} 及全部 ${item.跟卖报价.length} 个报价的详情`"
            @click="openProductModal(item)"
            @keydown.enter="openProductModal(item)"
            @keydown.space.prevent="openProductModal(item)"
          >
            <header class="competitor-status-header">
              <div class="competitor-status-identity">
                <div class="competitor-product-image competitor-status-image">
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
                <div class="competitor-status-title">
                  <div class="competitor-status-eyebrow">
                    <span>PLID{{ item.plid }}</span>
                    <span>{{ formatChinaDateTime(item.采集时间) }}</span>
                  </div>
                  <h3>{{ item.商品 }}</h3>
                  <p>{{ item.跟卖报价.length }} 个卖家报价 · 主卖家 {{ item.当前卖家 || "未知" }}</p>
                </div>
              </div>
              <span class="competitor-status-open">查看卖家库存 →</span>
            </header>

            <div class="competitor-status-summary">
              <div>
                <span>报价区间 / 主报价</span>
                <strong>{{ competitorOfferPriceRange(item) }}</strong>
                <small>主报价 {{ formatCurrency(item.价格) }}</small>
              </div>
              <div>
                <span>主报价库存</span>
                <strong
                  class="stock-pill"
                  :class="{
                    exact: item.库存精确,
                    unavailable: item.库存上限 === '没货',
                  }"
                >{{ item.库存上限 }}</strong>
                <small v-if="item.库存参考过期 && item.上次成功库存">
                  上次成功 {{ item.上次成功库存 }}
                  · {{ formatChinaDateTime(item.上次成功库存时间) }}
                </small>
                <small v-else>{{ item.当前卖家 || "未知卖家" }}</small>
              </div>
              <div>
                <span>经营信号</span>
                <div class="signal-labels">
                  <strong class="signal-label">{{ item.趋势判断 }}</strong>
                  <strong
                    class="signal-label price-signal"
                    :class="priceSignalClass(item.价格信号)"
                  >{{ item.价格信号 }}</strong>
                </div>
                <small v-if="item.价格变化 !== null">
                  价格变化 {{ formatSignedCurrency(item.价格变化) }}
                </small>
              </div>
              <div>
                <span>评论 / 评分</span>
                <strong>{{ item.评论数 }} 条 · {{ item.评分 ?? "—" }}</strong>
                <small>点击查看历史和评论</small>
              </div>
            </div>
          </article>
        </div>
        <div
          v-if="filteredCompetitors.length"
          class="compact-pagination competitor-pagination"
        >
          <button
            class="secondary-button"
            type="button"
            :disabled="competitorPage <= 1"
            @click="competitorPage -= 1"
          >
            上一页
          </button>
          <span>
            第 {{ competitorPage }} / {{ competitorPageCount }} 页 · 本页
            {{ pagedCompetitors.length }} 条 · 共 {{ filteredCompetitors.length }} 条
          </span>
          <button
            class="secondary-button"
            type="button"
            :disabled="competitorPage >= competitorPageCount"
            @click="competitorPage += 1"
          >
            下一页
          </button>
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
                <span>
                  {{ selected.价格信号 }}
                  <template v-if="selected.价格变化 !== null">
                    · {{ formatSignedCurrency(selected.价格变化) }}
                  </template>
                </span>
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
              <section class="panel competitor-offer-roster" aria-label="全部卖家报价与库存">
                <div class="competitor-offer-roster-heading">
                  <div>
                    <p class="section-kicker">SELLER OFFER INVENTORY</p>
                    <h2>全部卖家报价与库存</h2>
                    <span>同一 PLID 下按 Offer ID / SKU 区分报价，每个卖家的价格与库存独立统计。</span>
                  </div>
                  <span>{{ selected.跟卖报价.length }} 个报价</span>
                </div>
                <div v-if="selected.跟卖报价.length" class="competitor-offer-list">
                  <article
                    v-for="offer in selected.跟卖报价"
                    :key="offer.报价键"
                    class="competitor-offer-row"
                  >
                    <div class="competitor-offer-identity">
                      <div>
                        <strong>{{ offer.卖家 || "未知卖家" }}</strong>
                        <span
                          class="competitor-offer-kind"
                          :class="{ primary: offer.是否主报价 }"
                        >{{ offer.是否主报价 ? "当前主报价" : "跟卖报价" }}</span>
                      </div>
                      <small>
                        Offer ID {{ offer.offer_id || "未返回" }}
                        · SKU {{ offer.SKU || "未返回" }}
                      </small>
                      <small>
                        {{ offer.变体 || "默认款" }}
                        <template v-if="offer.条件"> · {{ offer.条件 }}</template>
                      </small>
                    </div>
                    <div class="competitor-offer-metric">
                      <span>该卖家价格</span>
                      <strong>{{ formatCurrency(offer.价格) }}</strong>
                      <small
                        class="price-signal"
                        :class="priceSignalClass(offer.价格信号)"
                      >
                        {{ offer.价格信号 }}
                        <template v-if="offer.价格变化 !== null">
                          · {{ formatSignedCurrency(offer.价格变化) }}
                        </template>
                      </small>
                    </div>
                    <div class="competitor-offer-metric competitor-offer-stock-metric">
                      <span>该卖家库存</span>
                      <strong>{{ offerStockDisplay(offer) }}</strong>
                      <small>{{ offerStockEvidenceLabel(offer) }}</small>
                      <small
                        class="offer-stock-signal"
                        :class="offerStockSignalClass(offer.库存信号)"
                      >
                        {{ offer.库存信号 }}
                        <template v-if="offer.库存数量变化 !== null">
                          · {{ formatSignedQuantity(offer.库存数量变化) }}
                        </template>
                      </small>
                    </div>
                    <div class="competitor-offer-metric">
                      <span>库存说明</span>
                      <strong>{{ offer.是否变体主报价 ? "变体主报价" : "公开跟卖" }}</strong>
                      <small>{{ offer.库存说明 || offer.库存原始状态 || "平台未返回更多说明" }}</small>
                    </div>
                  </article>
                </div>
                <div v-else class="competitor-offer-empty">
                  <strong>当前快照未返回可区分的卖家报价</strong>
                  <span>原链接和商品主报价仍然保留；系统不会猜测原始卖家或伪造 Offer ID、库存数量。</span>
                </div>
              </section>

              <section class="panel competitor-target-action-card">
                <div class="competitor-target-action-heading">
                  <div>
                    <p class="section-kicker">MONITORING LINK</p>
                    <h3>监控队列操作</h3>
                  </div>
                  <span>
                    {{
                      selectedTarget
                        ? "当前链接已在后续采集队列"
                        : "当前仅保留历史快照，链接不在后续采集队列"
                    }}
                  </span>
                </div>
                <template v-if="selectedTarget">
                  <template v-if="editingTargetPlid === selectedTarget.plid">
                    <input
                      v-model="editingTargetUrl"
                      class="target-edit-input"
                      type="url"
                      :aria-label="`修改 PLID${selectedTarget.plid} 链接`"
                      :disabled="targetManagerBusy === selectedTarget.plid"
                    />
                    <div class="competitor-target-action-buttons">
                      <button
                        class="primary-button"
                        type="button"
                        :disabled="targetManagerBusy === selectedTarget.plid"
                        @click="saveTargetEdit(selectedTarget.plid)"
                      >
                        {{ targetManagerBusy === selectedTarget.plid ? "保存中…" : "保存修改" }}
                      </button>
                      <button
                        class="quiet-button"
                        type="button"
                        :disabled="targetManagerBusy === selectedTarget.plid"
                        @click="cancelEditTarget"
                      >
                        取消修改
                      </button>
                    </div>
                  </template>
                  <template v-else>
                    <a
                      class="competitor-target-current-url"
                      :href="selectedTarget.url"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {{ selectedTarget.url }}
                    </a>
                    <div class="competitor-target-action-buttons">
                      <button
                        class="secondary-button priority"
                        type="button"
                        :disabled="
                          Boolean(targetManagerBusy)
                          || !props.canOperate
                          || !sharedBatchStatus.active
                          || sharedBatchStatus.current_plid === selectedTarget.plid
                          || prioritizedTargetStates.has(selectedTarget.plid)
                        "
                        @click="prioritizeTarget(selectedTarget)"
                      >
                        {{
                          targetManagerBusy === `priority:${selectedTarget.plid}`
                            ? "插队中…"
                            : targetPriorityLabel(selectedTarget.plid)
                        }}
                      </button>
                      <button
                        class="secondary-button"
                        type="button"
                        :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                        @click="beginEditTarget(selectedTarget)"
                      >
                        修改链接
                      </button>
                      <button
                        class="secondary-button danger"
                        type="button"
                        :disabled="Boolean(targetManagerBusy) || !props.canOperate"
                        @click="removeTarget(selectedTarget)"
                      >
                        删除链接
                      </button>
                    </div>
                  </template>
                </template>
                <div v-else class="competitor-target-action-buttons">
                  <button
                    class="primary-button"
                    type="button"
                    :disabled="targetManagerBusy === 'add' || !props.canOperate"
                    @click="addSelectedTarget"
                  >
                    {{ targetManagerBusy === "add" ? "加入中…" : "重新加入监控队列" }}
                  </button>
                </div>
                <p
                  v-if="targetManagerError"
                  class="target-manager-message error"
                  role="alert"
                >
                  {{ targetManagerError }}
                </p>
                <p
                  v-if="targetManagerNotice"
                  class="target-manager-message success"
                  role="status"
                >
                  {{ targetManagerNotice }}
                </p>
              </section>

              <section class="detail-grid modal-detail-grid">
                <article class="panel decision-card">
                  <p class="section-kicker">OPERATING SIGNAL</p>
                  <h2>{{ selected.趋势判断 }} · {{ selected.价格信号 }}</h2>
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
                    <span>
                      <small>区间价格变化</small>
                      <strong>
                        {{ formatCurrency(selected.区间起始价格) }} →
                        {{ formatCurrency(selected.价格) }}
                        <template v-if="selected.价格变化 !== null">
                          （{{ formatSignedCurrency(selected.价格变化) }}）
                        </template>
                      </strong>
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
                  <span>{{ latestVariants.length }} 个变体 · 评论共用商品数据</span>
                </div>
                <div v-if="!latestVariants.length" class="empty-state slim">
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
                      <tr
                        v-for="variant in latestVariants"
                        :key="`${variant.快照ID}:${variant.变体键}`"
                      >
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
                          <small
                            v-if="variant.每位客户限购 !== null"
                            class="purchase-limit-note"
                          >
                            每位客户限购 {{ variant.每位客户限购 }} 件
                          </small>
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
                  达到每位客户限购数时只记录“至少”该数量，不把促销限购误判成精确库存。
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
                <div v-if="detail.history.length" class="timeline">
                  <article
                    v-for="item in detail.history"
                    :key="item.快照ID"
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
                    <div
                      v-if="snapshotVariants(item.快照ID).length"
                      class="snapshot-variant-list"
                    >
                      <div
                        v-for="variant in snapshotVariants(item.快照ID)"
                        :key="`${item.快照ID}:${variant.变体键}`"
                        class="snapshot-variant-row"
                      >
                        <div class="competitor-product-image snapshot-variant-image">
                          <img
                            v-if="canShowCompetitorImage(variant.图片)"
                            :src="competitorImageUrl(variant.图片)"
                            :alt="`${variant.变体} 变体图片`"
                            width="96"
                            height="96"
                            loading="lazy"
                            decoding="async"
                            @error="markCompetitorImageFailed(variant.图片)"
                          />
                          <span v-else>暂无图片</span>
                        </div>
                        <div>
                          <strong>{{ variant.变体 }}</strong>
                          <span>
                            库存 {{ variant.库存 }}
                            <template v-if="variant.每位客户限购 !== null">
                              · 每位客户限购 {{ variant.每位客户限购 }} 件
                            </template>
                          </span>
                        </div>
                      </div>
                    </div>
                    <small v-else class="snapshot-variant-empty">
                      此快照尚无变体明细
                    </small>
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
