<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  AUTH_SESSION_ENDING_EVENT,
  ApiRequestError,
  addCompetitorPersonalWatchlistItem,
  collectCompetitor,
  createCompetitorTarget,
  createPersonalWatchlistLibrary,
  deleteCompetitorPersonalWatchlistItem,
  deletePersonalWatchlistLibraryItem,
  deletePersonalWatchlistLibrary,
  deleteCompetitorTarget,
  fetchCompetitorBatchStatus,
  fetchCompetitorDetail,
  fetchCompetitorLinkHealth,
  fetchCompetitorPersonalWatchlist,
  fetchPersonalWatchlistShareUsers,
  fetchCompetitorTargetAudits,
  fetchCompetitorTargets,
  fetchCompetitorStoreTargets,
  fetchCompetitors,
  logCompetitorBatchEvent,
  prioritizeCompetitorTarget,
  renamePersonalWatchlistLibrary,
  stopCompetitorBatch,
  takeoverCompetitorBatch,
  updateCompetitorTarget,
  updateCompetitorBatchOptions,
  updatePersonalWatchlistItemLibraries,
  updatePersonalWatchlistLibraryShares,
  updatePersonalWatchlistSettings,
  type CompetitorBatchStatus,
} from "../api";
import {
  buildCompetitorOfferTrend,
  comparableOfferNetOutflow,
  comparisonOffers,
  followerOffers,
  groupCompetitorOffersBySeller,
  sortCompetitorOffers,
  type CompetitorOfferSort,
  type CompetitorOfferTrendPoint,
} from "../competitorOfferHistory";
import {
  COMPETITOR_OPERATING_SIGNAL_OPTIONS,
  competitorOperatingSignals,
  matchesCompetitorOperatingSignal,
  offerOperatingSignals,
  offerPriceOperatingSignal,
  offerStockOperatingSignal,
  type CompetitorOperatingSignal,
} from "../competitorOperatingSignals";
import {
  sortCompetitorItems,
  type CompetitorListSortDirection,
} from "../competitorListSort";
import {
  competitorSearchTerm,
  matchesCompetitorSearch,
  matchesCompetitorSearchValues,
} from "../competitorSearch";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import {
  buildPersonalWatchlistWorkspaceCards,
  personalWatchlistPageForPlid,
  recountPersonalWatchlistLibraries,
  type PersonalWatchlistWorkspaceCard,
} from "../personalWatchlistWorkspace";
import {
  MAX_AUTOMATIC_RETRY_ATTEMPTS,
  mergeUniqueTargetUrls,
  scheduleRetryAfterGap,
} from "../retryQueue";
import {
  hasPersistentCollectionClientPeer,
  type CollectionClientMessage,
} from "../collectionClientClaim";
import {
  collectionCheckpointIsRunning,
  isCollectionSessionBoundaryStatus,
  shouldPreserveActiveCollectionRequest,
} from "../collectionRunLifecycle";
import { canUpdateVisibleBrowserForBatch } from "../collectionBatchOptions";
import type {
  CollectResult,
  CompetitorDateRange,
  CompetitorDetail,
  CompetitorItem,
  CompetitorLinkHealthItem,
  CompetitorOfferItem,
  CompetitorPersonalWatchlistItem,
  CompetitorPersonalWatchlistPayload,
  CompetitorTargetAuditItem,
  CompetitorTargetItem,
  CompetitorStoreTargetItem,
  CompetitorVariantItem,
  OwnFollowerHistoryItem,
  OwnStoreScope,
  PersonalWatchlistLibrary,
  PersonalWatchlistLibrarySharePermission,
  PersonalWatchlistSharedItem,
  PersonalWatchlistShareUser,
} from "../types";
import { formatChinaDateTime } from "../time";

defineOptions({ name: "CompetitorsPage" });
const props = defineProps<{
  canOperate?: boolean;
  canControlCollection?: boolean;
  isAdmin?: boolean;
  currentUsername?: string;
  currentStoreName?: string;
  accessibleConnectedStoreCount?: number;
  ownStoreScope?: OwnStoreScope;
  onPermissionDenied?: () => void;
}>();

interface CollectionQueueItem {
  index: number;
  url: string;
  priority?: boolean;
  retryKind?: "stock" | "automatic";
  retryAttempt?: number;
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

interface OfferTrendPanelPoint {
  index: number;
  x: number;
  y: number;
  value: number;
}

interface OfferTrendPanel {
  key: "price" | "stock" | "reviews";
  label: string;
  note: string;
  color: string;
  top: number;
  bottom: number;
  segments: string[];
  points: OfferTrendPanelPoint[];
  ticks: Array<{ y: number; label: string }>;
}

interface CollectionCheckpoint {
  version: 9;
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
  clientId?: string;
}

type CollectionRunMode =
  | "start"
  | "resume"
  | "auto_resume"
  | "scheduled_resume";
type TargetActionSource = "default" | "manual_retry";
type PersonalWatchlistLibraryFilter = "all" | "unclassified" | number;

const collectionCheckpointKey = "takealot-competitor-collection-v1";
const collectionClientKey = "takealot-competitor-client-v1";
const collectionClientChannelName = "takealot-competitor-client-claims-v1";
const collectionCheckpointVersion = 9;
const automaticResumeDelayMs = 10 * 60 * 1_000;
let collectionClientId = restoreCollectionClientId();
const collectionClientInstanceId = collectionId("client");
let collectionClientChannel: BroadcastChannel | null = null;
const rawUrls = ref("");
const targets = ref<CompetitorTargetItem[]>([]);
const personalWatchlistItems = ref<CompetitorPersonalWatchlistItem[]>([]);
const personalWatchlistSharedItems = ref<PersonalWatchlistSharedItem[]>([]);
const personalWatchlistPlids = ref<Set<string>>(new Set());
const personalWatchlistBusyPlid = ref("");
const personalWatchlistError = ref("");
const personalWatchlistNotice = ref("");
const personalWatchlistPage = ref(1);
const personalWatchlistPageSize = 6;
const personalWatchlistHighlightPlid = ref("");
const personalWatchlistLibraries = ref<PersonalWatchlistLibrary[]>([]);
const personalWatchlistDefaultConfigured = ref(false);
const personalWatchlistDefaultLibraryId = ref<number | null>(null);
const personalWatchlistLibraryFilter = ref<PersonalWatchlistLibraryFilter>("all");
const personalWatchlistLibraryModalOpen = ref(false);
const personalWatchlistLibraryAssignmentPlid = ref("");
const personalWatchlistLibrarySelection = ref<number[]>([]);
const personalWatchlistDefaultSelection = ref<number | null>(null);
const personalWatchlistNewLibraryName = ref("");
const personalWatchlistEditingLibraryId = ref<number | null>(null);
const personalWatchlistEditingLibraryName = ref("");
const personalWatchlistLibraryBusy = ref(false);
const personalWatchlistLibraryError = ref("");
const personalWatchlistLibraryNotice = ref("");
const personalWatchlistShareUsers = ref<PersonalWatchlistShareUser[]>([]);
const personalWatchlistShareUsersLoaded = ref(false);
const personalWatchlistShareUsersLoading = ref(false);
const personalWatchlistShareUserQuery = ref("");
const personalWatchlistSharingLibraryId = ref<number | null>(null);
const personalWatchlistShareDraft = ref<Array<{
  user_id: number;
  permission: PersonalWatchlistLibrarySharePermission;
}>>([]);
const pendingTargetAfterLibrarySetup = ref("");
const pendingPersonalWatchlistPlidAfterLibrarySetup = ref("");
const storeTargets = ref<CompetitorStoreTargetItem[]>([]);
const allStoreTargets = ref<CompetitorStoreTargetItem[]>([]);
const ownStoreScope = computed<OwnStoreScope>(
  () => props.ownStoreScope ?? "current",
);
const storeTargetMembershipCount = ref(0);
const allStoreTargetCount = ref(0);
const allStoreTargetMembershipCount = ref(0);
const allStoreTrackingStoreCount = ref(0);
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
  personalWatchlistCreated: boolean;
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
const storeCompetitors = ref<CompetitorItem[]>([]);
const selectedPlid = ref("");
const selectedOfferKey = ref("");
const offerSort = ref<CompetitorOfferSort>("net_outflow_desc");
const hoveredOfferTrendIndex = ref<number | null>(null);
const detail = ref<CompetitorDetail>({ history: [], reviews: [], variants: [] });
const detailModalOpen = ref(false);
const detailLoading = ref(false);
const detailError = ref("");
const competitorDetailCache = new Map<string, CompetitorDetail>();
const competitorDetailCacheLimit = 24;
const loading = ref(true);
const collecting = ref(false);
const collectionPreparing = ref(false);
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
const collectionDetachRequested = ref(false);
const takeoverBusy = ref(false);
const adoptableCheckpoint = ref<CollectionCheckpoint | null>(null);
const sharedBatchStatus = ref<CompetitorBatchStatus>({
  active: false,
  batch_id: null,
  owner_username: null,
  owner_display_name: null,
  source: "manual",
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
  current_retry_kind: null,
  current_retry_attempt: null,
  with_stock_probe: true,
  visible_browser: false,
  takeover_pending: false,
  reason: "",
  started_at: null,
  updated_at: null,
  queued_targets: [],
  priority_targets: [],
  prioritized_targets: [],
  results: [],
  errors: [],
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
const followerPresenceFilter = ref<"全部" | "有被跟卖" | "未发现跟卖">("全部");
const personalWatchlistFilter = ref<"全部" | "我的监控池">("全部");
const competitorSignalFilter = ref<CompetitorOperatingSignal>("全部");
const competitorSourceView = ref<"competitor" | "own_store">("competitor");
const competitorPage = ref(1);
const storeCompetitorPage = ref(1);
const competitorPageSize = ref(20);
const competitorPageSizeOptions = [20, 50, 100] as const;
const competitorListSortDirection = ref<CompetitorListSortDirection>("desc");
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
const ownFollowerHistoryStartDate = ref("");
const ownFollowerHistoryEndDate = ref("");
const ownFollowerHistoryItems = ref<OwnFollowerHistoryItem[]>([]);
const ownFollowerHistoryLoading = ref(false);
const ownFollowerHistoryLoaded = ref(false);
const ownFollowerHistoryError = ref("");
const ownFollowerHistoryOpen = ref(false);
const failedCompetitorImages = ref<Set<string>>(new Set());

const allCompetitorItems = computed(() => [
  ...storeCompetitors.value,
  ...competitors.value,
]);
const selected = computed(
  () => allCompetitorItems.value.find((item) => item.plid === selectedPlid.value) ?? null,
);
const selectedComparisonOffers = computed(() =>
  selected.value ? comparisonOffers(selected.value) : [],
);
const selectedOffer = computed(() => {
  const offers = selectedComparisonOffers.value;
  return offers.find((offer) => offer.报价键 === selectedOfferKey.value)
    ?? offers.find((offer) => offer.是否主报价)
    ?? offers[0]
    ?? null;
});
const selectedOfferLink = computed(
  () => selectedOffer.value?.链接 || selected.value?.链接 || "#",
);
const selectedHeroImage = computed(
  () => selectedOffer.value?.图片 || selected.value?.图片 || null,
);
const selectedSellerGroups = computed(() =>
  groupCompetitorOffersBySeller(selectedComparisonOffers.value, offerSort.value),
);
const selectedSellerGroup = computed(() =>
  selectedSellerGroups.value.find((group) =>
    group.offers.some((offer) => offer.报价键 === selectedOffer.value?.报价键),
  ) ?? selectedSellerGroups.value[0] ?? null,
);
const selectedSellerGroupOffers = computed(() =>
  selectedSellerGroup.value?.offers ?? [],
);
const selectedOfferPosition = computed(() =>
  selectedSellerGroups.value.findIndex((group) => group.key === selectedSellerGroup.value?.key),
);
const selectedOfferTrend = computed(() =>
  buildCompetitorOfferTrend(detail.value.history, selectedOffer.value),
);
const offerTrendChartWidth = 960;
const offerTrendChartHeight = 380;
const offerTrendPlotLeft = 86;
const offerTrendPlotRight = 936;
const offerTrendPlotWidth = offerTrendPlotRight - offerTrendPlotLeft;
const offerTrendPanels = computed<OfferTrendPanel[]>(() => {
  const definitions: Array<{
    key: OfferTrendPanel["key"];
    label: string;
    note: string;
    color: string;
    value: (point: CompetitorOfferTrendPoint) => number | null;
  }> = [
    {
      key: "price",
      label: "价格",
      note: "ZAR",
      color: "#b7522e",
      value: (point) => point.price,
    },
    {
      key: "stock",
      label: "库存",
      note: "仅连精确数量",
      color: "#236649",
      value: (point) => point.exactStock,
    },
    {
      key: "reviews",
      label: "评论",
      note: "PLID 商品共用",
      color: "#66519a",
      value: (point) => point.reviews,
    },
  ];
  return definitions.map((definition, panelIndex) => {
    const top = 20 + panelIndex * 112;
    const bottom = top + 76;
    const values = selectedOfferTrend.value.map(definition.value);
    const numericValues = values.filter((value): value is number => value !== null);
    const rawMinimum = numericValues.length ? Math.min(...numericValues) : 0;
    const rawMaximum = numericValues.length ? Math.max(...numericValues) : 1;
    const span = rawMaximum - rawMinimum;
    const padding = span === 0
      ? Math.max(1, Math.abs(rawMaximum) * 0.08)
      : Math.max(1, span * 0.12);
    const minimum = Math.max(0, rawMinimum - padding);
    const maximum = Math.max(minimum + 1, rawMaximum + padding);
    const yForValue = (value: number) =>
      bottom - ((value - minimum) / (maximum - minimum)) * (bottom - top);
    const points = values.flatMap((value, index) =>
      value === null
        ? []
        : [{
            index,
            x: offerTrendX(index, selectedOfferTrend.value),
            y: yForValue(value),
            value,
          }],
    );
    const segments: string[] = [];
    let currentSegment = "";
    for (let index = 0; index < values.length; index += 1) {
      const value = values[index];
      if (value === null) {
        if (currentSegment) segments.push(currentSegment);
        currentSegment = "";
        continue;
      }
      const x = offerTrendX(index, selectedOfferTrend.value);
      const y = yForValue(value);
      currentSegment += `${currentSegment ? " L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    }
    if (currentSegment) segments.push(currentSegment);
    const middle = (minimum + maximum) / 2;
    return {
      key: definition.key,
      label: definition.label,
      note: definition.note,
      color: definition.color,
      top,
      bottom,
      segments,
      points,
      ticks: [maximum, middle, minimum].map((value) => ({
        y: yForValue(value),
        label: formatOfferTrendAxisValue(definition.key, value),
      })),
    };
  });
});
const activeOfferTrendIndex = computed(() => {
  if (!selectedOfferTrend.value.length) return null;
  if (hoveredOfferTrendIndex.value === null) return selectedOfferTrend.value.length - 1;
  return Math.min(hoveredOfferTrendIndex.value, selectedOfferTrend.value.length - 1);
});
const activeOfferTrendPoint = computed(() => {
  const index = activeOfferTrendIndex.value;
  return index === null ? null : selectedOfferTrend.value[index] ?? null;
});
const activeOfferTrendX = computed(() => {
  const index = activeOfferTrendIndex.value;
  return index === null ? null : offerTrendX(index, selectedOfferTrend.value);
});
const offerTrendXAxisTicks = computed(() => {
  const count = selectedOfferTrend.value.length;
  if (!count) return [];
  const indexes = count <= 3
    ? Array.from({ length: count }, (_, index) => index)
    : [0, Math.floor((count - 1) / 2), count - 1];
  return [...new Set(indexes)].map((index) => ({
    index,
    x: offerTrendX(index, selectedOfferTrend.value),
    label: formatChinaDateTime(selectedOfferTrend.value[index]?.snapshot.采集时间 ?? null),
    anchor: index === 0 ? "start" : index === count - 1 ? "end" : "middle",
  }));
});
const competitorsByPlid = computed(
  () => new Map(allCompetitorItems.value.map((item) => [item.plid, item])),
);
const ownedPersonalWatchlistLibraries = computed(() =>
  personalWatchlistLibraries.value.filter((library) => library.access === "owner"),
);
const sharedPersonalWatchlistLibraries = computed(() =>
  personalWatchlistLibraries.value.filter((library) => library.access !== "owner"),
);
const activePersonalWatchlistLibrary = computed(() => {
  const filter = personalWatchlistLibraryFilter.value;
  if (typeof filter !== "number") return null;
  return personalWatchlistLibraries.value.find((library) => library.id === filter) ?? null;
});
const sharingPersonalWatchlistLibrary = computed(() => {
  const libraryId = personalWatchlistSharingLibraryId.value;
  if (libraryId === null) return null;
  return ownedPersonalWatchlistLibraries.value.find(
    (library) => library.id === libraryId,
  ) ?? null;
});
const filteredPersonalWatchlistShareUsers = computed(() => {
  const query = personalWatchlistShareUserQuery.value.trim().toLocaleLowerCase();
  if (!query) return personalWatchlistShareUsers.value;
  return personalWatchlistShareUsers.value.filter((user) =>
    [user.display_name, user.username]
      .some((value) => value.toLocaleLowerCase().includes(query)),
  );
});
const personalWatchlistCards = computed(() =>
  buildPersonalWatchlistWorkspaceCards(
    personalWatchlistItems.value,
    targets.value,
    allCompetitorItems.value,
    personalWatchlistSharedItems.value,
  ),
);
const filteredPersonalWatchlistCards = computed(() => {
  const filter = personalWatchlistLibraryFilter.value;
  if (filter === "all") {
    return personalWatchlistCards.value.filter((card) => card.personalMember);
  }
  if (filter === "unclassified") {
    return personalWatchlistCards.value.filter(
      (card) => card.personalMember && !card.libraryIds.length,
    );
  }
  return personalWatchlistCards.value.filter((card) => card.libraryIds.includes(filter));
});
const unclassifiedPersonalWatchlistCount = computed(
  () => personalWatchlistCards.value.filter(
    (card) => card.personalMember && !card.libraryIds.length,
  ).length,
);
const personalWatchlistPageCount = computed(() =>
  Math.max(
    1,
    Math.ceil(filteredPersonalWatchlistCards.value.length / personalWatchlistPageSize),
  ),
);
const pagedPersonalWatchlistCards = computed(() => {
  const start = (personalWatchlistPage.value - 1) * personalWatchlistPageSize;
  return filteredPersonalWatchlistCards.value.slice(
    start,
    start + personalWatchlistPageSize,
  );
});
const selectedTarget = computed(
  () => targets.value.find((target) => target.plid === selectedPlid.value) ?? null,
);
const selectedInPersonalWatchlist = computed(
  () => personalWatchlistPlids.value.has(selectedPlid.value),
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
      matchesCompetitorSearchValues(
        [
          target.plid,
          target.title,
          target.url,
          ...targetOffers(target).flatMap((offer) => [
            offer.offer_id,
            offer.卖家ID,
            offer.卖家,
            offer.SKU,
            offer.库存状态,
            offer.库存信号,
          ]),
        ],
        query,
      ),
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
const sharedBatchBelongsToCurrentAccount = computed(
  () =>
    Boolean(props.currentUsername)
    && sharedBatchStatus.value.owner_username?.toLowerCase()
      === props.currentUsername?.toLowerCase(),
);
const canUpdateVisibleBrowser = computed(() =>
  canUpdateVisibleBrowserForBatch(
    Boolean(props.canControlCollection),
    props.currentUsername,
    sharedBatchStatus.value,
  ),
);
const sharedBatchOwnerLabel = computed(() => {
  if (sharedBatchStatus.value.source === "scheduled") return "每日 09:00 自动任务";
  if (!sharedBatchBelongsToCurrentAccount.value) return sharedBatchOwner.value;
  return sharedBatchMatchesCheckpoint.value ? "本页面" : "本账号另一页面";
});
const sharedScheduledPause = computed(
  () =>
    sharedBatchStatus.value.source === "scheduled"
    && sharedBatchStatus.value.event === "scheduled_pause",
);
const adoptablePendingCount = computed(() => {
  if (
    sharedBatchStatus.value.active
    && sharedBatchBelongsToCurrentAccount.value
  ) {
    return Math.max(
      sharedBatchStatus.value.failed,
      adoptableCheckpoint.value
        ? checkpointPendingCount(adoptableCheckpoint.value)
        : 0,
    );
  }
  return adoptableCheckpoint.value
    ? checkpointPendingCount(adoptableCheckpoint.value)
    : 0;
});
const canTakeOverCollection = computed(
  () =>
    Boolean(props.canControlCollection)
    && !collecting.value
    && !takeoverBusy.value
    && Boolean(adoptableCheckpoint.value)
    && adoptablePendingCount.value > 0
    && (
      !sharedBatchStatus.value.active
      || sharedBatchBelongsToCurrentAccount.value
    ),
);
const sharedRetryProgress = computed(() => {
  const attempt = sharedBatchStatus.value.current_retry_attempt;
  if (!attempt) return "";
  return sharedBatchStatus.value.current_retry_kind === "stock"
    ? `正在库存复探 ${attempt}/2（总探测第 ${attempt + 1}/3 次）`
    : `正在自动重试 ${attempt}/${MAX_AUTOMATIC_RETRY_ATTEMPTS}`;
});
const collectionAlertTitle = computed(() => {
  if (sharedBatchStatus.value.active && !collecting.value) {
    if (sharedBatchStatus.value.source === "scheduled") {
      return "每日 09:00 自动采集正在运行";
    }
    if (sharedBatchMatchesCheckpoint.value) return "正在恢复刷新前的采集任务";
    return sharedBatchBelongsToCurrentAccount.value
      ? "本账号另一页面正在采集"
      : "当前已有竞品采集正在运行";
  }
  if (autoResumeAt.value) return "网络异常，已安排自动续爬";
  return "本次采集已暂停";
});
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
const competitorSignalOptions = COMPETITOR_OPERATING_SIGNAL_OPTIONS;
const filteredCompetitors = computed(() => {
  return competitors.value.filter(matchesCompetitorFilters);
});
const filteredStoreCompetitors = computed(() => {
  return storeCompetitors.value.filter(matchesCompetitorFilters);
});
const sortedCompetitors = computed(() =>
  sortCompetitorItems(
    filteredCompetitors.value,
    competitorSignalFilter.value,
    competitorListSortDirection.value,
  ),
);
const sortedStoreCompetitors = computed(() =>
  sortCompetitorItems(
    filteredStoreCompetitors.value,
    competitorSignalFilter.value,
    competitorListSortDirection.value,
  ),
);
const unifiedCollectionUrls = computed(() =>
  mergeUniqueTargetUrls(
    targets.value.map((target) => target.url),
    allStoreTargets.value.map((target) => target.url),
  ),
);
const activeSourceFilteredCount = computed(() =>
  competitorSourceView.value === "competitor"
    ? filteredCompetitors.value.length
    : filteredStoreCompetitors.value.length,
);
const activeSourceTotalCount = computed(() =>
  competitorSourceView.value === "competitor"
    ? competitors.value.length
    : storeCompetitors.value.length,
);
const ownStoreScopeLabel = computed(() =>
  ownStoreScope.value === "all"
    ? `全部有权店铺（${allStoreTrackingStoreCount.value || props.accessibleConnectedStoreCount || 0} 个）`
    : props.currentStoreName || "当前店铺",
);

function matchesCompetitorFilters(item: CompetitorItem) {
  if (
    item.来源 === "competitor"
    && personalWatchlistFilter.value === "我的监控池"
    && !personalWatchlistPlids.value.has(item.plid)
  ) {
    return false;
  }
  if (!matchesCompetitorSearch(item, competitorQuery.value)) {
    return false;
  }
  if (
    competitorStockFilter.value !== "全部"
    && competitorStockState(item) !== competitorStockFilter.value
  ) {
    return false;
  }
  if (followerPresenceFilter.value !== "全部") {
    const hasFollowers = item.跟卖发现日期.length > 0 || followerOffers(item).length > 0;
    if (followerPresenceFilter.value === "有被跟卖" && !hasFollowers) return false;
    if (followerPresenceFilter.value === "未发现跟卖" && hasFollowers) return false;
  }
  return matchesCompetitorOperatingSignal(item, competitorSignalFilter.value);
}
const competitorPageCount = computed(() =>
  Math.max(1, Math.ceil(sortedCompetitors.value.length / competitorPageSize.value)),
);
const pagedCompetitors = computed(() => {
  const start = (competitorPage.value - 1) * competitorPageSize.value;
  return sortedCompetitors.value.slice(start, start + competitorPageSize.value);
});
const storeCompetitorPageCount = computed(() =>
  Math.max(
    1,
    Math.ceil(sortedStoreCompetitors.value.length / competitorPageSize.value),
  ),
);
const pagedStoreCompetitors = computed(() => {
  const start = (storeCompetitorPage.value - 1) * competitorPageSize.value;
  return sortedStoreCompetitors.value.slice(start, start + competitorPageSize.value);
});
const competitorFiltersActive = computed(
  () =>
    Boolean(competitorQuery.value.trim())
    || competitorStockFilter.value !== "全部"
    || followerPresenceFilter.value !== "全部"
    || (
      competitorSourceView.value === "competitor"
      && personalWatchlistFilter.value !== "全部"
    )
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
  if (!allCompetitorItems.value.length) return "尚未采集";
  const latest = allCompetitorItems.value.reduce((candidate, item) =>
    new Date(item.采集时间).getTime() > new Date(candidate.采集时间).getTime()
      ? item
      : candidate,
  );
  return formatChinaDateTime(latest.采集时间);
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
const sharedBatchProgressIsAuthoritative = computed(
  () =>
    sharedBatchStatus.value.active
    || sharedBatchStatus.value.source === "scheduled",
);
const displayedBatchCompleted = computed(() =>
  sharedBatchProgressIsAuthoritative.value
    ? sharedBatchStatus.value.completed
    : completed.value,
);
const displayedBatchTotal = computed(() =>
  sharedBatchProgressIsAuthoritative.value
    ? sharedBatchStatus.value.total
    : total.value,
);
const displayedBatchSucceeded = computed(() =>
  sharedBatchProgressIsAuthoritative.value
    ? sharedBatchStatus.value.succeeded
    : collectionResults.value.length,
);
const displayedBatchFailed = computed(() =>
  sharedBatchProgressIsAuthoritative.value
    ? sharedBatchStatus.value.failed
    : failedIndexes.value.length,
);
const sharedBatchDetailsAreAuthoritative = computed(
  () => sharedBatchProgressIsAuthoritative.value,
);
const displayedCollectionResults = computed(() =>
  sharedBatchDetailsAreAuthoritative.value
    ? sharedBatchStatus.value.results
    : collectionResults.value,
);
const displayedCollectionErrors = computed(() =>
  sharedBatchDetailsAreAuthoritative.value
    ? sharedBatchStatus.value.errors
    : collectionErrors.value,
);
const hasDisplayedBatchProgress = computed(
  () =>
    sharedBatchStatus.value.active
    || (
      sharedBatchStatus.value.source === "scheduled"
      && Boolean(sharedBatchStatus.value.batch_id)
    )
    || Boolean(
      displayedCollectionResults.value.length
      || displayedCollectionErrors.value.length
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
  sharedBatchProgressIsAuthoritative.value
    ? sharedBatchStatus.value.pending
    : pendingResumeCount.value,
);
const showCollectionDetails = computed(
  () =>
    Boolean(
      displayedCollectionResults.value.length
      || displayedCollectionErrors.value.length,
    ),
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
  let checkpoint: CollectionCheckpoint | null = null;
  if (props.isAdmin) {
    window.addEventListener("beforeunload", closeCollectionClientChannel);
    window.addEventListener(
      AUTH_SESSION_ENDING_EVENT,
      detachCollectionForSessionChange,
    );
    checkpoint = readCollectionCheckpoint();
    restoreCheckpointCollectionClientId(checkpoint);
    await ensureUniqueCollectionClientId();
  }
  const initialRequests: Array<Promise<void>> = [loadOverview(), loadTargets()];
  if (props.isAdmin) initialRequests.push(loadSharedBatchStatus());
  await Promise.all(initialRequests);
  if (props.isAdmin) {
    if (checkpoint) await restoreCollectionCheckpoint(checkpoint);
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
  }
});

onBeforeUnmount(() => {
  detachCollectionForSessionChange();
  window.removeEventListener("keydown", handleWindowKeydown);
  window.removeEventListener("beforeunload", closeCollectionClientChannel);
  window.removeEventListener(
    AUTH_SESSION_ENDING_EVENT,
    detachCollectionForSessionChange,
  );
  closeCollectionClientChannel();
  if (sharedBatchTimer !== null) window.clearInterval(sharedBatchTimer);
  if (batchHeartbeatTimer !== null) window.clearInterval(batchHeartbeatTimer);
  if (collectionClockTimer !== null) window.clearInterval(collectionClockTimer);
  if (personalWatchlistHighlightTimer !== null) {
    window.clearTimeout(personalWatchlistHighlightTimer);
  }
  document.body.style.overflow = "";
});

let sharedBatchTimer: number | null = null;
let batchHeartbeatTimer: number | null = null;
let collectionClockTimer: number | null = null;
let personalWatchlistHighlightTimer: number | null = null;

watch([targetQuery, targetPageSize], () => {
  targetPage.value = 1;
});

watch(targetPageCount, (pageCount) => {
  if (targetPage.value > pageCount) targetPage.value = pageCount;
});

watch(personalWatchlistPageCount, (pageCount) => {
  if (personalWatchlistPage.value > pageCount) personalWatchlistPage.value = pageCount;
});

watch(personalWatchlistLibraryFilter, () => {
  personalWatchlistPage.value = 1;
});

watch(
  [
    competitorQuery,
    competitorStockFilter,
    followerPresenceFilter,
    personalWatchlistFilter,
    competitorSignalFilter,
    competitorListSortDirection,
    competitorPageSize,
  ],
  () => {
    competitorPage.value = 1;
    storeCompetitorPage.value = 1;
  },
);

watch(competitorPageCount, (pageCount) => {
  if (competitorPage.value > pageCount) competitorPage.value = pageCount;
});
watch(storeCompetitorPageCount, (pageCount) => {
  if (storeCompetitorPage.value > pageCount) storeCompetitorPage.value = pageCount;
});

function competitorDetailCacheKey(
  plid: string,
  start: string,
  end: string,
  scope: OwnStoreScope,
): string {
  return [
    plid,
    start,
    end,
    scope,
    props.currentStoreName ?? "",
    selected.value?.采集时间 ?? "",
  ].join("\u001f");
}

function cachedCompetitorDetail(key: string): CompetitorDetail | undefined {
  const cached = competitorDetailCache.get(key);
  if (!cached) return undefined;
  competitorDetailCache.delete(key);
  competitorDetailCache.set(key, cached);
  return cached;
}

function cacheCompetitorDetail(key: string, value: CompetitorDetail): void {
  competitorDetailCache.delete(key);
  competitorDetailCache.set(key, value);
  if (competitorDetailCache.size <= competitorDetailCacheLimit) return;
  const oldestKey = competitorDetailCache.keys().next().value;
  if (oldestKey !== undefined) competitorDetailCache.delete(oldestKey);
}

let detailRequestId = 0;
watch(
  [
    detailModalOpen,
    selectedPlid,
    appliedStartDate,
    appliedEndDate,
    ownStoreScope,
    () => selected.value?.采集时间 ?? "",
    () => props.currentStoreName ?? "",
  ],
  async ([modalOpen, plid, start, end, scope]) => {
    const requestId = ++detailRequestId;
    if (!plid) {
      detail.value = { history: [], reviews: [], variants: [] };
      detailLoading.value = false;
      detailError.value = "";
      return;
    }
    if (!modalOpen) {
      detailLoading.value = false;
      detailError.value = "";
      return;
    }
    const cacheKey = competitorDetailCacheKey(plid, start, end, scope);
    const cached = cachedCompetitorDetail(cacheKey);
    if (cached) {
      detail.value = cached;
      detailLoading.value = false;
      detailError.value = "";
      return;
    }
    detailLoading.value = true;
    detailError.value = "";
    try {
      const result = await fetchCompetitorDetail(plid, start, end, scope);
      if (requestId === detailRequestId) {
        detail.value = result;
        cacheCompetitorDetail(cacheKey, result);
      }
    } catch (error) {
      if (requestId === detailRequestId) {
        detailError.value = error instanceof Error ? error.message : "读取商品详情失败";
      }
    } finally {
      if (requestId === detailRequestId) detailLoading.value = false;
    }
  },
);

watch(ownStoreScope, async () => {
  storeCompetitorPage.value = 1;
  selectedPlid.value = "";
  await Promise.all([loadOverview(), loadTargets()]);
});

watch([selectedOfferKey, () => selectedOfferTrend.value.length], () => {
  hoveredOfferTrendIndex.value = null;
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

function ownStoreNames(item: CompetitorItem): string {
  return [...new Set(item.自有报价.map((offer) => offer.店铺).filter(Boolean))].join("、")
    || ownStoreScopeLabel.value;
}

function ownStoreVariantCount(item: CompetitorItem): number {
  const identities = comparisonOffers(item)
    .filter((offer) => offer.报价来源 === "seller_api")
    .map((offer) =>
      String(
        offer.TSIN
        || offer.图片
        || offer.变体键
        || offer.SKU
        || offer.offer_id
        || offer.报价键,
      ).trim(),
    )
    .filter(Boolean);
  return new Set(identities).size;
}

function clearCompetitorFilters(): void {
  competitorQuery.value = "";
  competitorStockFilter.value = "全部";
  followerPresenceFilter.value = "全部";
  personalWatchlistFilter.value = "全部";
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

async function queryOwnFollowerHistory(): Promise<void> {
  ownFollowerHistoryError.value = "";
  if (!ownFollowerHistoryStartDate.value || !ownFollowerHistoryEndDate.value) {
    ownFollowerHistoryError.value = "请选择完整的开始日期和结束日期";
    return;
  }
  if (ownFollowerHistoryStartDate.value > ownFollowerHistoryEndDate.value) {
    ownFollowerHistoryError.value = "开始日期不能晚于结束日期";
    return;
  }
  ownFollowerHistoryLoading.value = true;
  try {
    const overview = await fetchCompetitors(
      ownFollowerHistoryStartDate.value,
      ownFollowerHistoryEndDate.value,
      "all",
    );
    ownFollowerHistoryItems.value = overview.own_follower_events;
    ownFollowerHistoryLoaded.value = true;
  } catch (error) {
    ownFollowerHistoryError.value =
      error instanceof Error ? error.message : "读取自有链接跟卖历史失败";
  } finally {
    ownFollowerHistoryLoading.value = false;
  }
}

function openProductModal(item: CompetitorItem) {
  selectedPlid.value = item.plid;
  const offers = comparisonOffers(item);
  selectedOfferKey.value = sortCompetitorOffers(offers, offerSort.value)[0]?.报价键
    ?? offers.find((offer) => offer.是否主报价)?.报价键
    ?? offers[0]?.报价键
    ?? "";
  hoveredOfferTrendIndex.value = null;
  if (editingTargetPlid.value && editingTargetPlid.value !== item.plid) {
    cancelEditTarget();
  }
  clearTargetManagerFeedback();
  clearPersonalWatchlistFeedback();
  detailModalOpen.value = true;
}

function closeProductModal() {
  detailModalOpen.value = false;
  selectedOfferKey.value = "";
  hoveredOfferTrendIndex.value = null;
  if (editingTargetPlid.value === selectedPlid.value) cancelEditTarget();
  clearTargetManagerFeedback();
  clearPersonalWatchlistFeedback();
}

function selectCompetitorOffer(offer: CompetitorOfferItem) {
  selectedOfferKey.value = offer.报价键;
  hoveredOfferTrendIndex.value = null;
}

function selectAdjacentCompetitorOffer(direction: -1 | 1) {
  if (!selectedSellerGroups.value.length) return;
  const currentIndex = selectedOfferPosition.value < 0 ? 0 : selectedOfferPosition.value;
  const nextIndex = Math.min(
    selectedSellerGroups.value.length - 1,
    Math.max(0, currentIndex + direction),
  );
  selectCompetitorOffer(selectedSellerGroups.value[nextIndex]!.offers[0]!);
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
  if (personalWatchlistLibraryModalOpen.value) {
    closePersonalWatchlistLibraryModal();
    return;
  }
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
      fetchCompetitors(
        appliedStartDate.value,
        appliedEndDate.value,
        ownStoreScope.value,
      ),
      props.isAdmin ? fetchCompetitorLinkHealth() : Promise.resolve([]),
    ]);
    competitors.value = overview.items;
    storeCompetitors.value = overview.store_items;
    competitorDateRange.value = overview.date_range;
    if (!appliedStartDate.value && overview.date_range.selected_start) {
      appliedStartDate.value = overview.date_range.selected_start;
    }
    if (!appliedEndDate.value && overview.date_range.selected_end) {
      appliedEndDate.value = overview.date_range.selected_end;
    }
    if (!rangeStartDate.value) rangeStartDate.value = appliedStartDate.value;
    if (!rangeEndDate.value) rangeEndDate.value = appliedEndDate.value;
    if (!ownFollowerHistoryStartDate.value) {
      ownFollowerHistoryStartDate.value =
        overview.date_range.selected_start ?? overview.date_range.available_start ?? "";
    }
    if (!ownFollowerHistoryEndDate.value) {
      ownFollowerHistoryEndDate.value =
        overview.date_range.selected_end ?? overview.date_range.available_end ?? "";
    }
    linkHealth.value = healthItems;
    if (!allCompetitorItems.value.some((item) => item.plid === selectedPlid.value)) {
      selectedPlid.value = allCompetitorItems.value[0]?.plid ?? "";
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
    if (
      status.active
      && canUpdateVisibleBrowserForBatch(
        Boolean(props.canControlCollection),
        props.currentUsername,
        status,
      )
    ) {
      withStockProbe.value = status.with_stock_probe;
      visibleBrowser.value = status.visible_browser;
    }
    const checkpoint = readCollectionCheckpoint();
    if (
      checkpoint
      && checkpoint.version >= collectionCheckpointVersion
      && checkpoint.clientId !== collectionClientId
      && checkpointPendingCount(checkpoint) > 0
      && (
        !status.active
        || status.batch_id === checkpoint.batchId
      )
    ) {
      adoptableCheckpoint.value = checkpoint;
    }
    mergeQueuedTargetsIntoLocalBatch(status);
  } catch {
    // Keep the last shared progress during a short local-service interruption.
  }
}

async function loadTargets() {
  try {
    const [
      loadedTargets,
      storeTargetPayload,
      allStoreTargetPayload,
      personalWatchlistPayload,
    ] = await Promise.all([
      fetchCompetitorTargets(),
      fetchCompetitorStoreTargets(ownStoreScope.value),
      fetchCompetitorStoreTargets("all"),
      fetchCompetitorPersonalWatchlist(),
    ]);
    targets.value = loadedTargets;
    applyPersonalWatchlistPayload(personalWatchlistPayload);
    storeTargets.value = storeTargetPayload.items;
    allStoreTargets.value = allStoreTargetPayload.items;
    storeTargetMembershipCount.value = storeTargetPayload.selected_membership_count;
    allStoreTargetCount.value = storeTargetPayload.all_store_unique_count;
    allStoreTargetMembershipCount.value = storeTargetPayload.all_store_membership_count;
    allStoreTrackingStoreCount.value = storeTargetPayload.all_store_count;
    if (!batchUrls.value.length) {
      rawUrls.value = targets.value.map((target) => target.url).join("\n");
    }
  } catch (error) {
    targetManagerError.value =
      error instanceof Error ? error.message : "读取竞品链接清单失败";
  }
}

function applyPersonalWatchlistPayload(
  payload: CompetitorPersonalWatchlistPayload,
): void {
  personalWatchlistItems.value = payload.items;
  personalWatchlistSharedItems.value = payload.shared_items ?? [];
  personalWatchlistPlids.value = new Set(payload.items.map((item) => item.plid));
  personalWatchlistLibraries.value = payload.libraries;
  personalWatchlistDefaultConfigured.value = payload.default_library_configured;
  personalWatchlistDefaultLibraryId.value = payload.default_library_id;
  personalWatchlistDefaultSelection.value = payload.default_library_id;
  if (
    typeof personalWatchlistLibraryFilter.value === "number"
    && !payload.libraries.some(
      (library) => library.id === personalWatchlistLibraryFilter.value,
    )
  ) {
    personalWatchlistLibraryFilter.value = "all";
  }
}

async function loadPersonalWatchlist(): Promise<void> {
  applyPersonalWatchlistPayload(await fetchCompetitorPersonalWatchlist());
}

function setPersonalWatchlistLocal(
  plid: string,
  included: boolean,
  membership?: CompetitorPersonalWatchlistItem,
): void {
  const next = new Set(personalWatchlistPlids.value);
  if (included) {
    next.add(plid);
    personalWatchlistSharedItems.value = personalWatchlistSharedItems.value.filter(
      (item) => item.plid !== plid,
    );
    const existing = personalWatchlistItems.value.find((item) => item.plid === plid);
    const nextMembership = membership ?? existing ?? {
      plid,
      added_at: new Date().toISOString(),
      source: "competitor",
      library_ids: [],
    };
    personalWatchlistItems.value = existing
      ? personalWatchlistItems.value.map((item) =>
        item.plid === plid ? nextMembership : item,
      )
      : [nextMembership, ...personalWatchlistItems.value];
  } else {
    next.delete(plid);
    personalWatchlistItems.value = personalWatchlistItems.value.filter(
      (item) => item.plid !== plid,
    );
  }
  personalWatchlistPlids.value = next;
  personalWatchlistLibraries.value = recountPersonalWatchlistLibraries(
    personalWatchlistLibraries.value,
    personalWatchlistItems.value,
    personalWatchlistSharedItems.value,
  );
}

async function persistPersonalWatchlistAddition(plid: string): Promise<boolean> {
  const result = await addCompetitorPersonalWatchlistItem(plid);
  setPersonalWatchlistLocal(plid, true, result.item);
  await loadPersonalWatchlist();
  return result.created;
}

function personalWatchlistLibraryNames(
  card: PersonalWatchlistWorkspaceCard,
): string[] {
  const selectedIds = new Set(card.libraryIds);
  return personalWatchlistLibraries.value
    .filter((library) => selectedIds.has(library.id))
    .map((library) => library.access === "owner"
      ? library.name
      : `${library.name} · ${library.owner_display_name}共享`);
}

async function loadPersonalWatchlistShareUsers(force = false): Promise<void> {
  if (
    personalWatchlistShareUsersLoading.value
    || (personalWatchlistShareUsersLoaded.value && !force)
  ) return;
  personalWatchlistShareUsersLoading.value = true;
  try {
    personalWatchlistShareUsers.value = await fetchPersonalWatchlistShareUsers();
    personalWatchlistShareUsersLoaded.value = true;
  } catch (error) {
    personalWatchlistLibraryError.value = error instanceof Error
      ? error.message
      : "系统用户列表读取失败";
  } finally {
    personalWatchlistShareUsersLoading.value = false;
  }
}

function openPersonalWatchlistLibrarySettings(): void {
  personalWatchlistLibraryAssignmentPlid.value = "";
  personalWatchlistLibrarySelection.value = [];
  personalWatchlistDefaultSelection.value = personalWatchlistDefaultLibraryId.value;
  personalWatchlistLibraryError.value = "";
  personalWatchlistLibraryNotice.value = "";
  personalWatchlistLibraryModalOpen.value = true;
  document.body.style.overflow = "hidden";
  void loadPersonalWatchlistShareUsers();
}

function openPersonalWatchlistCardLibraries(
  card: PersonalWatchlistWorkspaceCard,
): void {
  personalWatchlistLibraryAssignmentPlid.value = card.plid;
  personalWatchlistLibrarySelection.value = [...card.libraryIds];
  personalWatchlistDefaultSelection.value = personalWatchlistDefaultLibraryId.value;
  personalWatchlistLibraryError.value = "";
  personalWatchlistLibraryNotice.value = "";
  personalWatchlistLibraryModalOpen.value = true;
  document.body.style.overflow = "hidden";
  void loadPersonalWatchlistShareUsers();
}

function promptForPersonalWatchlistDefault(url: string): void {
  pendingTargetAfterLibrarySetup.value = url;
  openPersonalWatchlistLibrarySettings();
  personalWatchlistLibraryNotice.value = (
    "首次新增前，请选择一个默认类型库，或明确选择“不自动加入任何类型库”。"
  );
}

function promptForPersonalWatchlistDefaultBeforeMembership(plid: string): void {
  openPersonalWatchlistLibrarySettings();
  pendingPersonalWatchlistPlidAfterLibrarySetup.value = plid;
  personalWatchlistLibraryNotice.value = (
    "首次加入个人监控池前，请选择一个默认类型库，或明确选择“不自动加入任何类型库”。"
  );
}

function closePersonalWatchlistLibraryModal(): void {
  personalWatchlistLibraryModalOpen.value = false;
  personalWatchlistLibraryAssignmentPlid.value = "";
  personalWatchlistLibrarySelection.value = [];
  personalWatchlistEditingLibraryId.value = null;
  personalWatchlistEditingLibraryName.value = "";
  personalWatchlistSharingLibraryId.value = null;
  personalWatchlistShareDraft.value = [];
  personalWatchlistShareUserQuery.value = "";
  pendingTargetAfterLibrarySetup.value = "";
  pendingPersonalWatchlistPlidAfterLibrarySetup.value = "";
  document.body.style.overflow = "";
}

async function addPersonalWatchlistLibrary(): Promise<void> {
  const name = personalWatchlistNewLibraryName.value.trim();
  if (!name || personalWatchlistLibraryBusy.value) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  try {
    const result = await createPersonalWatchlistLibrary(name);
    personalWatchlistLibraries.value = [
      ...personalWatchlistLibraries.value,
      result.library,
    ];
    personalWatchlistDefaultSelection.value = result.library.id;
    personalWatchlistNewLibraryName.value = "";
    personalWatchlistLibraryNotice.value = (
      `类型库“${result.library.name}”已创建，并已选为待保存的默认类型库。`
    );
  } catch (error) {
    personalWatchlistLibraryError.value =
      error instanceof Error ? error.message : "创建类型库失败";
  } finally {
    personalWatchlistLibraryBusy.value = false;
  }
}

function beginRenamePersonalWatchlistLibrary(library: PersonalWatchlistLibrary): void {
  personalWatchlistEditingLibraryId.value = library.id;
  personalWatchlistEditingLibraryName.value = library.name;
  personalWatchlistLibraryError.value = "";
}

async function savePersonalWatchlistLibraryRename(): Promise<void> {
  const libraryId = personalWatchlistEditingLibraryId.value;
  const name = personalWatchlistEditingLibraryName.value.trim();
  if (libraryId === null || !name || personalWatchlistLibraryBusy.value) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  try {
    const result = await renamePersonalWatchlistLibrary(libraryId, name);
    personalWatchlistLibraries.value = personalWatchlistLibraries.value.map(
      (library) => library.id === libraryId ? result.library : library,
    );
    personalWatchlistEditingLibraryId.value = null;
    personalWatchlistEditingLibraryName.value = "";
    personalWatchlistLibraryNotice.value = `类型库已重命名为“${result.library.name}”。`;
  } catch (error) {
    personalWatchlistLibraryError.value =
      error instanceof Error ? error.message : "重命名类型库失败";
  } finally {
    personalWatchlistLibraryBusy.value = false;
  }
}

function openPersonalWatchlistLibrarySharing(library: PersonalWatchlistLibrary): void {
  if (library.access !== "owner") return;
  personalWatchlistSharingLibraryId.value = library.id;
  personalWatchlistShareDraft.value = library.shares.map((share) => ({
    user_id: share.user_id,
    permission: share.permission,
  }));
  personalWatchlistShareUserQuery.value = "";
  personalWatchlistLibraryError.value = "";
  personalWatchlistLibraryNotice.value = "";
  void loadPersonalWatchlistShareUsers();
}

function personalWatchlistSharePermissionFor(
  userId: number,
): PersonalWatchlistLibrarySharePermission | null {
  return personalWatchlistShareDraft.value.find(
    (share) => share.user_id === userId,
  )?.permission ?? null;
}

function setPersonalWatchlistShareEnabled(userId: number, event: Event): void {
  const enabled = (event.target as HTMLInputElement).checked;
  const existing = personalWatchlistSharePermissionFor(userId);
  if (enabled && existing === null) {
    const nextShare: {
      user_id: number;
      permission: PersonalWatchlistLibrarySharePermission;
    } = { user_id: userId, permission: "read" };
    personalWatchlistShareDraft.value = [
      ...personalWatchlistShareDraft.value,
      nextShare,
    ].sort((left, right) => left.user_id - right.user_id);
  } else if (!enabled) {
    personalWatchlistShareDraft.value = personalWatchlistShareDraft.value.filter(
      (share) => share.user_id !== userId,
    );
  }
}

function setPersonalWatchlistSharePermission(userId: number, event: Event): void {
  const permission = (event.target as HTMLSelectElement).value;
  if (permission !== "read" && permission !== "edit") return;
  personalWatchlistShareDraft.value = personalWatchlistShareDraft.value.map(
    (share) => share.user_id === userId ? { ...share, permission } : share,
  );
}

async function savePersonalWatchlistLibraryShares(): Promise<void> {
  const library = sharingPersonalWatchlistLibrary.value;
  if (!library || personalWatchlistLibraryBusy.value) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  try {
    const result = await updatePersonalWatchlistLibraryShares(
      library.id,
      personalWatchlistShareDraft.value,
    );
    personalWatchlistLibraries.value = personalWatchlistLibraries.value.map(
      (item) => item.id === library.id ? result.library : item,
    );
    personalWatchlistShareDraft.value = result.library.shares.map((share) => ({
      user_id: share.user_id,
      permission: share.permission,
    }));
    personalWatchlistLibraryNotice.value = result.library.share_count
      ? `类型库“${result.library.name}”已分享给 ${result.library.share_count} 个用户。`
      : `类型库“${result.library.name}”已取消全部分享。`;
  } catch (error) {
    personalWatchlistLibraryError.value = error instanceof Error
      ? error.message
      : "保存类型库分享权限失败";
  } finally {
    personalWatchlistLibraryBusy.value = false;
  }
}

async function removePersonalWatchlistLibrary(
  library: PersonalWatchlistLibrary,
): Promise<void> {
  if (personalWatchlistLibraryBusy.value) return;
  if (!window.confirm(`删除类型库“${library.name}”？商品仍保留在个人监控池。`)) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  try {
    await deletePersonalWatchlistLibrary(library.id);
    personalWatchlistLibrarySelection.value = personalWatchlistLibrarySelection.value.filter(
      (libraryId) => libraryId !== library.id,
    );
    if (personalWatchlistSharingLibraryId.value === library.id) {
      personalWatchlistSharingLibraryId.value = null;
      personalWatchlistShareDraft.value = [];
    }
    await loadPersonalWatchlist();
    personalWatchlistLibraryNotice.value = (
      `类型库“${library.name}”已删除，监控池商品和采集历史未改变。`
    );
  } catch (error) {
    personalWatchlistLibraryError.value =
      error instanceof Error ? error.message : "删除类型库失败";
  } finally {
    personalWatchlistLibraryBusy.value = false;
  }
}

function togglePersonalWatchlistLibrarySelection(libraryId: number): void {
  const next = new Set(personalWatchlistLibrarySelection.value);
  if (next.has(libraryId)) next.delete(libraryId);
  else next.add(libraryId);
  personalWatchlistLibrarySelection.value = [...next].sort((a, b) => a - b);
}

async function savePersonalWatchlistCardLibraries(): Promise<void> {
  const plid = personalWatchlistLibraryAssignmentPlid.value;
  if (!plid || personalWatchlistLibraryBusy.value) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  try {
    await updatePersonalWatchlistItemLibraries(
      plid,
      personalWatchlistLibrarySelection.value,
    );
    await loadPersonalWatchlist();
    personalWatchlistLibraryNotice.value = `PLID${plid} 的类型库归类已保存。`;
  } catch (error) {
    personalWatchlistLibraryError.value =
      error instanceof Error ? error.message : "保存商品类型库失败";
  } finally {
    personalWatchlistLibraryBusy.value = false;
  }
}

function canEditPersonalWatchlistLibrary(
  library: PersonalWatchlistLibrary | null,
): boolean {
  return library?.access === "owner" || library?.access === "edit";
}

async function removeCardFromActivePersonalWatchlistLibrary(
  card: PersonalWatchlistWorkspaceCard,
): Promise<void> {
  const library = activePersonalWatchlistLibrary.value;
  if (
    !library
    || !canEditPersonalWatchlistLibrary(library)
    || personalWatchlistBusyPlid.value
  ) return;
  personalWatchlistBusyPlid.value = card.plid;
  personalWatchlistLibraryError.value = "";
  try {
    await deletePersonalWatchlistLibraryItem(library.id, card.plid);
    await loadPersonalWatchlist();
    personalWatchlistNotice.value = (
      `PLID${card.plid} 已从类型库“${library.name}”移除；`
      + "个人监控关系、全局采集和历史记录均未改变。"
    );
  } catch (error) {
    personalWatchlistError.value = error instanceof Error
      ? error.message
      : "从类型库移除商品失败";
  } finally {
    personalWatchlistBusyPlid.value = "";
  }
}

async function savePersonalWatchlistDefault(): Promise<void> {
  if (personalWatchlistLibraryBusy.value) return;
  personalWatchlistLibraryBusy.value = true;
  personalWatchlistLibraryError.value = "";
  const pendingUrl = pendingTargetAfterLibrarySetup.value;
  const pendingPlid = pendingPersonalWatchlistPlidAfterLibrarySetup.value;
  try {
    const result = await updatePersonalWatchlistSettings(
      personalWatchlistDefaultSelection.value,
    );
    personalWatchlistDefaultConfigured.value = result.default_library_configured;
    personalWatchlistDefaultLibraryId.value = result.default_library_id;
    personalWatchlistLibraryNotice.value = result.default_library_id === null
      ? "已设置为新增商品不自动加入任何类型库。"
      : "默认类型库已保存，新增商品会自动归入该库。";
    personalWatchlistLibraryModalOpen.value = false;
    personalWatchlistLibraryAssignmentPlid.value = "";
    pendingTargetAfterLibrarySetup.value = "";
    pendingPersonalWatchlistPlidAfterLibrarySetup.value = "";
    document.body.style.overflow = "";
  } catch (error) {
    personalWatchlistLibraryError.value =
      error instanceof Error ? error.message : "保存默认类型库失败";
    personalWatchlistLibraryBusy.value = false;
    return;
  }
  personalWatchlistLibraryBusy.value = false;
  if (pendingUrl) {
    newTargetUrl.value = pendingUrl;
    await addTarget();
  } else if (pendingPlid) {
    try {
      await persistPersonalWatchlistAddition(pendingPlid);
      personalWatchlistNotice.value = `PLID${pendingPlid} 已加入你的个人监控池。`;
      await focusPersonalWatchlistCard(pendingPlid);
    } catch (error) {
      personalWatchlistError.value =
        error instanceof Error ? error.message : "加入个人监控池失败";
    }
  }
}

async function focusPersonalWatchlistCard(plid: string): Promise<boolean> {
  personalWatchlistLibraryFilter.value = "all";
  const page = personalWatchlistPageForPlid(
    personalWatchlistCards.value,
    plid,
    personalWatchlistPageSize,
  );
  if (page === null) {
    targetManagerError.value = `PLID${plid} 已加入个人监控池，但暂时无法显示对应卡片`;
    return false;
  }
  personalWatchlistPage.value = page;
  await nextTick();
  const card = document.getElementById(`personal-watchlist-card-${plid}`);
  if (!card) {
    targetManagerError.value = `PLID${plid} 已加入个人监控池，但对应卡片尚未加载`;
    return false;
  }
  if (personalWatchlistHighlightTimer !== null) {
    window.clearTimeout(personalWatchlistHighlightTimer);
  }
  personalWatchlistHighlightPlid.value = "";
  await nextTick();
  personalWatchlistHighlightPlid.value = plid;
  personalWatchlistHighlightTimer = window.setTimeout(() => {
    if (personalWatchlistHighlightPlid.value === plid) {
      personalWatchlistHighlightPlid.value = "";
    }
    personalWatchlistHighlightTimer = null;
  }, 4_500);
  await nextTick();
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.focus({ preventScroll: true });
  return true;
}

function openPersonalWatchlistCard(card: PersonalWatchlistWorkspaceCard): void {
  if (card.competitor) {
    openProductModal(card.competitor);
    return;
  }
  clearTargetManagerFeedback();
  if (!card.personalMember) {
    const library = activePersonalWatchlistLibrary.value;
    targetManagerNotice.value = library
      ? `PLID${card.plid} 来自 ${library.owner_display_name} 分享的类型库“${library.name}”；商品详情仍按你的账号数据权限显示。`
      : `PLID${card.plid} 来自共享类型库；商品详情仍按你的账号数据权限显示。`;
  } else if (card.source === "own_store") {
    targetManagerNotice.value = (
      `PLID${card.plid} 是自有店铺商品，已在个人监控池并持续参加每日跟卖巡检；`
      + "公开快照完成后会在此补齐详情。"
    );
  } else {
    targetManagerNotice.value = card.target
      ? `PLID${card.plid} 已在监控队列和你的个人监控池中，正在等待首次采集。`
      : `PLID${card.plid} 当前只保留在你的个人监控池中；重新粘贴原链接可恢复全局监控。`;
  }
}

async function addSharedCardToPersonalWatchlist(
  card: PersonalWatchlistWorkspaceCard,
): Promise<void> {
  if (card.personalMember || personalWatchlistBusyPlid.value) return;
  if (!personalWatchlistDefaultConfigured.value) {
    promptForPersonalWatchlistDefaultBeforeMembership(card.plid);
    return;
  }
  personalWatchlistBusyPlid.value = card.plid;
  clearPersonalWatchlistFeedback();
  try {
    await persistPersonalWatchlistAddition(card.plid);
    personalWatchlistNotice.value = (
      `PLID${card.plid} 已加入你的个人监控池；共享类型库归属保持不变。`
    );
    await focusPersonalWatchlistCard(card.plid);
  } catch (error) {
    personalWatchlistError.value = error instanceof Error
      ? error.message
      : "加入个人监控池失败";
  } finally {
    personalWatchlistBusyPlid.value = "";
  }
}

async function removeFromPersonalWatchlist(plid: string): Promise<void> {
  if (personalWatchlistBusyPlid.value) return;
  clearPersonalWatchlistFeedback();
  personalWatchlistBusyPlid.value = plid;
  try {
    await deleteCompetitorPersonalWatchlistItem(plid);
    setPersonalWatchlistLocal(plid, false);
    await loadPersonalWatchlist();
    personalWatchlistNotice.value = (
      `PLID${plid} 已从你的个人监控池删除；全局监控队列、每日采集和历史记录不受影响。`
    );
  } catch (error) {
    personalWatchlistError.value =
      error instanceof Error ? error.message : "更新个人监控池失败";
  } finally {
    personalWatchlistBusyPlid.value = "";
  }
}

async function toggleSelectedPersonalWatchlist(): Promise<void> {
  const item = selected.value;
  if (!item || personalWatchlistBusyPlid.value) return;
  const removing = personalWatchlistPlids.value.has(item.plid);
  if (removing) {
    await removeFromPersonalWatchlist(item.plid);
    return;
  }
  if (!personalWatchlistDefaultConfigured.value) {
    closeProductModal();
    promptForPersonalWatchlistDefaultBeforeMembership(item.plid);
    return;
  }
  clearPersonalWatchlistFeedback();
  personalWatchlistBusyPlid.value = item.plid;
  try {
    await persistPersonalWatchlistAddition(item.plid);
    closeProductModal();
    personalWatchlistNotice.value = `PLID${item.plid} 已加入你的个人监控池并定位到对应卡片。`;
    await focusPersonalWatchlistCard(item.plid);
  } catch (error) {
    personalWatchlistError.value =
      error instanceof Error ? error.message : "更新个人监控池失败";
  } finally {
    personalWatchlistBusyPlid.value = "";
  }
}

function clearPersonalWatchlistFeedback(): void {
  personalWatchlistError.value = "";
  personalWatchlistNotice.value = "";
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
  if (
    plid
    && !personalWatchlistDefaultConfigured.value
    && !personalWatchlistPlids.value.has(plid)
  ) {
    promptForPersonalWatchlistDefault(url);
    return;
  }
  const existingTarget = targets.value.find((target) => target.plid === plid);
  if (existingTarget) {
    targetManagerBusy.value = "add";
    try {
      const personalWatchlistCreated = await persistPersonalWatchlistAddition(plid);
      newTargetUrl.value = "";
      showDuplicateTarget(existingTarget, personalWatchlistCreated);
      await focusPersonalWatchlistCard(plid);
    } catch (error) {
      targetManagerError.value =
        error instanceof Error ? error.message : "加入监控队列和个人监控池失败";
    } finally {
      targetManagerBusy.value = "";
    }
    return;
  }
  targetManagerBusy.value = "add";
  try {
    const result = await createCompetitorTarget(url);
    newTargetUrl.value = "";
    if (result.automatic_store_target) {
      if (result.personal_watchlist_member) {
        setPersonalWatchlistLocal(
          plid,
          true,
          result.personal_watchlist_item,
        );
      }
      const storeNames = result.store_names.join("、") || "已接入店铺";
      targetManagerNotice.value = (
        `PLID${plid} 属于自有店铺（${storeNames}），无需重复加入真正竞品；`
        + `已加入你的个人监控池，并持续参加${allStoreTargetCount.value}条自有链接的每日全量跟卖巡检。`
      );
      await focusPersonalWatchlistCard(plid);
      return;
    }
    if (!result.item) {
      throw new Error("新增接口未返回竞品记录，请刷新后重试");
    }
    if (result.personal_watchlist_member) {
      setPersonalWatchlistLocal(
        result.item.plid,
        true,
        result.personal_watchlist_item,
      );
    }
    await loadTargets();
    if (props.isAdmin) await loadSharedBatchStatus();
    targetManagerNotice.value = result.queued_to_active_batch
      ? `PLID${result.item.plid} 已加入监控队列和你的个人监控池，同时追加到当前运行批次队尾；断点中的原任务顺序保持不变。`
      : `PLID${result.item.plid} 已加入监控队列和你的个人监控池，将进入下一次采集清单。`;
    await focusPersonalWatchlistCard(result.item.plid);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 409 && plid) {
      let personalWatchlistCreated = false;
      try {
        personalWatchlistCreated = await persistPersonalWatchlistAddition(plid);
        await loadTargets();
      } catch (membershipError) {
        targetManagerError.value = membershipError instanceof Error
          ? membershipError.message
          : "加入个人监控池失败";
        return;
      }
      const duplicate = targets.value.find((target) => target.plid === plid);
      if (duplicate) {
        newTargetUrl.value = "";
        showDuplicateTarget(duplicate, personalWatchlistCreated);
        await focusPersonalWatchlistCard(plid);
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

function showDuplicateTarget(
  target: CompetitorTargetItem,
  personalWatchlistCreated: boolean,
) {
  targetManagerError.value = "";
  targetManagerNotice.value = "";
  duplicateTarget.value = {
    plid: target.plid,
    hasHistory: target.has_history,
    personalWatchlistCreated,
  };
}

async function jumpToDuplicateTarget() {
  const duplicate = duplicateTarget.value;
  if (!duplicate) return;
  if (!personalWatchlistPlids.value.has(duplicate.plid)) {
    await persistPersonalWatchlistAddition(duplicate.plid);
  }
  await focusPersonalWatchlistCard(duplicate.plid);
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
  target: Pick<CompetitorTargetItem, "plid">,
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
    `监控清单新增了 ${appended} 个链接，已按新增顺序追加到当前批次队尾。`,
  );
  persistCollectionCheckpoint();
}

function applyQueuedTargetsToRunQueue(
  queue: CollectionQueueItem[],
  knownIndexes: Set<number>,
) {
  if (activeIndex.value !== null && activeRequestId.value) return;
  for (const target of sharedBatchStatus.value.queued_targets ?? []) {
    const index = batchUrls.value.findIndex(
      (url) => plidFromUrl(url) === target.plid,
    );
    if (index < 0) continue;
    const existingPosition = queue.findIndex(
      (item) => item.index === index && !item.priority,
    );
    if (existingPosition >= 0) {
      knownIndexes.add(index);
      continue;
    }
    if (attemptedIndexes.value.includes(index) || knownIndexes.has(index)) {
      continue;
    }
    const pendingItem = resumeQueue.value.find((item) => item.index === index);
    if (!pendingItem) continue;
    queue.push(pendingItem);
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

function restoreCheckpointCollectionClientId(
  checkpoint: CollectionCheckpoint | null,
) {
  if (
    !checkpoint?.clientId
    || checkpointPendingCount(checkpoint) === 0
  ) {
    return;
  }
  collectionClientId = checkpoint.clientId;
  try {
    sessionStorage.setItem(collectionClientKey, collectionClientId);
  } catch {
    // The in-memory identity is enough when browser storage is unavailable.
  }
}

function closeCollectionClientChannel() {
  collectionClientChannel?.close();
  collectionClientChannel = null;
}

async function ensureUniqueCollectionClientId() {
  if (typeof BroadcastChannel === "undefined") return;
  try {
    const channel = new BroadcastChannel(collectionClientChannelName);
    collectionClientChannel = channel;
    const occupied = await hasPersistentCollectionClientPeer({
      channel,
      clientId: collectionClientId,
      instanceId: collectionClientInstanceId,
      createProbeId: () => collectionId("request"),
      wait: async (milliseconds) => {
        await delay(milliseconds);
      },
    });
    if (!occupied) return;
    collectionClientId = collectionId("client");
    sessionStorage.setItem(collectionClientKey, collectionClientId);
  } catch {
    collectionClientChannel?.close();
    collectionClientChannel = null;
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
    version: collectionCheckpointVersion,
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
    running: collectionCheckpointIsRunning({
      collecting: collecting.value,
      manualStopRequested: manualStopRequested.value,
      detachRequested: collectionDetachRequested.value,
    }),
    activeIndex: activeIndex.value,
    activeRequestId: activeRequestId.value,
    stockUnprobedIndexes: stockUnprobedIndexes.value,
    autoResumeAt:
      autoResumeAt.value === null
        ? null
        : new Date(autoResumeAt.value).toISOString(),
    clientId: collectionClientId,
  };
  try {
    localStorage.setItem(collectionCheckpointKey, JSON.stringify(checkpoint));
  } catch {
    // Keep the live in-memory checkpoint when browser storage is unavailable.
  }
}

function readCollectionCheckpoint(): CollectionCheckpoint | null {
  let checkpoint: CollectionCheckpoint;
  try {
    const raw = localStorage.getItem(collectionCheckpointKey);
    if (!raw) return null;
    checkpoint = JSON.parse(raw) as CollectionCheckpoint;
  } catch {
    try {
      localStorage.removeItem(collectionCheckpointKey);
    } catch {
      // Ignore unavailable browser storage and continue with a fresh batch.
    }
    return null;
  }
  if (
    checkpoint.version !== collectionCheckpointVersion
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
    return null;
  }
  return checkpoint;
}

function checkpointPendingCount(checkpoint: CollectionCheckpoint) {
  const attempted = new Set(checkpoint.attemptedIndexes);
  const terminal = new Set(checkpoint.terminalIndexes ?? []);
  const pending = new Set(checkpoint.failedIndexes);
  checkpoint.batchUrls.forEach((_url, index) => {
    if (!attempted.has(index) && !terminal.has(index)) pending.add(index);
  });
  terminal.forEach((index) => pending.delete(index));
  return pending.size;
}

async function restoreCollectionCheckpoint(
  checkpoint: CollectionCheckpoint,
  forceAdopt = false,
) {
  const checkpointBelongsToThisPage =
    checkpoint.version < collectionCheckpointVersion
    || checkpoint.clientId === collectionClientId;
  const checkpointMatchesActiveBatch =
    sharedBatchStatus.value.active
    && Boolean(checkpoint.batchId)
    && sharedBatchStatus.value.batch_id === checkpoint.batchId;
  if (!checkpointBelongsToThisPage && !forceAdopt) {
    adoptableCheckpoint.value = checkpoint;
    return false;
  }
  if (
    sharedBatchStatus.value.active
    && !checkpointMatchesActiveBatch
  ) {
    return false;
  }
  if (
    checkpoint.version < collectionCheckpointVersion
    && checkpointMatchesActiveBatch
    && !(await confirmLegacyCheckpointOwnership(checkpoint))
  ) {
    return false;
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
  if (checkpoint.version < collectionCheckpointVersion) {
    persistCollectionCheckpoint();
  }
  adoptableCheckpoint.value = null;
  return true;
}

async function confirmLegacyCheckpointOwnership(
  checkpoint: CollectionCheckpoint,
): Promise<boolean> {
  if (
    !props.canControlCollection
    || !checkpoint.batchId
    || !sharedBatchBelongsToCurrentAccount.value
  ) {
    return false;
  }
  const status = sharedBatchStatus.value;
  try {
    sharedBatchStatus.value = await logCompetitorBatchEvent({
      batchId: checkpoint.batchId,
      clientId: collectionClientId,
      event: "heartbeat",
      completed: status.completed,
      total: status.total,
      pending: status.pending,
      succeeded: status.succeeded,
      failed: status.failed,
      terminal: status.terminal,
      withStockProbe: status.with_stock_probe,
      visibleBrowser: status.visible_browser,
      reason: status.reason,
    });
    return true;
  } catch {
    return false;
  }
}

async function startCollection() {
  if (!props.canControlCollection) {
    showCollectionNotice(
      "竞品批次的开始、继续和停止仅限 kxx 账号；当前账号仍可新增链接和插队。",
    );
    return;
  }
  if (collectionPreparing.value) return;
  collectionDetachRequested.value = false;
  collectionPreparing.value = true;
  collectionErrors.value = [];
  try {
    const [latestTargets, latestAllStoreTargetPayload] = await Promise.all([
      fetchCompetitorTargets(),
      fetchCompetitorStoreTargets("all"),
    ]);
    if (collectionDetachRequested.value) return;
    targets.value = latestTargets;
    allStoreTargets.value = latestAllStoreTargetPayload.items;
    allStoreTargetCount.value = latestAllStoreTargetPayload.all_store_unique_count;
    allStoreTargetMembershipCount.value =
      latestAllStoreTargetPayload.all_store_membership_count;
    allStoreTrackingStoreCount.value = latestAllStoreTargetPayload.all_store_count;
    const urls = mergeUniqueTargetUrls(
      latestTargets.map((target) => target.url),
      latestAllStoreTargetPayload.items.map((target) => target.url),
    );
    await startNewCollection(
      urls,
      "当前没有可采集的真正竞品或全部自有店铺链接",
    );
  } catch (error) {
    collectionErrors.value = [
      {
        plid: "",
        url: "",
        message: error instanceof Error ? error.message : "刷新全部采集目标失败",
      },
    ];
  } finally {
    collectionPreparing.value = false;
  }
}

async function startNewCollection(urls: string[], emptyMessage: string) {
  if (collectionDetachRequested.value) return;
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
    if (!urls.length) throw new Error(emptyMessage);
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
  if (collectionDetachRequested.value) return;
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
  if (sharedBatchBelongsToCurrentAccount.value) {
    return `本账号已有竞品批次在另一页面运行：已检查 ${status.completed}/${status.total}，待续爬 ${status.pending}${current}。为避免两个库存浏览器并发，本页面不会重复启动；请回到原页面操作。`;
  }
  return `${sharedBatchOwner.value} 的竞品批次正在运行：已检查 ${status.completed}/${status.total}，待续爬 ${status.pending}${current}。请等待当前批次结束，或由发起人点击“停止采集”后再开始。`;
}

async function updateVisibleBrowserSetting() {
  const status = sharedBatchStatus.value;
  if (!status.active) {
    persistCollectionCheckpoint();
    return;
  }
  if (!canUpdateVisibleBrowser.value || !status.batch_id) {
    visibleBrowser.value = status.visible_browser;
    showCollectionNotice("当前账号无权修改运行批次的显示浏览器设置。");
    return;
  }
  try {
    sharedBatchStatus.value = await updateCompetitorBatchOptions(
      status.batch_id,
      visibleBrowser.value,
    );
    if (status.source !== "scheduled") persistCollectionCheckpoint();
    showCollectionActivityNotice(
      visibleBrowser.value
        ? "已同步到运行批次：从下一条链接开始显示库存探测浏览器。"
        : "已同步到运行批次：从下一条链接开始隐藏库存探测浏览器。",
    );
  } catch (error) {
    visibleBrowser.value = status.visible_browser;
    if (status.source !== "scheduled") persistCollectionCheckpoint();
    showCollectionNotice(
      error instanceof Error ? error.message : "更新显示浏览器设置失败",
    );
  }
}

async function takeOverAndResumeCollection() {
  if (!props.canControlCollection || takeoverBusy.value) return;
  let checkpoint = adoptableCheckpoint.value ?? readCollectionCheckpoint();
  if (!checkpoint || checkpointPendingCount(checkpoint) === 0) {
    showCollectionNotice("没有找到可接回的失败或未完成断点。");
    return;
  }
  takeoverBusy.value = true;
  collectionStopReason.value = "";
  try {
    const activeStatus = sharedBatchStatus.value;
    if (activeStatus.active) {
      if (
        !sharedBatchBelongsToCurrentAccount.value
        || !activeStatus.batch_id
        || activeStatus.batch_id !== checkpoint.batchId
      ) {
        throw new Error("当前运行批次与本页保存的断点不一致，不能安全接管。");
      }
      let ready = false;
      for (let attempt = 0; attempt < 600 && !ready; attempt += 1) {
        try {
          const takeover = await takeoverCompetitorBatch(
            activeStatus.batch_id,
            collectionClientId,
          );
          sharedBatchStatus.value = takeover.status;
          ready = takeover.ready;
        } catch (error) {
          await loadSharedBatchStatus();
          if (!sharedBatchStatus.value.active) break;
          throw error;
        }
        if (!ready) {
          showCollectionNotice(
            "已申请接管本账号批次；正在等待当前商品探测完成，完成后自动继续待重试链接。",
          );
          await delay(1_000);
        }
      }
      if (!ready && sharedBatchStatus.value.active) {
        throw new Error("等待当前商品结束超过10分钟，暂未完成接管，请稍后重试。");
      }
      // Let the former page receive its lease rejection and write its last
      // checkpoint before this page adopts and becomes the sole writer.
      await delay(1_500);
      checkpoint = readCollectionCheckpoint() ?? checkpoint;
    }
    if (
      sharedBatchStatus.value.active
      && sharedBatchStatus.value.batch_id !== checkpoint.batchId
    ) {
      throw new Error("服务端批次与本地断点已不一致，已停止接管以避免重复采集。");
    }
    const restored = await restoreCollectionCheckpoint(checkpoint, true);
    if (!restored || !pendingResumeCount.value) {
      throw new Error("断点中没有可继续的待重试链接。");
    }
    restoredRunWasActive.value = false;
    collectionStopReason.value = "";
    collectionActivityNotice.value =
      `已接管本账号批次，正在继续 ${pendingResumeCount.value} 条待重试或未完成链接。`;
    persistCollectionCheckpoint();
    await resumeCollection("auto_resume");
  } catch (error) {
    showCollectionNotice(
      error instanceof Error ? error.message : "接管竞品批次失败",
    );
  } finally {
    takeoverBusy.value = false;
    await loadSharedBatchStatus();
  }
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
    || collectionDetachRequested.value
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
      withStockProbe: withStockProbe.value,
      visibleBrowser: visibleBrowser.value,
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
    if (collectionDetachRequested.value) {
      // Logout/session replacement already saved a resumable checkpoint.
    } else if (mode === "scheduled_resume") {
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
    while (
      !controller.signal.aborted
      && !collectionStopReason.value
      && !collectionDetachRequested.value
    ) {
      applyQueuedTargetsToRunQueue(queue, knownIndexes);
      applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
      while (cursor < queue.length) {
        if (controller.signal.aborted || collectionDetachRequested.value) break;
        applyQueuedTargetsToRunQueue(queue, knownIndexes);
        applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
        const {
          index,
          url,
          priority = false,
          retryKind,
          retryAttempt,
        } = queue[cursor]!;
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
              retryKind,
              retryAttempt,
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
          if (controller.signal.aborted || collectionDetachRequested.value) break;
          if (
            error instanceof ApiRequestError
            && isCollectionSessionBoundaryStatus(error.status)
          ) {
            collectionDetachRequested.value = true;
            controller.abort();
            persistCollectionCheckpoint();
            break;
          }
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
                  {
                    index,
                    url,
                    retryKind: "stock",
                    retryAttempt: retryCount,
                  },
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
                  {
                    index,
                    url,
                    retryKind: "automatic",
                    retryAttempt: retryCount,
                  },
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
          if (
            !shouldPreserveActiveCollectionRequest(
              collectionDetachRequested.value,
              settled,
            )
          ) {
            activeIndex.value = null;
            activeRequestId.value = null;
            activeStartedAt.value = null;
          }
          persistCollectionCheckpoint();
        }
        if (
          batchLeaseConflict
          || controller.signal.aborted
          || collectionDetachRequested.value
        ) break;
        await recordBatchEvent("progress");
        appendPendingItemsToRunQueue(queue, knownIndexes, cursor);
        if (collectionStopReason.value || controller.signal.aborted) break;
        if (cursor < queue.length) await delay(1_000);
      }
      if (
        batchLeaseConflict
        || controller.signal.aborted
        || collectionStopReason.value
        || collectionDetachRequested.value
      ) break;
      await loadSharedBatchStatus();
      appendPendingItemsToRunQueue(queue, knownIndexes, cursor);
      applyQueuedTargetsToRunQueue(queue, knownIndexes);
      applyPriorityTargetsToRunQueue(queue, cursor, knownIndexes);
      if (cursor >= queue.length) break;
    }
    if (!collectionDetachRequested.value) {
      await Promise.all([loadOverview(), loadTargets()]);
    }
  } finally {
    if (collectionDetachRequested.value) {
      // The server-side request is shielded and may still finish. Keep the
      // request identity in the browser checkpoint so kxx can rejoin it.
    } else if (batchLeaseConflict) {
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

function detachCollectionForSessionChange() {
  if (!collecting.value && !collectionPreparing.value) return;
  collectionDetachRequested.value = true;
  if (!collecting.value) return;
  persistCollectionCheckpoint();
  abortController.value?.abort();
}

async function stopCollection() {
  if (!props.canControlCollection) {
    showCollectionNotice(
      "竞品批次的开始、继续和停止仅限 kxx 账号；当前账号仍可新增链接和插队。",
    );
    return;
  }
  const activeBatchId = sharedBatchStatus.value.active
    ? sharedBatchStatus.value.batch_id
    : batchId.value;
  if (!activeBatchId) {
    showCollectionNotice("当前没有可停止的竞品采集批次。");
    return;
  }
  const scheduledBatch = sharedBatchStatus.value.source === "scheduled";
  manualStopRequested.value = collecting.value;
  clearAutomaticResumeSchedule();
  collectionStopReason.value = scheduledBatch
    ? "已手动中断今日 09:00 自动批次；今天不会再次自动启动，明天 09:00 照常。"
    : "已手动停止；当前浏览器探测已中断并关闭，可以点击“继续失败/未完成”从断点恢复。";
  if (collecting.value) persistCollectionCheckpoint();
  const activeController = abortController.value;
  const stopRequest = stopCompetitorBatch(
    activeBatchId,
    collectionStopReason.value,
  );
  activeController?.abort();
  try {
    sharedBatchStatus.value = await stopRequest;
  } catch (error) {
    showCollectionNotice(
      error instanceof Error
        ? `停止信号发送失败：${error.message}`
        : "停止信号发送失败，请再次点击停止采集",
    );
  }
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

function offerTrendX(index: number, trend: CompetitorOfferTrendPoint[]) {
  const count = trend.length;
  if (count <= 1) return offerTrendPlotLeft + offerTrendPlotWidth / 2;
  const firstTime = trend[0]?.capturedAtMs;
  const lastTime = trend[count - 1]?.capturedAtMs;
  const currentTime = trend[index]?.capturedAtMs;
  const ratio = firstTime !== undefined
    && lastTime !== undefined
    && currentTime !== undefined
    && Number.isFinite(firstTime)
    && Number.isFinite(lastTime)
    && Number.isFinite(currentTime)
    && lastTime > firstTime
    ? (currentTime - firstTime) / (lastTime - firstTime)
    : index / (count - 1);
  return offerTrendPlotLeft + ratio * offerTrendPlotWidth;
}

function formatOfferTrendAxisValue(
  key: OfferTrendPanel["key"],
  value: number,
) {
  if (key === "price") return `R ${Math.round(value).toLocaleString("en-ZA")}`;
  return Math.round(value).toLocaleString("zh-CN");
}

function handleOfferTrendPointer(event: PointerEvent) {
  if (!selectedOfferTrend.value.length) return;
  const svg = event.currentTarget as SVGSVGElement;
  const bounds = svg.getBoundingClientRect();
  if (!bounds.width) return;
  const viewX = ((event.clientX - bounds.left) / bounds.width) * offerTrendChartWidth;
  hoveredOfferTrendIndex.value = selectedOfferTrend.value.reduce(
    (nearestIndex, _point, index) =>
      Math.abs(offerTrendX(index, selectedOfferTrend.value) - viewX)
        < Math.abs(offerTrendX(nearestIndex, selectedOfferTrend.value) - viewX)
        ? index
        : nearestIndex,
    0,
  );
}

function stepOfferTrendPoint(direction: -1 | 1) {
  if (!selectedOfferTrend.value.length) return;
  const current = activeOfferTrendIndex.value ?? selectedOfferTrend.value.length - 1;
  hoveredOfferTrendIndex.value = Math.min(
    selectedOfferTrend.value.length - 1,
    Math.max(0, current + direction),
  );
}

function offerIntervalMovementLabel(offer: CompetitorOfferItem) {
  if (!offer.库存可比 || offer.库存数量变化 === null) return "区间库存不可比";
  if (offer.库存数量变化 < 0) return `区间库存净流出 ${Math.abs(offer.库存数量变化)} 件`;
  if (offer.库存数量变化 > 0) return `区间库存净补货 ${offer.库存数量变化} 件`;
  return "区间库存数量不变";
}

function offerNetOutflowRankLabel(offer: CompetitorOfferItem) {
  const outflow = comparableOfferNetOutflow(offer);
  return outflow === null ? "净流出不可比" : `净流出 ${outflow} 件`;
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

function sellerGroupPriceRange(offers: CompetitorOfferItem[]) {
  const prices = offers
    .map((offer) => offer.价格)
    .filter((price): price is number => price !== null)
    .sort((first, second) => first - second);
  if (!prices.length) return "价格待采集";
  const lowest = prices[0]!;
  const highest = prices[prices.length - 1]!;
  return lowest === highest
    ? formatCurrency(lowest)
    : `${formatCurrency(lowest)} – ${formatCurrency(highest)}`;
}

function sellerGroupStockSummary(offers: CompetitorOfferItem[]) {
  const inStock = offers.filter((offer) => offer.库存状态 === "有货").length;
  const outOfStock = offers.filter((offer) => offer.库存状态 === "没货").length;
  const unknown = offers.length - inStock - outOfStock;
  return [
    inStock ? `${inStock} 个有货` : "",
    outOfStock ? `${outOfStock} 个没货` : "",
    unknown ? `${unknown} 个未探测` : "",
  ].filter(Boolean).join(" · ") || "库存待采集";
}

function followerSellerCount(item: CompetitorItem) {
  return groupCompetitorOffersBySeller(followerOffers(item), "default").length;
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
  <div
    class="competitor-module"
    :class="{ 'admin-priority-layout': props.isAdmin }"
  >
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

    <section
      class="panel personal-watchlist-summary-card personal-operator-workspace"
      :class="{ 'operator-primary': !props.isAdmin }"
      aria-labelledby="personal-watchlist-workspace-title"
    >
      <div class="personal-watchlist-summary-copy">
        <p class="section-kicker">当前账号专属工作区</p>
        <strong id="personal-watchlist-workspace-title">
          {{ props.currentUsername || "当前账号" }} 的个人监控池
        </strong>
        <span>
          这里直接展示你关注的真正竞品和自有店铺商品。粘贴链接会自动识别来源并加入当前账号个人池；
          真正竞品进入监控队列，自有商品沿用每日全量跟卖巡检；个人归类默认私有，只有主动分享的类型库对指定用户开放。
        </span>
      </div>
      <div class="personal-watchlist-summary-count" aria-label="当前账号个人监控池商品数量">
        <strong>{{ personalWatchlistPlids.size }}</strong>
        <span>个我的商品</span>
      </div>
      <button
        type="button"
        class="secondary-button personal-watchlist-library-settings-button"
        @click="openPersonalWatchlistLibrarySettings"
      >
        类型库设置
      </button>
      <form class="target-add-row personal-watchlist-quick-add" @submit.prevent="addTarget">
        <label for="personal-watchlist-link">新增监控链接</label>
        <input
          id="personal-watchlist-link"
          v-model="newTargetUrl"
          type="url"
          aria-label="新增 Takealot 竞品链接并同时加入监控队列和个人监控池"
          placeholder="粘贴 Takealot 商品链接，例如 https://www.takealot.com/.../PLID12345678"
          :disabled="targetManagerBusy === 'add' || !props.canOperate"
          @input="clearTargetManagerFeedback"
        />
        <button
          class="primary-button"
          type="submit"
          :disabled="targetManagerBusy === 'add' || !props.canOperate"
        >
          {{
            targetManagerBusy === "add"
              ? "正在加入…"
              : "加入监控队列和我的监控池"
          }}
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
              : "该链接已在监控队列中，正在等待首次采集。"
          }}
          {{
            duplicateTarget.personalWatchlistCreated
              ? "已自动加入你的个人监控池。"
              : "该商品也已在你的个人监控池中。"
          }}
        </span>
        <button
          class="secondary-button"
          type="button"
          @click="jumpToDuplicateTarget"
        >
          定位到我的监控池
        </button>
      </div>
      <p v-if="targetManagerNotice" class="target-manager-message success" role="status">
        {{ targetManagerNotice }}
      </p>
      <p v-if="personalWatchlistError" class="target-manager-message error" role="alert">
        {{ personalWatchlistError }}
      </p>
      <p v-if="personalWatchlistNotice" class="target-manager-message success" role="status">
        {{ personalWatchlistNotice }}
      </p>

      <section
        class="personal-watchlist-board"
        aria-labelledby="personal-watchlist-board-title"
      >
        <div class="personal-watchlist-board-heading">
          <div>
            <p class="section-kicker">MY PERSONAL WATCHLIST</p>
            <h3 id="personal-watchlist-board-title">个人监控池商品</h3>
          </div>
          <span v-if="personalWatchlistItems.length || activePersonalWatchlistLibrary">
            第 {{ personalWatchlistPage }}/{{ personalWatchlistPageCount }} 页 ·
            <template v-if="activePersonalWatchlistLibrary">
              {{ activePersonalWatchlistLibrary.name }} 共
              {{ activePersonalWatchlistLibrary.item_count }} 个
            </template>
            <template v-else>
              显示 {{ filteredPersonalWatchlistCards.length }} / 我的
              {{ personalWatchlistItems.length }} 个
            </template>
          </span>
        </div>

        <nav
          v-if="personalWatchlistCards.length || personalWatchlistLibraries.length"
          class="personal-watchlist-library-filter"
          aria-label="按个人类型库筛选监控池商品"
        >
          <div class="personal-watchlist-library-filter-group personal-filter-core">
            <span>我的监控池</span>
            <button
              type="button"
              :class="{ selected: personalWatchlistLibraryFilter === 'all' }"
              @click="personalWatchlistLibraryFilter = 'all'"
            >
              全部 <small>{{ personalWatchlistItems.length }}</small>
            </button>
            <button
              type="button"
              :class="{ selected: personalWatchlistLibraryFilter === 'unclassified' }"
              @click="personalWatchlistLibraryFilter = 'unclassified'"
            >
              未分类 <small>{{ unclassifiedPersonalWatchlistCount }}</small>
            </button>
          </div>
          <div
            v-if="ownedPersonalWatchlistLibraries.length"
            class="personal-watchlist-library-filter-group"
          >
            <span>我的类型库</span>
            <button
              v-for="library in ownedPersonalWatchlistLibraries"
              :key="`filter-${library.id}`"
              type="button"
              :class="{ selected: personalWatchlistLibraryFilter === library.id }"
              @click="personalWatchlistLibraryFilter = library.id"
            >
              {{ library.name }} <small>{{ library.item_count }}</small>
            </button>
          </div>
          <div
            v-if="sharedPersonalWatchlistLibraries.length"
            class="personal-watchlist-library-filter-group shared-library-filter-group"
          >
            <span>共享给我</span>
            <button
              v-for="library in sharedPersonalWatchlistLibraries"
              :key="`filter-${library.id}`"
              type="button"
              :class="{ selected: personalWatchlistLibraryFilter === library.id }"
              @click="personalWatchlistLibraryFilter = library.id"
            >
              {{ library.name }}
              <em>{{ library.access === "edit" ? "可编辑" : "只读" }}</em>
              <small>{{ library.item_count }}</small>
            </button>
          </div>
        </nav>

        <div
          v-if="filteredPersonalWatchlistCards.length"
          class="personal-watchlist-product-grid"
        >
          <article
            v-for="card in pagedPersonalWatchlistCards"
            :id="`personal-watchlist-card-${card.plid}`"
            :key="card.plid"
            class="personal-watchlist-product-card"
            :class="{
              'has-detail': Boolean(card.competitor),
              'is-shared-card': !card.personalMember,
              'is-highlighted': personalWatchlistHighlightPlid === card.plid,
            }"
            tabindex="-1"
            @click="openPersonalWatchlistCard(card)"
          >
            <div class="competitor-product-image personal-watchlist-product-image">
              <img
                v-if="card.competitor && canShowCompetitorImage(card.competitor.图片)"
                :src="competitorImageUrl(card.competitor.图片)"
                :alt="`${card.competitor.商品} 商品图片`"
                width="192"
                height="192"
                loading="lazy"
                decoding="async"
                @error="markCompetitorImageFailed(card.competitor.图片)"
              />
              <span v-else>暂无图片</span>
            </div>
            <div class="personal-watchlist-product-body">
              <div class="personal-watchlist-product-meta">
                <span>PLID{{ card.plid }}</span>
                <strong
                  v-if="personalWatchlistHighlightPlid === card.plid"
                  class="personal-watchlist-location-badge"
                  role="status"
                >已定位到此商品</strong>
                <small :class="{ inactive: card.source !== 'own_store' && !card.target }">
                  {{
                    !card.personalMember
                      ? "共享类型库"
                      : card.source === "own_store"
                      ? "自有店铺 · 每日巡检"
                      : card.target
                        ? "真正竞品 · 监控队列"
                        : "真正竞品 · 仅个人池"
                  }}
                </small>
              </div>
              <h4>
                {{ card.competitor?.商品 || card.target?.title || "等待首次采集" }}
              </h4>
              <div class="personal-watchlist-library-chips">
                <span
                  v-for="libraryName in personalWatchlistLibraryNames(card)"
                  :key="libraryName"
                >{{ libraryName }}</span>
                <small v-if="!personalWatchlistLibraryNames(card).length">未加入类型库</small>
              </div>
              <p v-if="card.competitor">
                主卖家 {{ card.competitor.当前卖家 || "未知" }} ·
                {{
                  card.competitor.来源 === "own_store"
                    ? ownStoreVariantCount(card.competitor)
                    : card.competitor.跟卖报价.length
                }} 个变体 / 报价
              </p>
              <p v-else-if="!card.personalMember">
                共享库仅传递库内 PLID；当前账号无权读取的店铺私有详情不会显示。
              </p>
              <p v-else-if="card.target">
                已加入两个清单，首次采集完成后会在这里补齐商品、价格和库存。
              </p>
              <p v-else>
                个人归类仍保留；如需恢复全局采集，请重新粘贴原商品链接。
              </p>
              <div v-if="card.competitor" class="personal-watchlist-product-metrics">
                <span>
                  <small>当前价格</small>
                  <strong>{{ formatCurrency(card.competitor.价格) }}</strong>
                </span>
                <span>
                  <small>当前库存</small>
                  <strong>{{ card.competitor.库存上限 }}</strong>
                </span>
                <span>
                  <small>最近采集</small>
                  <strong>{{ formatChinaDateTime(card.competitor.采集时间) }}</strong>
                </span>
              </div>
              <div class="personal-watchlist-product-actions">
                <span>
                  {{ card.personalMember ? "加入个人池" : "加入共享库" }}时间
                  {{ formatChinaDateTime(card.addedAt) }}
                </span>
                <button
                  v-if="card.competitor"
                  type="button"
                  class="secondary-button"
                  @click.stop="openProductModal(card.competitor)"
                >
                  查看商品详情
                </button>
                <button
                  v-if="card.personalMember"
                  type="button"
                  class="secondary-button"
                  @click.stop="openPersonalWatchlistCardLibraries(card)"
                >
                  设置类型库
                </button>
                <button
                  v-if="
                    !card.personalMember
                    && (card.competitor || card.target)
                  "
                  type="button"
                  class="secondary-button"
                  :disabled="Boolean(personalWatchlistBusyPlid)"
                  @click.stop="addSharedCardToPersonalWatchlist(card)"
                >加入我的监控池</button>
                <button
                  v-if="
                    activePersonalWatchlistLibrary
                    && canEditPersonalWatchlistLibrary(activePersonalWatchlistLibrary)
                  "
                  type="button"
                  class="secondary-button danger-soft"
                  :disabled="Boolean(personalWatchlistBusyPlid)"
                  @click.stop="removeCardFromActivePersonalWatchlistLibrary(card)"
                >从此类型库移除</button>
                <button
                  v-if="props.isAdmin && card.target"
                  type="button"
                  class="secondary-button"
                  @click.stop="openTargetActionForLink(card.plid, card.target.url)"
                >
                  监控队列操作
                </button>
                <button
                  v-if="card.personalMember"
                  type="button"
                  class="secondary-button danger"
                  :disabled="Boolean(personalWatchlistBusyPlid)"
                  @click.stop="removeFromPersonalWatchlist(card.plid)"
                >
                  {{
                    personalWatchlistBusyPlid === card.plid
                      ? "正在移除…"
                      : "从个人池移除"
                  }}
                </button>
              </div>
            </div>
          </article>
        </div>
        <div v-else class="personal-watchlist-empty-state">
          <strong>
            {{ activePersonalWatchlistLibrary
              ? "当前类型库暂无商品"
              : "个人监控池暂无商品" }}
          </strong>
          <span v-if="activePersonalWatchlistLibrary">
            {{ canEditPersonalWatchlistLibrary(activePersonalWatchlistLibrary)
              ? "可从自己的监控池卡片中把商品加入这个类型库。"
              : "该共享库为只读；内容由创建者或可编辑成员维护。" }}
          </span>
          <span v-else>
            在上方粘贴 Takealot 链接，系统会识别真正竞品或自有商品并加入你的个人监控池。
          </span>
        </div>
        <div v-if="personalWatchlistPageCount > 1" class="personal-watchlist-pagination">
          <button
            type="button"
            class="secondary-button"
            :disabled="personalWatchlistPage <= 1"
            @click="personalWatchlistPage -= 1"
          >上一页</button>
          <span>第 {{ personalWatchlistPage }} / {{ personalWatchlistPageCount }} 页</span>
          <button
            type="button"
            class="secondary-button"
            :disabled="personalWatchlistPage >= personalWatchlistPageCount"
            @click="personalWatchlistPage += 1"
          >下一页</button>
        </div>
      </section>
    </section>

    <section
      v-if="props.isAdmin"
      class="collector panel shared-management-panel"
    >
      <div class="section-heading">
        <div>
          <p class="section-kicker">管理员核心工作区</p>
          <h2>全局链接与批次</h2>
        </div>
        <p class="section-note">
          个人工作区之外的共享清单、插队、审计和管理员批次控制集中放在这里
        </p>
      </div>
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
                            <span>同一 PLID 只入队一次，价格和库存按卖家报价身份区分</span>
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
                                <template v-if="offer.SKU">SKU {{ offer.SKU }}</template>
                                <template v-if="offer.变体 && offer.变体 !== '默认款'">
                                  · {{ offer.变体 }}
                                </template>
                                <template v-if="offer.条件"> · {{ offer.条件 }}</template>
                              </small>
                              <small v-if="offer.offer_id" class="offer-id-secondary">
                                Offer ID {{ offer.offer_id }}
                              </small>
                            </div>
                            <div class="target-offer-price">
                              <strong>{{ formatCurrency(offer.价格) }}</strong>
                              <small
                                v-if="offerPriceOperatingSignal(offer)"
                                class="price-signal"
                                :class="priceSignalClass(offer.价格信号)"
                              >
                                {{ offerPriceOperatingSignal(offer) }}
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
                              <small
                                v-if="offerStockOperatingSignal(offer)"
                                :class="offerStockSignalClass(offer.库存信号)"
                              >
                                {{ offerStockOperatingSignal(offer) }}
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
          <p class="section-kicker">
            {{ props.canControlCollection ? "管理员批次控制" : "共享采集状态" }}
          </p>
          <h3>批量采集真正竞品 + 自有链接</h3>
        </div>
        <span>
          真正竞品 {{ targets.length }} 个 · 全部有权店铺自有链接
          {{ allStoreTargets.length }} 个 · 本批去重后 {{ unifiedCollectionUrls.length }} 个
        </span>
      </div>
      <p class="method-note collection-scope-note">
        每次点击开始都会重新读取最新真正竞品清单，再装入全部有权店铺（管理员当前为六店）的全部
        自有链接，并按PLID去重后在同一个串行批次中采集；顶栏店铺只影响页面查看范围，不会缩小
        手动采集范围。两类链接都可插队，断点继续保留原顺序。服务器每天09:00会自动启动同一个
        共享串行批次，读取全部活跃真正竞品和所有已接入店铺的当前自有PLID；所有账号看到相同进度，
        kxx可随时停止，运行中发现的新链接会追加到队尾。
        自有商品价格与库存使用 Seller API，公开页只识别并探测排除自有Offer后的跟卖报价。
      </p>
      <div
        v-if="sharedBatchStatus.active"
        class="shared-collection-status"
        role="status"
        aria-live="polite"
      >
        <div>
          <strong>
            {{ sharedScheduledPause ? "全员同步暂停中" : "全员同步采集中" }}
            · {{ sharedBatchOwnerLabel }}
          </strong>
          <span>
            已检查 {{ sharedBatchStatus.completed }}/{{ sharedBatchStatus.total }}
            · 成功 {{ sharedBatchStatus.succeeded }}
            · 未解决 {{ sharedBatchStatus.failed }}
            · 确认失效 {{ retainedConfirmedInvalidCount }}
            · 待续爬 {{ sharedBatchStatus.pending }}
          </span>
          <small v-if="sharedBatchStatus.failed">
            未解决数量只在某条链接成功后减少；复探进行中数字可能暂时不变。
          </small>
          <small v-if="sharedScheduledPause" class="collection-auto-resume-countdown">
            {{ sharedBatchStatus.reason }}
          </small>
        </div>
        <span v-if="sharedBatchStatus.current_plid" class="shared-current-plid">
          当前第 {{ (sharedBatchStatus.current_index ?? 0) + 1 }} 条 ·
          PLID{{ sharedBatchStatus.current_plid }}
          <small v-if="sharedRetryProgress">{{ sharedRetryProgress }}</small>
          <small v-if="sharedBatchStatus.current_stage">
            {{ sharedBatchStatus.current_stage }}
          </small>
        </span>
        <span v-else-if="sharedScheduledPause">网络暂停，等待自动续爬；kxx可随时停止</span>
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
            @change="updateVisibleBrowserSetting"
            :disabled="
              !withStockProbe
              || !canUpdateVisibleBrowser
            "
          />
          <span class="switch"></span>
          <span>
            <strong>显示检测浏览器</strong>
            <small>运行中可切换，从下一条任务链接开始生效</small>
          </span>
        </label>
        <button
          v-if="
            props.canControlCollection
            && !collecting
            && adoptableCheckpoint
            && adoptablePendingCount > 0
            && (
              !sharedBatchStatus.active
              || sharedBatchBelongsToCurrentAccount
            )
          "
          class="primary-button resume-button"
          @click="takeOverAndResumeCollection"
          :disabled="!canTakeOverCollection"
        >
          {{ takeoverBusy ? "等待当前商品结束…" : `接管并继续待重试（${adoptablePendingCount}）` }}
        </button>
        <button
          class="primary-button"
          @click="startCollection"
          v-if="props.canControlCollection && !collecting"
          :disabled="
            anotherBatchIsActive
            || collectionPreparing
          "
        >
          {{ collectionPreparing ? "正在核对最新全量清单…" : `开始采集（${unifiedCollectionUrls.length}）` }}
        </button>
        <button
          v-if="props.canControlCollection && !collecting && pendingResumeCount"
          class="primary-button resume-button"
          @click="resumeCollection()"
          :disabled="anotherBatchIsActive"
        >
          继续失败/未完成（{{ pendingResumeCount }}）
        </button>
        <button
          class="primary-button stop-button"
          @click="stopCollection"
          v-if="props.canControlCollection && (collecting || sharedBatchStatus.active)"
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
            {{ collectionAlertTitle }}
          </strong>
          <small>{{ collectionStopReason }}</small>
          <small v-if="autoResumeAt" class="collection-auto-resume-countdown">
            距离下次自动尝试：{{ autoResumeCountdown }}
          </small>
        </span>
      </div>
      <div
        v-if="hasDisplayedBatchProgress"
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
          showCollectionDetails
          && (displayedCollectionResults.length || displayedCollectionErrors.length)
        "
        class="collection-task-detail collection-task-detail-panel"
      >
        <summary>
          <span>
            <strong>任务爬取详情</strong>
            <small>成功与待重试任务在同一面板内分组查看</small>
          </span>
          <b>{{ displayedCollectionResults.length + displayedCollectionErrors.length }}</b>
        </summary>
        <div class="collection-task-detail-groups">
          <section
            v-if="displayedCollectionResults.length"
            class="collection-task-detail-group success"
          >
            <header>
              <strong>成功任务</strong>
              <span>{{ displayedCollectionResults.length }} 个</span>
            </header>
            <div class="collection-task-detail-list">
              <article
                v-for="result in displayedCollectionResults"
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
          <section
            v-if="displayedCollectionErrors.length"
            class="collection-task-detail-group retry"
          >
            <header>
              <strong>待重试任务</strong>
              <span>{{ displayedCollectionErrors.length }} 个</span>
            </header>
            <div class="collection-task-detail-list">
              <article
                v-for="error in displayedCollectionErrors"
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
        <span>自有店铺链接</span>
        <strong>{{ storeCompetitors.length }}</strong>
        <small>
          {{ ownStoreScopeLabel }} · 目标 {{ storeTargets.length }} 个
          <template v-if="ownStoreScope === 'all'">
            · 店铺内合计 {{ storeTargetMembershipCount }} 个
          </template>
        </small>
      </article>
      <article>
        <span>真正竞品</span>
        <strong>{{ competitors.length }}</strong>
        <small>与自有店铺分区</small>
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
      v-if="props.isAdmin && linkHealth.length"
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
        v-if="personalWatchlistLibraryModalOpen"
        class="competitor-modal-backdrop personal-watchlist-library-backdrop"
        @click.self="closePersonalWatchlistLibraryModal"
      >
        <section
          class="personal-watchlist-library-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="personal-watchlist-library-modal-title"
        >
          <header class="personal-watchlist-library-modal-header">
            <div>
              <p class="section-kicker">PERSONAL TYPE LIBRARIES</p>
              <h2 id="personal-watchlist-library-modal-title">个人监控池类型库</h2>
              <span>我的库由我管理；共享库按创建者授予的只读或可编辑权限协作。</span>
            </div>
            <button
              type="button"
              class="competitor-modal-close"
              aria-label="关闭类型库设置"
              @click="closePersonalWatchlistLibraryModal"
            >×</button>
          </header>

          <div class="personal-watchlist-library-modal-body">
            <p
              v-if="personalWatchlistLibraryError"
              class="target-manager-message error"
              role="alert"
            >{{ personalWatchlistLibraryError }}</p>
            <p
              v-if="personalWatchlistLibraryNotice"
              class="target-manager-message success"
              role="status"
            >{{ personalWatchlistLibraryNotice }}</p>

            <section
              v-if="personalWatchlistLibraryAssignmentPlid"
              class="personal-watchlist-library-section"
            >
              <div class="personal-watchlist-library-section-heading">
                <div>
                  <p class="section-kicker">CARD CLASSIFICATION</p>
                  <h3>PLID{{ personalWatchlistLibraryAssignmentPlid }} 的类型库</h3>
                </div>
                <span>一张卡片可以同时加入多个类型库</span>
              </div>
              <div
                v-if="personalWatchlistLibraries.length"
                class="personal-watchlist-library-options"
              >
                <label
                  v-for="library in personalWatchlistLibraries"
                  :key="`assignment-${library.id}`"
                  :class="{ 'is-read-only': library.access === 'read' }"
                >
                  <input
                    type="checkbox"
                    :checked="personalWatchlistLibrarySelection.includes(library.id)"
                    :disabled="library.access === 'read'"
                    @change="togglePersonalWatchlistLibrarySelection(library.id)"
                  />
                  <span>
                    {{ library.name }}
                    <em v-if="library.access !== 'owner'">
                      {{ library.owner_display_name }}共享
                    </em>
                  </span>
                  <small>
                    {{ library.access === "read" ? "只读" : "可编辑" }} ·
                    {{ library.item_count }} 个
                  </small>
                </label>
              </div>
              <div v-else class="personal-watchlist-library-empty">
                还没有类型库，可先在下方创建并命名。
              </div>
              <button
                type="button"
                class="primary-button"
                :disabled="personalWatchlistLibraryBusy"
                @click="savePersonalWatchlistCardLibraries"
              >保存这张卡片的归类</button>
            </section>

            <section class="personal-watchlist-library-section">
              <div class="personal-watchlist-library-section-heading">
                <div>
                  <p class="section-kicker">DEFAULT FOR NEW LINKS</p>
                  <h3>新增链接默认归类</h3>
                </div>
                <span v-if="!personalWatchlistDefaultConfigured">首次新增前必须先选择一次</span>
              </div>
              <div class="personal-watchlist-library-options default-options">
                <label>
                  <input
                    v-model="personalWatchlistDefaultSelection"
                    type="radio"
                    name="personal-watchlist-default-library"
                    :value="null"
                  />
                  <span>不自动加入任何类型库</span>
                  <small>商品仍会加入个人监控池</small>
                </label>
                <label
                  v-for="library in ownedPersonalWatchlistLibraries"
                  :key="`default-${library.id}`"
                >
                  <input
                    v-model="personalWatchlistDefaultSelection"
                    type="radio"
                    name="personal-watchlist-default-library"
                    :value="library.id"
                  />
                  <span>{{ library.name }}</span>
                  <small>{{ library.item_count }} 个商品</small>
                </label>
              </div>
              <button
                type="button"
                class="primary-button"
                :disabled="personalWatchlistLibraryBusy"
                @click="savePersonalWatchlistDefault"
              >
                {{ pendingTargetAfterLibrarySetup || pendingPersonalWatchlistPlidAfterLibrarySetup
                  ? "保存默认设置并继续加入"
                  : "保存默认设置" }}
              </button>
            </section>

            <section class="personal-watchlist-library-section">
              <div class="personal-watchlist-library-section-heading">
                <div>
                  <p class="section-kicker">CUSTOM LIBRARIES</p>
                  <h3>创建和管理类型库</h3>
                </div>
                <span>只有创建者能重命名、删除和设置分享权限</span>
              </div>
              <form
                class="personal-watchlist-library-create"
                @submit.prevent="addPersonalWatchlistLibrary"
              >
                <input
                  v-model="personalWatchlistNewLibraryName"
                  type="text"
                  maxlength="40"
                  placeholder="例如：重点跟进、医疗用品、下周复盘"
                  :disabled="personalWatchlistLibraryBusy"
                />
                <button
                  type="submit"
                  class="secondary-button"
                  :disabled="personalWatchlistLibraryBusy || !personalWatchlistNewLibraryName.trim()"
                >创建类型库</button>
              </form>
              <div
                v-if="ownedPersonalWatchlistLibraries.length"
                class="personal-watchlist-library-list"
              >
                <article
                  v-for="library in ownedPersonalWatchlistLibraries"
                  :key="library.id"
                >
                  <template v-if="personalWatchlistEditingLibraryId === library.id">
                    <input
                      v-model="personalWatchlistEditingLibraryName"
                      type="text"
                      maxlength="40"
                      :aria-label="`重命名类型库 ${library.name}`"
                    />
                    <button
                      type="button"
                      class="secondary-button"
                      :disabled="personalWatchlistLibraryBusy || !personalWatchlistEditingLibraryName.trim()"
                      @click="savePersonalWatchlistLibraryRename"
                    >保存</button>
                    <button
                      type="button"
                      class="secondary-button"
                      @click="personalWatchlistEditingLibraryId = null"
                    >取消</button>
                  </template>
                  <template v-else>
                    <div>
                      <strong>{{ library.name }}</strong>
                      <span>
                        {{ library.item_count }} 个商品 ·
                        {{ library.share_count ? `已分享 ${library.share_count} 人` : "仅自己" }}
                      </span>
                    </div>
                    <button
                      type="button"
                      class="secondary-button share-library-button"
                      @click="openPersonalWatchlistLibrarySharing(library)"
                    >分享设置</button>
                    <button
                      type="button"
                      class="secondary-button"
                      @click="beginRenamePersonalWatchlistLibrary(library)"
                    >重命名</button>
                    <button
                      type="button"
                      class="secondary-button danger"
                      :disabled="personalWatchlistLibraryBusy"
                      @click="removePersonalWatchlistLibrary(library)"
                    >删除</button>
                  </template>
                </article>
              </div>
              <div v-else class="personal-watchlist-library-empty">
                当前账号还没有类型库。
              </div>
            </section>

            <section
              v-if="sharingPersonalWatchlistLibrary"
              class="personal-watchlist-library-section personal-watchlist-sharing-section"
            >
              <div class="personal-watchlist-library-section-heading">
                <div>
                  <p class="section-kicker">SHARE PERMISSIONS</p>
                  <h3>分享“{{ sharingPersonalWatchlistLibrary.name }}”</h3>
                </div>
                <button
                  type="button"
                  class="secondary-button"
                  @click="personalWatchlistSharingLibraryId = null"
                >收起</button>
              </div>
              <div class="personal-watchlist-permission-guide">
                <span>
                  <strong>只读</strong>
                  可查看库和库内卡片，不能增删内容。
                </span>
                <span>
                  <strong>可编辑</strong>
                  可加入自己的监控池卡片，也可从库中移除卡片；不能改库名、删除库或管理分享。
                </span>
              </div>
              <label class="personal-watchlist-share-search">
                <span>查找系统用户</span>
                <input
                  v-model="personalWatchlistShareUserQuery"
                  type="search"
                  placeholder="输入姓名或账号"
                />
              </label>
              <div
                v-if="personalWatchlistShareUsersLoading"
                class="personal-watchlist-library-empty"
              >正在读取系统用户…</div>
              <div
                v-else-if="filteredPersonalWatchlistShareUsers.length"
                class="personal-watchlist-share-user-list"
              >
                <article
                  v-for="shareUser in filteredPersonalWatchlistShareUsers"
                  :key="shareUser.id"
                  :class="{
                    selected: personalWatchlistSharePermissionFor(shareUser.id),
                    inactive: !shareUser.active,
                  }"
                >
                  <label>
                    <input
                      type="checkbox"
                      :checked="Boolean(personalWatchlistSharePermissionFor(shareUser.id))"
                      @change="setPersonalWatchlistShareEnabled(shareUser.id, $event)"
                    />
                    <span>
                      <strong>{{ shareUser.display_name }}</strong>
                      <small>@{{ shareUser.username }}</small>
                    </span>
                  </label>
                  <em v-if="!shareUser.active">账号已停用</em>
                  <select
                    :value="personalWatchlistSharePermissionFor(shareUser.id) || 'read'"
                    :disabled="!personalWatchlistSharePermissionFor(shareUser.id)"
                    :aria-label="`${shareUser.display_name} 的类型库权限`"
                    @change="setPersonalWatchlistSharePermission(shareUser.id, $event)"
                  >
                    <option value="read">只读</option>
                    <option value="edit">可编辑</option>
                  </select>
                </article>
              </div>
              <div v-else class="personal-watchlist-library-empty">
                {{ personalWatchlistShareUserQuery.trim()
                  ? "没有匹配的系统用户。"
                  : "系统中暂无其他用户。" }}
              </div>
              <div class="personal-watchlist-share-actions">
                <span>已选择 {{ personalWatchlistShareDraft.length }} 人</span>
                <button
                  type="button"
                  class="primary-button"
                  :disabled="personalWatchlistLibraryBusy"
                  @click="savePersonalWatchlistLibraryShares"
                >保存分享权限</button>
              </div>
            </section>

            <section
              v-if="sharedPersonalWatchlistLibraries.length"
              class="personal-watchlist-library-section shared-with-me-section"
            >
              <div class="personal-watchlist-library-section-heading">
                <div>
                  <p class="section-kicker">SHARED WITH ME</p>
                  <h3>共享给我的类型库</h3>
                </div>
                <span>共享不会自动加入我的个人监控池</span>
              </div>
              <div class="shared-with-me-library-grid">
                <article
                  v-for="library in sharedPersonalWatchlistLibraries"
                  :key="`shared-summary-${library.id}`"
                >
                  <div>
                    <strong>{{ library.name }}</strong>
                    <span>创建者 {{ library.owner_display_name }} · @{{ library.owner_username }}</span>
                  </div>
                  <em :class="library.access">
                    {{ library.access === "edit" ? "可编辑" : "只读" }}
                  </em>
                  <small>{{ library.item_count }} 个商品</small>
                  <button
                    type="button"
                    class="secondary-button"
                    @click="
                      personalWatchlistLibraryFilter = library.id;
                      closePersonalWatchlistLibraryModal()
                    "
                  >查看此库</button>
                </article>
              </div>
            </section>
          </div>
        </section>
      </div>
    </Teleport>

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
          <h2>自有店铺与真正竞品</h2>
        </div>
        <button class="quiet-button" @click="loadOverview">刷新页面数据</button>
      </div>
      <div v-if="loading" class="empty-state">正在读取本机数据……</div>
      <div v-else-if="!allCompetitorItems.length" class="empty-state">
        <strong>还没有可展示快照</strong>
        <span>先执行一次全量刷新建立自有店铺首拉基准，再开始跟卖采集。</span>
      </div>
      <div v-else>
        <div class="competitor-source-tabs" role="tablist" aria-label="竞品数据来源">
          <button
            type="button"
            role="tab"
            :aria-selected="competitorSourceView === 'competitor'"
            aria-controls="true-competitor-panel"
            :class="{ active: competitorSourceView === 'competitor' }"
            @click="competitorSourceView = 'competitor'"
          >
            <strong>真正竞品</strong>
            <span>{{ competitors.length }} 个商品 · 默认查看</span>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="competitorSourceView === 'own_store'"
            aria-controls="own-store-follower-panel"
            :class="{ active: competitorSourceView === 'own_store' }"
            @click="competitorSourceView = 'own_store'"
          >
            <strong>自有商品跟卖</strong>
            <span>{{ storeCompetitors.length }} 个商品 · {{ ownStoreScopeLabel }}</span>
          </button>
        </div>
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
            <span>跟卖状态</span>
            <select v-model="followerPresenceFilter">
              <option value="全部">全部跟卖状态</option>
              <option value="有被跟卖">有被跟卖</option>
              <option value="未发现跟卖">未发现跟卖</option>
            </select>
          </label>
          <label
            v-if="competitorSourceView === 'competitor'"
            class="competitor-filter-field"
          >
            <span>个人监控池</span>
            <select v-model="personalWatchlistFilter">
              <option value="全部">全部竞品</option>
              <option value="我的监控池">
                只看我的监控池（{{ personalWatchlistPlids.size }}）
              </option>
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
            <span>当前信号排序</span>
            <select
              v-model="competitorListSortDirection"
              :disabled="competitorSignalFilter === '全部'"
            >
              <option value="desc">信号值降序</option>
              <option value="asc">信号值升序</option>
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
                {{ activeRangeLabel }} · 当前分区显示
                {{ activeSourceFilteredCount }} / {{ activeSourceTotalCount }}
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
          自有店铺的 Seller API 每次完整刷新都会更新当前报价并追加历史点；公开页读取全部报价，
          先按全部已接入店铺 Offer ID/SKU 排除自有报价，再探测其他卖家，因此竞争卖家抢到主报价时也不会漏掉。
          商品变体本身不算跟卖；同一卖家的不同变体会归在同一个卖家组内。评论属于整个 PLID，不归属某个卖家。
        </p>
        <section
          v-if="competitorSourceView === 'own_store'"
          id="own-store-follower-panel"
          class="competitor-source-section own-store-source-section"
          role="tabpanel"
        >
          <div class="competitor-source-heading">
            <div>
              <p class="section-kicker">OWN STORE FOLLOWERS</p>
              <h3>自有店铺链接</h3>
            </div>
            <div class="competitor-source-heading-actions">
              <span>
                {{ ownStoreScopeLabel }} · 共 {{ filteredStoreCompetitors.length }} 条 ·
                Seller API 刷新与跟卖观察分开
              </span>
            </div>
          </div>
          <section
            class="own-follower-history-query"
            :class="{ 'is-collapsed': !ownFollowerHistoryOpen }"
            aria-labelledby="own-follower-history-title"
          >
            <div class="own-follower-history-heading">
              <div>
                <p class="section-kicker">FOLLOWER DISCOVERY HISTORY</p>
                <h4 id="own-follower-history-title">六店自有链接跟卖发现记录</h4>
              </div>
              <div class="own-follower-history-heading-actions">
                <span>按北京时间自然日 · 以系统保存的公开页快照为准</span>
                <button
                  type="button"
                  class="quiet-button own-follower-history-toggle"
                  :aria-expanded="ownFollowerHistoryOpen"
                  aria-controls="own-follower-history-details"
                  @click="ownFollowerHistoryOpen = !ownFollowerHistoryOpen"
                >
                  {{ ownFollowerHistoryOpen ? "收起记录" : "展开记录" }}
                </button>
              </div>
            </div>
            <div
              v-if="ownFollowerHistoryOpen"
              id="own-follower-history-details"
              class="own-follower-history-details"
            >
              <form class="own-follower-history-form" @submit.prevent="queryOwnFollowerHistory">
                <label class="competitor-filter-field">
                  <span>开始日期</span>
                  <input
                    v-model="ownFollowerHistoryStartDate"
                    type="date"
                    :max="ownFollowerHistoryEndDate || undefined"
                  />
                </label>
                <span class="competitor-date-range-separator" aria-hidden="true">至</span>
                <label class="competitor-filter-field">
                  <span>结束日期</span>
                  <input
                    v-model="ownFollowerHistoryEndDate"
                    type="date"
                    :min="ownFollowerHistoryStartDate || undefined"
                  />
                </label>
                <button class="primary-button" type="submit" :disabled="ownFollowerHistoryLoading">
                  {{ ownFollowerHistoryLoading ? "查询中…" : "查询六店跟卖记录" }}
                </button>
              </form>
              <p v-if="ownFollowerHistoryError" class="inline-error" role="alert">
                {{ ownFollowerHistoryError }}
              </p>
              <p v-if="!ownFollowerHistoryLoaded && !ownFollowerHistoryLoading" class="method-note">
                选择日期后查询：即使某个跟卖卖家后来消失，只要区间内曾在快照中出现，仍会列出具体发现日期。
                “新增跟卖卖家”表示系统首次观察日期落在所选区间，不代表卖家实际开始跟卖的绝对时间。
              </p>
              <div v-else-if="ownFollowerHistoryLoading" class="empty-state compact-empty-state">
                正在读取六店历史快照…
              </div>
              <div
                v-else-if="ownFollowerHistoryItems.length"
                class="own-follower-history-list"
              >
                <article
                  v-for="event in ownFollowerHistoryItems"
                  :key="`own-follower-history-${event.plid}`"
                  class="own-follower-history-card"
                >
                  <div class="own-follower-history-card-heading">
                    <div>
                      <strong>{{ event.商品 }}</strong>
                      <span>PLID{{ event.plid }} · {{ event.店铺.join("、") || "自有店铺" }}</span>
                    </div>
                    <a :href="event.链接" target="_blank" rel="noreferrer">打开商品页</a>
                  </div>
                  <p>
                    区间内查到跟卖：{{ event.跟卖发现日期.join("、") }} ·
                    新增卖家 {{ event.新增跟卖卖家数 }} 个
                  </p>
                  <div class="own-follower-seller-events">
                    <div
                      v-for="seller in event.跟卖卖家明细"
                      :key="`${event.plid}-${seller.卖家ID || seller.卖家}`"
                    >
                      <strong>{{ seller.卖家 }}</strong>
                      <span>系统首次观察到：{{ seller.首次发现日期 }}</span>
                      <span>本区间查到：{{ seller.区间发现日期.join("、") }}</span>
                      <em v-if="seller.是否区间新增">本区间新增跟卖卖家</em>
                    </div>
                  </div>
                </article>
              </div>
              <div v-else class="empty-state compact-empty-state">
                所选区间没有保存到自有链接的非自有卖家报价。
              </div>
            </div>
          </section>
          <div v-if="!filteredStoreCompetitors.length" class="empty-state competitor-filter-empty">
            <strong>没有符合条件的自有店铺链接</strong>
            <span>可以调整筛选，或先执行一次完整刷新建立 Seller API 数据。</span>
          </div>
          <div v-else class="competitor-status-list">
            <article
              v-for="item in pagedStoreCompetitors"
              :key="`store-${item.plid}`"
              :id="`store-row-${item.plid}`"
              class="competitor-status-card own-store-card"
              :class="{ selected: selectedPlid === item.plid }"
              tabindex="0"
              role="button"
              aria-haspopup="dialog"
              :aria-label="`查看 ${item.商品} 及 ${followerSellerCount(item)} 个跟卖卖家`"
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
                      <span>自有 · PLID{{ item.plid }}</span>
                      <span>{{ ownStoreNames(item) }} · 更新 {{ formatChinaDateTime(item.采集时间) }}</span>
                    </div>
                    <h3>{{ item.商品 }}</h3>
                    <p>
                      {{ item.自有报价.length }} 个自有 Offer ·
                      {{ followerSellerCount(item) }} 个跟卖卖家 ·
                      {{ ownStoreVariantCount(item) }} 个自有变体
                    </p>
                  </div>
                </div>
                <span class="competitor-status-open">查看跟卖库存 →</span>
              </header>
              <div class="competitor-status-summary">
                <div>
                  <span>跟卖报价 / 自有最新价</span>
                  <strong>{{ competitorOfferPriceRange(item) }}</strong>
                  <small>自有 {{ formatCurrency(item.价格) }}</small>
                </div>
                <div>
                  <span>Seller API 最新库存</span>
                  <strong class="stock-pill" :class="{ exact: item.库存精确 }">
                    {{ item.库存上限 }}
                  </strong>
                  <small>不执行公开页主报价探测</small>
                </div>
                <div>
                  <span>跟卖状态</span>
                  <div class="signal-labels">
                    <strong
                      v-for="signal in competitorOperatingSignals(item)"
                      :key="signal"
                      class="signal-label price-signal"
                      :class="priceSignalClass(signal)"
                    >{{ signal }}</strong>
                  </div>
                  <small v-if="!competitorOperatingSignals(item).length">
                    当前区间没有保留的经营信号
                  </small>
                  <small>{{ item.区间快照数 ?? 0 }} 次跟卖观察</small>
                </div>
                <div>
                  <span>商品共享评论 / 评分</span>
                  <strong>{{ item.评论数 }} 条 · {{ item.评分 ?? "—" }}</strong>
                  <small>评论按 PLID 商品维度单独同步</small>
                </div>
              </div>
            </article>
          </div>
          <div
            v-if="filteredStoreCompetitors.length"
            class="compact-pagination competitor-pagination"
          >
            <button
              class="secondary-button"
              type="button"
              :disabled="storeCompetitorPage <= 1"
              @click="storeCompetitorPage -= 1"
            >
              上一页
            </button>
            <span>
              第 {{ storeCompetitorPage }} / {{ storeCompetitorPageCount }} 页 · 本页
              {{ pagedStoreCompetitors.length }} 条 · 共 {{ filteredStoreCompetitors.length }} 条
            </span>
            <button
              class="secondary-button"
              type="button"
              :disabled="storeCompetitorPage >= storeCompetitorPageCount"
              @click="storeCompetitorPage += 1"
            >
              下一页
            </button>
          </div>
        </section>

        <section
          v-else
          id="true-competitor-panel"
          class="competitor-source-section true-competitor-source-section"
          role="tabpanel"
        >
          <div class="competitor-source-heading">
            <div>
              <p class="section-kicker">TRUE COMPETITORS</p>
              <h3>真正竞品</h3>
            </div>
            <span>
              共 {{ filteredCompetitors.length }} 条 · 我的监控池
              {{ personalWatchlistPlids.size }} 条
            </span>
          </div>
          <div v-if="!filteredCompetitors.length" class="empty-state competitor-filter-empty">
            <strong>没有符合条件的竞品</strong>
            <span>可以调整关键词、个人监控池、库存状态或经营信号。</span>
          </div>
          <div v-else class="competitor-status-list">
          <article
            v-for="item in pagedCompetitors"
            :key="item.plid"
            :id="`competitor-row-${item.plid}`"
            v-memo="[
              item,
              selectedPlid === item.plid,
              personalWatchlistPlids.has(item.plid),
              failedCompetitorImages.has(item.图片 || ''),
            ]"
            class="competitor-status-card"
            :class="{
              selected: selectedPlid === item.plid,
            }"
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
                    <strong
                      v-if="personalWatchlistPlids.has(item.plid)"
                      class="personal-watchlist-badge"
                    >我的监控池</strong>
                  </div>
                  <h3>{{ item.商品 }}</h3>
                  <p>{{ followerSellerCount(item) }} 个卖家 · {{ item.跟卖报价.length }} 个变体 / 报价 · 主卖家 {{ item.当前卖家 || "未知" }}</p>
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
                  <strong
                    v-for="signal in competitorOperatingSignals(item)"
                    :key="signal"
                    class="signal-label price-signal"
                    :class="priceSignalClass(signal)"
                  >{{ signal }}</strong>
                </div>
                <small v-if="!competitorOperatingSignals(item).length">当前区间没有保留的经营信号</small>
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
        </section>
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
                  v-if="canShowCompetitorImage(selectedHeroImage)"
                  :src="competitorImageUrl(selectedHeroImage)"
                  :alt="`${selected.商品} ${selectedOffer?.变体 || ''} 商品图片`"
                  width="192"
                  height="192"
                  decoding="async"
                  fetchpriority="high"
                  @error="markCompetitorImageFailed(selectedHeroImage)"
                />
                <span v-else>暂无图片</span>
              </div>
              <div>
                <p class="section-kicker">
                  {{ selected.来源 === "own_store" ? "OWN STORE FOLLOWERS" : "COMPETITOR DETAIL" }}
                </p>
                <h2>{{ selected.商品 }}</h2>
                <span>
                  PLID{{ selected.plid }}
                  · 当前查看
                  {{ selectedOffer ? (selectedOffer.卖家 || "未知卖家") : (selected.当前卖家 || "未知卖家") }}
                  <template v-if="selectedOffer">
                    · SKU {{ selectedOffer.SKU || "未返回" }}
                  </template>
                </span>
                <small v-if="selectedOffer?.offer_id" class="offer-id-secondary">
                  Offer ID {{ selectedOffer.offer_id }}
                </small>
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

          <section
            class="personal-watchlist-banner"
            :class="{ 'is-member': selectedInPersonalWatchlist }"
            aria-label="个人监控池操作"
          >
            <div class="personal-watchlist-copy">
              <p class="section-kicker">PERSONAL WATCHLIST</p>
              <strong>
                {{ selectedInPersonalWatchlist ? "已在你的个人监控池" : "尚未加入你的个人监控池" }}
              </strong>
              <span>
                仅当前账号可见，用于个人筛选和类型库归类；加入或删除都不会启动、停止全局每日采集，也不会删除历史。
              </span>
              <small
                v-if="personalWatchlistError"
                class="personal-watchlist-feedback error"
                role="alert"
              >{{ personalWatchlistError }}</small>
              <small
                v-else-if="personalWatchlistNotice"
                class="personal-watchlist-feedback success"
                role="status"
              >{{ personalWatchlistNotice }}</small>
            </div>
            <button
              type="button"
              class="personal-watchlist-toggle-button"
              :class="{ 'is-remove': selectedInPersonalWatchlist }"
              :disabled="personalWatchlistBusyPlid === selected.plid"
              @click="toggleSelectedPersonalWatchlist"
            >
              {{
                personalWatchlistBusyPlid === selected.plid
                  ? "正在更新…"
                  : selectedInPersonalWatchlist
                    ? "从个人监控池删除"
                    : "加入个人监控池"
              }}
            </button>
          </section>

          <div v-if="detailLoading" class="empty-state slim">正在读取商品详情……</div>
          <p v-else-if="detailError" class="error-banner">{{ detailError }}</p>
          <template v-else>
            <div class="competitor-modal-metrics">
              <article>
                <small>当前价格</small>
                <strong>{{ formatCurrency(selectedOffer ? selectedOffer.价格 : selected.价格) }}</strong>
                <span
                  v-if="selectedOffer
                    ? offerPriceOperatingSignal(selectedOffer)
                    : ['降价', '涨价', '价格不变'].includes(selected.价格信号)"
                >
                  {{ selectedOffer ? offerPriceOperatingSignal(selectedOffer) : selected.价格信号 }}
                  <template v-if="(selectedOffer ? selectedOffer.价格变化 : selected.价格变化) !== null">
                    · {{ formatSignedCurrency(selectedOffer ? selectedOffer.价格变化 : selected.价格变化) }}
                  </template>
                </span>
              </article>
              <article>
                <small>当前卖家库存</small>
                <strong>{{ selectedOffer ? offerStockDisplay(selectedOffer) : selected.库存上限 }}</strong>
                <span v-if="selectedOffer">
                  <template v-if="offerStockOperatingSignal(selectedOffer)">
                    {{ offerStockOperatingSignal(selectedOffer) }} ·
                  </template>
                  {{ offerStockEvidenceLabel(selectedOffer) }}
                </span>
                <span v-else-if="selected.库存参考过期 && selected.上次成功库存">
                  本次未探测；上次成功 {{ selected.上次成功库存 }}
                  · {{ formatChinaDateTime(selected.上次成功库存时间) }}
                </span>
              </article>
              <article>
                <small>PLID 商品共用评论 / 评分</small>
                <strong>{{ selected.评论数 }} 条 · {{ selected.评分 ?? "—" }}</strong>
                <span>同一 PLID 的主报价与跟卖共用</span>
              </article>
              <article>
                <small>最近采集</small>
                <strong>{{ formatChinaDateTime(selected.采集时间) }}</strong>
              </article>
            </div>

            <div class="competitor-modal-content">
              <section
                v-if="selected.来源 === 'own_store'"
                class="panel competitor-offer-roster own-store-baseline-panel"
                aria-label="自有店铺 Seller API 最新刷新"
              >
                <div class="competitor-offer-roster-heading">
                  <div>
                    <p class="section-kicker">SELLER API REFRESH</p>
                    <h2>自有 Offer 最新完整刷新</h2>
                    <span>手动或定时完整刷新都会更新当前值并写入历史；不另外探测主报价库存。</span>
                  </div>
                  <span>{{ selected.自有报价.length }} 个 Offer</span>
                </div>
                <div class="competitor-offer-list">
                  <div
                    v-for="offer in selected.自有报价"
                    :key="offer.offer_id"
                    class="competitor-offer-row own-store-offer-row"
                  >
                    <div class="competitor-offer-identity">
                      <div><strong>Offer ID {{ offer.offer_id }}</strong></div>
                      <small>
                        {{ offer.店铺 }} · SKU {{ offer.SKU || "未返回" }} ·
                        {{ offer.状态 || "未返回" }}
                      </small>
                    </div>
                    <div class="competitor-offer-metric">
                      <span>最新价格</span>
                      <strong>{{ formatCurrency(offer.价格) }}</strong>
                    </div>
                    <div class="competitor-offer-metric">
                      <span>Seller API 库存</span>
                      <strong>{{ offer.库存 ?? "—" }}</strong>
                      <small>
                        Takealot可售 {{ offer.Takealot可售库存 ?? "—" }} ·
                        卖家可售 {{ offer.卖家可售库存 ?? "—" }}
                      </small>
                    </div>
                    <div class="competitor-offer-metric">
                      <span>拉取时间</span>
                      <strong>{{ formatChinaDateTime(offer.拉取时间) }}</strong>
                    </div>
                  </div>
                </div>
              </section>

              <section class="panel competitor-offer-workbench" aria-label="卖家报价连续对比台">
                <div class="competitor-offer-workbench-heading">
                  <div>
                    <p class="section-kicker">SELLER COMPARISON WORKBENCH</p>
                    <h2>全部卖家连续对比</h2>
                    <span>卖家名称去重；同一卖家的不同变体和报价归在一起，组内可继续切换具体报价。</span>
                  </div>
                  <div class="competitor-offer-workbench-controls">
                    <label>
                      <span>卖家排序</span>
                      <select v-model="offerSort">
                        <option value="net_outflow_desc">区间库存净流出优先</option>
                        <option value="price_asc">当前价格从低到高</option>
                        <option value="stock_asc">当前精确库存从低到高</option>
                        <option value="default">主报价与原始顺序</option>
                      </select>
                    </label>
                    <div class="competitor-offer-stepper">
                      <button
                        type="button"
                        class="quiet-button"
                        :disabled="selectedOfferPosition <= 0"
                        @click="selectAdjacentCompetitorOffer(-1)"
                      >
                        上一个卖家
                      </button>
                      <span>
                        {{ selectedOfferPosition >= 0 ? selectedOfferPosition + 1 : 0 }}
                        / {{ selectedSellerGroups.length }}
                      </span>
                      <button
                        type="button"
                        class="quiet-button"
                        :disabled="selectedOfferPosition < 0 || selectedOfferPosition >= selectedSellerGroups.length - 1"
                        @click="selectAdjacentCompetitorOffer(1)"
                      >
                        下一个卖家
                      </button>
                    </div>
                  </div>
                </div>
                <p class="method-note competitor-offer-sort-note">
                  当前观察区间：{{ activeRangeLabel }}。默认按可比首尾精确库存的净流出排序；
                  库存净流出是公开库存观察信号，不等于平台实际出货或订单，补货也会影响结果。
                </p>

                <div v-if="selectedSellerGroups.length" class="competitor-offer-workbench-grid">
                  <div class="competitor-offer-navigator" role="listbox" aria-label="卖家报价列表">
                    <button
                      v-for="group in selectedSellerGroups"
                      :key="group.key"
                      type="button"
                      class="competitor-offer-nav-item"
                      :class="{ selected: selectedSellerGroup?.key === group.key }"
                      role="option"
                      :aria-selected="selectedSellerGroup?.key === group.key"
                      @click="selectCompetitorOffer(group.offers[0]!)"
                    >
                      <span class="competitor-offer-nav-identity">
                        <strong>{{ group.sellerName }}</strong>
                        <small>{{ group.offers.length }} 个变体 / 报价</small>
                        <small>
                          {{ group.offers.some((offer) => offer.报价来源 === "seller_api") ? "含自有 Seller API" : "公开跟卖报价" }}
                        </small>
                      </span>
                      <span class="competitor-offer-nav-values">
                        <strong>{{ sellerGroupPriceRange(group.offers) }}</strong>
                        <small>{{ sellerGroupStockSummary(group.offers) }}</small>
                      </span>
                      <span
                        class="competitor-offer-nav-signal"
                        :class="offerStockSignalClass(group.offers[0]!.库存信号)"
                      >
                        {{ offerSort === "net_outflow_desc" ? offerNetOutflowRankLabel(group.offers[0]!) : offerIntervalMovementLabel(group.offers[0]!) }}
                      </span>
                      <span v-if="selectedSellerGroup?.key === group.key" class="competitor-offer-selected">
                        正在查看
                      </span>
                    </button>
                  </div>

                  <div class="competitor-offer-trend-pane">
                    <div class="competitor-offer-trend-heading">
                      <div>
                        <strong>{{ selectedSellerGroup?.sellerName || "未知卖家" }}</strong>
                        <span>
                          {{ selectedSellerGroupOffers.length }} 个变体 / 报价 · 当前 {{ selectedOffer?.变体 || selectedOffer?.SKU || "默认款" }}
                          · {{ selectedOfferTrend.length }} 个相关时间点
                        </span>
                      </div>
                      <span>{{ selectedOffer ? offerIntervalMovementLabel(selectedOffer) : "—" }}</span>
                    </div>

                    <div class="competitor-seller-variant-list" aria-label="当前卖家的变体和报价">
                      <button
                        v-for="offer in selectedSellerGroupOffers"
                        :key="offer.报价键"
                        type="button"
                        :class="{ selected: selectedOffer?.报价键 === offer.报价键 }"
                        @click="selectCompetitorOffer(offer)"
                      >
                        <span>
                          <strong>{{ offer.变体 || "默认款" }}</strong>
                          <small>
                            {{ offer.报价来源 === "seller_api" ? "自有 Seller API" : "公开跟卖" }}
                            · SKU {{ offer.SKU || "未返回" }}
                          </small>
                        </span>
                        <span>
                          <strong>{{ formatCurrency(offer.价格) }}</strong>
                          <small>{{ offerStockDisplay(offer) }}</small>
                        </span>
                      </button>
                    </div>

                    <div v-if="activeOfferTrendPoint" class="competitor-offer-trend-tooltip" aria-live="polite">
                      <div>
                        <small>北京时间</small>
                        <strong>{{ formatChinaDateTime(activeOfferTrendPoint.snapshot.采集时间) }}</strong>
                      </div>
                      <div>
                        <small>价格</small>
                        <strong>{{ formatCurrency(activeOfferTrendPoint.offer.价格) }}</strong>
                      </div>
                      <div>
                        <small>库存</small>
                        <strong>{{ offerStockDisplay(activeOfferTrendPoint.offer) }}</strong>
                        <span>{{ offerStockEvidenceLabel(activeOfferTrendPoint.offer) }}</span>
                      </div>
                      <div>
                        <small>评论数</small>
                        <strong>{{ activeOfferTrendPoint.reviews ?? "—" }}</strong>
                        <span>{{ activeOfferTrendPoint.reviews === null ? "该Seller API时间点未同步评论" : "PLID 商品共用" }}</span>
                      </div>
                    </div>

                    <div v-if="!selectedOfferTrend.length" class="empty-state slim">
                      当前观察区间没有可安全识别为该卖家的历史报价，不使用其他卖家快照代替。
                    </div>
                    <div
                      v-else
                      class="competitor-offer-trend-chart"
                      tabindex="0"
                      aria-label="卖家报价历史折线图，使用左右方向键切换时间点"
                      @keydown.left.prevent="stepOfferTrendPoint(-1)"
                      @keydown.right.prevent="stepOfferTrendPoint(1)"
                    >
                      <svg
                        :viewBox="`0 0 ${offerTrendChartWidth} ${offerTrendChartHeight}`"
                        role="img"
                        aria-label="价格、精确库存和商品共用评论数折线图"
                        @pointermove="handleOfferTrendPointer"
                        @pointerleave="hoveredOfferTrendIndex = null"
                      >
                        <g v-for="panel in offerTrendPanels" :key="panel.key">
                          <rect
                            class="offer-trend-panel-surface"
                            :class="`offer-trend-panel-surface-${panel.key}`"
                            x="4"
                            :y="panel.top - 12"
                            :width="offerTrendChartWidth - 8"
                            height="100"
                            rx="8"
                          />
                          <line
                            v-if="panel.key !== 'price'"
                            class="offer-trend-panel-divider"
                            x1="4"
                            :x2="offerTrendChartWidth - 4"
                            :y1="panel.top - 18"
                            :y2="panel.top - 18"
                            vector-effect="non-scaling-stroke"
                          />
                          <text class="offer-trend-panel-label" x="8" :y="panel.top + 12">
                            {{ panel.label }}
                          </text>
                          <text class="offer-trend-panel-note" x="8" :y="panel.top + 29">
                            {{ panel.note }}
                          </text>
                          <g v-for="tick in panel.ticks" :key="`${panel.key}:${tick.y}`">
                            <line
                              class="offer-trend-grid-line"
                              :x1="offerTrendPlotLeft"
                              :x2="offerTrendPlotRight"
                              :y1="tick.y"
                              :y2="tick.y"
                            />
                            <text
                              class="offer-trend-axis-label"
                              :x="offerTrendPlotLeft - 8"
                              :y="tick.y + 4"
                            >{{ tick.label }}</text>
                          </g>
                          <path
                            v-for="(segment, segmentIndex) in panel.segments"
                            :key="`${panel.key}:${segmentIndex}`"
                            class="offer-trend-line"
                            :d="segment"
                            :stroke="panel.color"
                            vector-effect="non-scaling-stroke"
                          />
                          <circle
                            v-for="point in panel.points"
                            :key="`${panel.key}:${point.index}`"
                            class="offer-trend-point"
                            :class="{ active: point.index === activeOfferTrendIndex }"
                            :cx="point.x"
                            :cy="point.y"
                            :r="point.index === activeOfferTrendIndex ? 5.5 : 3.5"
                            :fill="panel.color"
                            vector-effect="non-scaling-stroke"
                          />
                        </g>
                        <line
                          v-if="activeOfferTrendX !== null"
                          class="offer-trend-cursor"
                          :x1="activeOfferTrendX"
                          :x2="activeOfferTrendX"
                          y1="12"
                          y2="330"
                          vector-effect="non-scaling-stroke"
                        />
                        <text
                          v-for="tick in offerTrendXAxisTicks"
                          :key="`x:${tick.index}`"
                          class="offer-trend-time-label"
                          :x="tick.x"
                          y="365"
                          :text-anchor="tick.anchor"
                        >{{ tick.label }}</text>
                      </svg>
                      <p>鼠标横向移动可查看最近时间点的具体价格、库存证据和评论数；键盘可用左右方向键切换。</p>
                    </div>
                  </div>
                </div>
                <div v-else class="competitor-offer-empty">
                  <strong>当前快照未返回可区分的卖家报价</strong>
                  <span>原链接仍然保留；系统不会把变体本身猜成跟卖，也不会伪造卖家、Offer ID或库存数量。</span>
                </div>
              </section>

              <section
                v-if="props.isAdmin && selected.来源 === 'competitor'"
                class="panel competitor-target-action-card"
              >
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
                <p class="method-note">
                  监控队列按 PLID 管理；切换卖家报价只改变详情展示，不会重复加入队列。
                </p>
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

              <section
                v-else-if="props.isAdmin"
                class="panel competitor-target-action-card own-store-auto-target"
              >
                <div class="competitor-target-action-heading">
                  <div>
                    <p class="section-kicker">AUTOMATIC FOLLOWER TARGET</p>
                    <h3>自有店铺自动追踪</h3>
                  </div>
                  <span>无需加入真实竞品清单</span>
                </div>
                <p class="method-note">
                  该 PLID 来自全部已接入店铺的 Seller API 当前 Offer，每天09:00自动纳入可见共享批次；
                  公开报价先排除全部自有 Offer ID/SKU，竞争卖家即使成为主报价也继续追踪。
                  公开变体只作为商品选项展示，不直接算作跟卖；首次或评论数变化时单独读取PLID共用评论。
                  删除或修改真实竞品清单不会影响这个自动目标。
                </p>
                <div class="competitor-target-action-buttons">
                  <button
                    class="secondary-button priority"
                    type="button"
                    :disabled="
                      Boolean(targetManagerBusy)
                      || !props.canOperate
                      || !sharedBatchStatus.active
                      || sharedBatchStatus.current_plid === selected.plid
                      || prioritizedTargetStates.has(selected.plid)
                    "
                    @click="
                      prioritizeTarget(
                        { plid: selected.plid },
                        targetActionIsManualRetry,
                      )
                    "
                  >
                    {{
                      targetManagerBusy === `priority:${selected.plid}`
                        ? "插队中…"
                        : targetActionPriorityLabel(selected.plid)
                    }}
                  </button>
                </div>
              </section>

              <section class="detail-grid modal-detail-grid">
                <article v-if="selectedOffer" class="panel decision-card">
                  <p class="section-kicker">SELLER OFFER SIGNAL</p>
                  <h2>{{ selectedOffer.卖家 || "未知卖家" }}</h2>
                  <div v-if="offerOperatingSignals(selectedOffer).length" class="signal-labels">
                    <strong
                      v-for="signal in offerOperatingSignals(selectedOffer)"
                      :key="signal"
                      class="signal-label price-signal"
                      :class="priceSignalClass(signal)"
                    >{{ signal }}</strong>
                  </div>
                  <p>
                    当前只展示该报价身份的价格、库存与区间变化：
                    卖家 {{ selectedOffer.卖家 || "未知卖家" }}
                    · SKU {{ selectedOffer.SKU || "未返回" }}。
                  </p>
                  <p v-if="selectedOffer.offer_id" class="offer-id-secondary">
                    辅助身份：Offer ID {{ selectedOffer.offer_id }}
                  </p>
                  <p class="method-note">
                    实际比较：
                    {{ formatChinaDateTime(selected.信号区间开始) }}
                    至 {{ formatChinaDateTime(selected.信号区间结束) }}
                    · {{ selected.区间快照数 ?? 0 }} 个快照
                  </p>
                  <div class="decision-stats">
                    <span>
                      <small>当前库存</small>
                      <strong>{{ offerStockDisplay(selectedOffer) }}</strong>
                    </span>
                    <span>
                      <small>当前价格</small>
                      <strong>{{ formatCurrency(selectedOffer.价格) }}</strong>
                    </span>
                    <span>
                      <small>区间价格变化</small>
                      <strong>
                        {{ formatCurrency(selectedOffer.区间起始价格) }} →
                        {{ formatCurrency(selectedOffer.价格) }}
                        <template v-if="selectedOffer.价格变化 !== null">
                          （{{ formatSignedCurrency(selectedOffer.价格变化) }}）
                        </template>
                      </strong>
                    </span>
                    <span>
                      <small>区间库存变化</small>
                      <strong>
                        {{
                          selectedOffer.库存可比 && selectedOffer.库存数量变化 !== null
                            ? formatSignedQuantity(selectedOffer.库存数量变化)
                            : "不可比"
                        }}
                      </strong>
                    </span>
                    <span>
                      <small>变体</small>
                      <strong>{{ selectedOffer.变体 || "默认款" }}</strong>
                    </span>
                    <span><small>条件</small><strong>{{ selectedOffer.条件 || "未返回" }}</strong></span>
                  </div>
                  <a :href="selectedOfferLink" target="_blank" rel="noreferrer">
                    打开当前卖家报价页
                  </a>
                </article>

                <article v-else class="panel decision-card">
                  <p class="section-kicker">OPERATING SIGNAL</p>
                  <h2>{{ competitorOperatingSignals(selected).join(" · ") || "当前区间无保留信号" }}</h2>
                  <p>{{ selected.判断说明 }}</p>
                  <a :href="selected.链接" target="_blank" rel="noreferrer">
                    打开 Takealot 商品页
                  </a>
                </article>

                <article class="panel review-balance">
                  <p class="section-kicker">REVIEW BALANCE</p>
                  <h2>评论结构（同一 PLID 商品共用）</h2>
                  <p v-if="selected.共享评论说明" class="method-note">
                    {{ selected.共享评论说明 }}
                  </p>
                  <p v-else class="method-note">
                    Takealot 评论属于当前 PLID 商品，未区分卖家或 Offer ID，因此主报价与跟卖共用同一份评论结构。
                  </p>
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
                    <h2>PLID 商品变体库存（共用）</h2>
                  </div>
                  <span>{{ latestVariants.length }} 个变体 · 当前卖家库存以上方所选报价为准</span>
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

              <section class="panel reviews-panel">
                <div class="section-heading">
                  <div>
                    <p class="section-kicker">VOICE OF CUSTOMER</p>
                    <h2>公开评论（同一 PLID 商品共用）</h2>
                  </div>
                  <span class="review-result-count">
                    显示 {{ filteredReviews.length }} / {{ detail.reviews.length }} 条
                  </span>
                </div>
                <p class="method-note">
                  同一 PLID 的主报价与跟卖共用这些商品评论；平台未返回卖家或 Offer ID 归属。
                </p>
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
              <a :href="selectedOfferLink" target="_blank" rel="noreferrer">
                打开当前卖家报价页
              </a>
              <button type="button" @click="closeProductModal">关闭</button>
            </div>
          </template>
        </section>
      </div>
    </Teleport>

    <footer class="module-footer">
      价格和库存按每个卖家报价分开展示；同一 PLID 的商品评论共用。所有观察信号均需结合连续快照判断。
    </footer>
  </div>
</template>
