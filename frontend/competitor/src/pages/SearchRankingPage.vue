<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

import {
  analyzeSearchRanking,
  ApiRequestError,
  confirmSearchRankingDecisionParameters,
  confirmSearchRankingProductFacts,
  controlSearchRankingBatch,
  fetchSearchRankingRootExpansionLibrary,
  fetchSearchRankingBatchPreview,
  fetchSearchRankingBatchStatus,
  fetchSearchRankingDetail,
  fetchSearchRankingProducts,
  revokeSearchRankingProductFact,
  restartSearchRankingBatch,
  startSearchRankingBatch,
} from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { matchesProductSearch } from "../productSearch";
import { groupSearchRankingProducts } from "../searchRankingFamilies";
import {
  rootExpansionCheckIsPhrase,
  rootExpansionCheckLabel,
} from "../searchRankingRootExpansion";
import { formatChinaDateTime } from "../time";
import type {
  OwnStoreScope,
  SearchRankingDetailPayload,
  SearchRankingBatchPreviewPayload,
  SearchRankingBatchStatusValue,
  SearchRankingDecisionParameterType,
  SearchRankingKeywordResult,
  SearchRankingListPayload,
  SearchRankingProduct,
  SearchRankingDecisionParameterProfile,
  SearchRankingProductFactRecord,
  SearchRankingProductFactType,
  SearchRankingRootSource,
  SearchRankingTitleStrategy,
  SearchRankingTitleStrategyKey,
  SearchRootExpansionLibraryPayload,
} from "../types";

const props = defineProps<{
  canOperate: boolean;
  storeScope?: OwnStoreScope;
  multiStoreLabel?: string;
  onPermissionDenied?: (message: string) => void;
}>();

const listPayload = ref<SearchRankingListPayload | null>(null);
const detail = ref<SearchRankingDetailPayload | null>(null);
const selectedOfferId = ref("");
const selectedStoreCode = ref("");
const search = ref("");
const identityDifferenceFilter = ref<"all" | "high" | "moderate" | "aligned" | "manual" | "unanalysed">("all");
const titleScoreFilter = ref<"all" | "85_plus" | "70_84" | "55_69" | "below_55" | "insufficient" | "unscored">("all");
const loadingList = ref(false);
const loadingDetail = ref(false);
const analyzing = ref(false);
const factConfirmationOpen = ref(false);
const factSaving = ref(false);
const factModalError = ref("");
const decisionParameterSaving = ref(false);
const decisionParameterChoices = ref<Record<string, boolean | null>>({});
const factRevocationTarget = ref<SearchRankingProductFactRecord | null>(null);
const factRevocationReason = ref("");
const rootExpansionLibrary = ref<SearchRootExpansionLibraryPayload | null>(null);
const rootExpansionLibrarySearch = ref("");
const rootExpansionLibraryLoading = ref(false);
const batchPreviewPayload = ref<SearchRankingBatchPreviewPayload | null>(null);
const batchLoading = ref(false);
const batchAction = ref<"start" | "pause" | "resume" | "restart" | "stop" | "">("");
const factDrafts = ref<Array<{
  fact_type: SearchRankingProductFactType;
  fact_term: string;
  statement: string;
}>>([]);
const error = ref("");
const failedImages = ref(new Set<string>());
const rankingDetailElement = ref<HTMLElement | null>(null);
const rankingDetailMinimumHeight = ref(0);
let batchPollTimer: ReturnType<typeof setTimeout> | null = null;
let detailRequestSequence = 0;
let productListRequestSequence = 0;

const products = computed(() => listPayload.value?.items ?? []);
const productFamilies = computed(() => groupSearchRankingProducts(products.value));
const eligibility = computed(() => listPayload.value?.eligibility ?? null);
const filteredProductFamilies = computed(() => {
  return productFamilies.value.filter((family) => {
    const textMatches = family.variants.some((item) => matchesProductSearch(
      {
        productNames: [item.title, item.company_product_name],
        otherValues: [
          item.sku,
          item.company_sku,
          item.offer_id,
          item.productline_id,
          item.store_name,
          item.store_code,
        ],
      },
      search.value,
    ));
    if (!textMatches) return false;
    const latest = family.latest_analysis;
    const differenceMatches = identityDifferenceFilter.value === "all"
      || (identityDifferenceFilter.value === "manual" && latest?.manual_fact_required)
      || (identityDifferenceFilter.value === "unanalysed" && !latest)
      || latest?.identity_difference_level === identityDifferenceFilter.value;
    if (!differenceMatches) return false;
    const score = latest?.title_score_value;
    const scoreMatches = titleScoreFilter.value === "all"
      || (titleScoreFilter.value === "unscored" && (score === null || score === undefined))
      || (titleScoreFilter.value === "insufficient" && latest?.title_score_band === "insufficient_evidence")
      || (titleScoreFilter.value === "85_plus" && score !== null && score !== undefined && score >= 85)
      || (titleScoreFilter.value === "70_84" && score !== null && score !== undefined && score >= 70 && score < 85)
      || (titleScoreFilter.value === "55_69" && score !== null && score !== undefined && score >= 55 && score < 70)
      || (titleScoreFilter.value === "below_55" && score !== null && score !== undefined && score < 55);
    return scoreMatches;
  });
});
const selectedProduct = computed(() => detail.value?.product ?? null);
const selectedFamily = computed(() => productFamilies.value.find((family) =>
  family.variants.some(
    (item) =>
      item.offer_id === selectedOfferId.value
      && String(item.store_code ?? "") === selectedStoreCode.value,
  ),
) ?? null);
const analysis = computed(() => detail.value?.analysis ?? null);
const batchPreview = computed(() => batchPreviewPayload.value?.preview ?? null);
const batchState = computed(() => batchPreviewPayload.value?.batch ?? null);
const activeBatchStatuses = new Set<SearchRankingBatchStatusValue>([
  "queued",
  "running",
  "pausing",
  "stopping",
]);
const batchIsActive = computed(() => Boolean(
  batchState.value?.status && activeBatchStatuses.has(batchState.value.status),
));
const batchProgressPercent = computed(() => {
  const total = batchState.value?.target_count ?? 0;
  const processed = batchState.value?.processed_count ?? 0;
  return total > 0 ? Math.min(100, Math.round(processed / total * 100)) : 0;
});
const factRecommendation = computed(() => analysis.value?.product_fact_recommendation ?? null);
const productFactProfile = computed(() => detail.value?.product_fact_profile ?? null);
const decisionParameterProfile = computed(() => detail.value?.decision_parameter_profile ?? null);
const decisionParametersFullyClassified = computed(() => {
  const candidates = decisionParameterProfile.value?.candidates ?? [];
  return candidates.every((item) => decisionParameterChoices.value[item.parameter_key] != null);
});
const manualFactCanBeConfirmed = computed(() => Boolean(
  factRecommendation.value
    && detail.value?.status.configured
    && detail.value?.status.product_fact_manual_confirmation_available,
));
const currentTitleChangedSinceAnalysis = computed(() => {
  if (analysis.value?.variant_projection?.title_review_available) return false;
  const currentTitle = String(selectedProduct.value?.title ?? "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  const analyzedTitle = String(analysis.value?.source_title ?? "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  return Boolean(currentTitle && analyzedTitle && currentTitle !== analyzedTitle);
});
const acceptedKeywords = computed(() =>
  analysis.value?.keywords.filter((item) => item.relevance_status === "accepted") ?? [],
);
const blueOceanKeywords = computed(() =>
  analysis.value?.keywords.filter((item) => item.relevance_status === "opportunity") ?? [],
);
const semanticGradeCounts = computed(() => {
  const counts = { S: 0, A: 0, "C/I": 0 };
  for (const item of analysis.value?.keywords ?? []) {
    const grade = item.validation_evidence.semantic_relation_grade;
    if (grade) counts[grade] += 1;
  }
  return counts;
});
const comparisonKeywords = computed(() =>
  analysis.value?.keywords.filter((item) =>
    item.validation_evidence.comparison_role != null
    || item.validation_evidence.comparison_baseline_rank != null,
  ) ?? [],
);
const rejectedKeywords = computed(() =>
  analysis.value?.keywords.filter((item) =>
    ["rejected_irrelevant", "model_low_confidence"].includes(item.relevance_status),
  ) ?? [],
);
const contextualRootExpansionChecks = computed(() =>
  analysis.value?.root_expansion_checks ?? [],
);
const contextualRootExpansionSummary = computed(() => {
  let eligible = 0;
  let rejected = 0;
  let phraseRoots = 0;
  let followupRoots = 0;
  for (const check of contextualRootExpansionChecks.value) {
    if (rootExpansionCheckIsPhrase(check)) phraseRoots += 1;
    if ((check.journey_depth ?? 0) > 0) followupRoots += 1;
    for (const expansion of check.expansions ?? []) {
      if (expansion.relevance_status === "eligible") eligible += 1;
      if (expansion.relevance_status === "rejected_irrelevant") rejected += 1;
    }
  }
  return { eligible, rejected, phraseRoots, followupRoots };
});
type QuerySourceChannel =
  | "takealot_root_expansion"
  | "takealot_autocomplete_path"
  | "model_south_african_direct"
  | "comparison_resample"
  | "title_verified_parameter"
  | "human_confirmed_decision_parameter";

function hasQuerySourceChannel(item: SearchRankingKeywordResult, channel: QuerySourceChannel) {
  const channels = item.validation_evidence.query_source_channels ?? [];
  return item.validation_evidence.query_source_channel === channel || channels.includes(channel);
}

const platformExpansionKeywords = computed(() =>
  analysis.value?.keywords.filter((item) =>
    hasQuerySourceChannel(item, "takealot_root_expansion")
      || hasQuerySourceChannel(item, "takealot_autocomplete_path"),
  ) ?? [],
);
const modelDirectKeywords = computed(() =>
  analysis.value?.keywords.filter((item) =>
    hasQuerySourceChannel(item, "model_south_african_direct"),
  ) ?? [],
);
const titleParameterKeywords = computed(() =>
  analysis.value?.keywords.filter((item) =>
    hasQuerySourceChannel(item, "title_verified_parameter")
      || hasQuerySourceChannel(item, "human_confirmed_decision_parameter"),
  ) ?? [],
);
const platformSearchCollected = computed(() =>
  analysis.value?.keywords.some((item) => item.pages_scanned > 0) ?? false,
);
const providerFallbackSucceeded = computed(() => {
  const attempts = analysis.value?.provider_attempts ?? [];
  return platformSearchCollected.value
    && attempts.some((item) => item.status === "accepted")
    && attempts.some((item) => item.status !== "accepted");
});
const latestAttemptHasUnusableSpend = computed(() => {
  const attempt = detail.value?.latest_attempt;
  if (!attempt || attempt.vision_stage_completed) return false;
  return (attempt.usage?.total_tokens ?? 0) > 0
    || (attempt.estimated_cost_cny !== null && attempt.estimated_cost_cny !== undefined);
});
const titleStrategies = computed<SearchRankingTitleStrategy[]>(() => {
  const current = analysis.value;
  if (!current) return [];
  const hasStrategyContract = current.title_strategies !== undefined
    || current.profile.title_strategies !== undefined;
  const suppliedStrategies = current.title_strategies ?? current.profile.title_strategies ?? [];

  const defaults: Record<SearchRankingTitleStrategyKey, Omit<SearchRankingTitleStrategy, "strategy">> = {
    contiguous_core: {
      label: "完整连续词组版",
      title: hasStrategyContract ? null : current.title_suggestion,
      available: !hasStrategyContract && Boolean(current.title_suggestion),
      explanation: hasStrategyContract
        ? "本轮没有按当前核心词门槛形成可用的连续词组方案。"
        : current.title_reason
          || "保留已验证核心短语的完整连续性并前置；商品类型在前、功能卖点居中，明确规格参数放在末尾。",
      evidence_keywords: acceptedKeywords.value.map((item) => item.keyword),
    },
    hot_term_coverage: {
      label: "类目热词覆盖版",
      title: null,
      available: false,
      explanation: hasStrategyContract
        ? "本轮没有形成与连续词组版足够不同且证据完整的热词覆盖方案。"
        : "该历史记录尚未生成独立的类目热词覆盖方案；重新分析后才会按平台根词扩展证据评估。",
      evidence_keywords: acceptedKeywords.value
        .filter((item) => item.validation_evidence.autocomplete_rank != null)
        .map((item) => item.keyword),
    },
    adjacent_opportunity: {
      label: "S/A蓝海命名版",
      title: null,
      available: false,
      explanation: hasStrategyContract
        ? "本轮没有同时通过S/A语义关系、平台根词扩展、实际命中与首页低竞争门槛的方案。"
        : "旧记录没有现行S/A语义关系证据，已安全停用；重新分析后再评估。",
      evidence_keywords: [],
    },
  };
  const order: SearchRankingTitleStrategyKey[] = [
    "contiguous_core",
    "hot_term_coverage",
    "adjacent_opportunity",
  ];

  return order.map((strategy) => {
    const supplied = suppliedStrategies?.find((item) => item.strategy === strategy);
    const fallback = defaults[strategy];
    if (!supplied) return { strategy, ...fallback };
    return {
      ...fallback,
      ...supplied,
      strategy,
      title: supplied.title?.trim() || null,
      available: supplied.available && Boolean(supplied.title?.trim()),
      evidence_keywords: [
        ...new Set((supplied.evidence_keywords ?? fallback.evidence_keywords).filter(Boolean)),
      ],
    };
  });
});

onMounted(() => {
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  void loadProducts();
  void loadRootExpansionLibrary();
  void loadBatchPreview();
});

watch(() => props.storeScope, () => {
  void loadProducts();
});

onUnmounted(() => {
  if (batchPollTimer) clearTimeout(batchPollTimer);
});

async function loadProducts(preferredOfferId = "") {
  const requestSequence = ++productListRequestSequence;
  const requestedStoreScope = props.storeScope ?? "current";
  detailRequestSequence += 1;
  loadingDetail.value = false;
  loadingList.value = true;
  error.value = "";
  try {
    const payload = await fetchSearchRankingProducts(requestedStoreScope);
    if (
      requestSequence !== productListRequestSequence
      || requestedStoreScope !== (props.storeScope ?? "current")
    ) return;
    listPayload.value = payload;
    const preferredFamily = productFamilies.value.find((family) =>
      family.variants.some(
        (item) =>
          item.offer_id === preferredOfferId
          && (
            !selectedStoreCode.value
            || String(item.store_code ?? "") === selectedStoreCode.value
          ),
      ),
    );
    const nextFamily = preferredFamily
      ?? productFamilies.value.find((family) => family.latest_analysis?.status === "completed")
      ?? productFamilies.value.find((family) => family.representative.analyzable)
      ?? productFamilies.value[0];
    const next = nextFamily?.representative;
    if (next) await selectProduct(next);
    else {
      selectedOfferId.value = "";
      selectedStoreCode.value = "";
      detail.value = null;
    }
  } catch (caught) {
    if (requestSequence !== productListRequestSequence) return;
    error.value = errorMessage(caught, "搜索定位商品列表加载失败");
  } finally {
    if (requestSequence === productListRequestSequence) loadingList.value = false;
  }
}

async function loadBatchPreview() {
  batchLoading.value = true;
  try {
    batchPreviewPayload.value = await fetchSearchRankingBatchPreview();
    scheduleBatchPoll();
  } catch (caught) {
    error.value = errorMessage(caught, "多店串行分析预览加载失败");
  } finally {
    batchLoading.value = false;
  }
}

function scheduleBatchPoll() {
  if (batchPollTimer) clearTimeout(batchPollTimer);
  batchPollTimer = null;
  if (!batchIsActive.value) return;
  batchPollTimer = setTimeout(() => void refreshBatchStatus(), 2_500);
}

async function refreshBatchStatus() {
  const wasActive = batchIsActive.value;
  try {
    const payload = await fetchSearchRankingBatchStatus();
    if (batchPreviewPayload.value) batchPreviewPayload.value.batch = payload.batch;
    if (wasActive && !batchIsActive.value) {
      await loadProducts(selectedOfferId.value);
      await loadBatchPreview();
      return;
    }
  } catch (caught) {
    error.value = errorMessage(caught, "串行批次状态读取失败");
  }
  scheduleBatchPoll();
}

async function startFullBatch() {
  const preview = batchPreview.value;
  if (!preview) return;
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号可以查看批次预估，但不能调用模型或采集排名");
    return;
  }
  const cost = preview.estimated_cost;
  const duration = preview.estimated_duration;
  const policy = batchPreviewPayload.value?.policy;
  const accepted = window.confirm([
    `确认串行分析 ${preview.store_count} 个授权店铺的 ${preview.eligible_count} 个商品族（由 ${preview.eligible_offer_count} 条有效 Offer 按同店同 PLID 合并）？`,
    `预计 ${preview.fresh_vision_count} 个商品族需新双阶段模型分析（隔离识图 + 图文融合），约 ${formatWholeNumber(preview.estimated_usage.total_tokens)} Token。`,
    `常见费用约 ¥${cost.typical_low_cny.toFixed(2)}–¥${cost.typical_high_cny.toFixed(2)}，保守上界约 ¥${cost.conservative_upper_cny.toFixed(2)}。`,
    `预计用时约 ${duration.likely_min_hours}–${duration.likely_max_hours} 小时。公开请求全程单并发、每次间隔 ${policy?.public_request_min_interval_seconds ?? 3}–${policy?.public_request_max_interval_seconds ?? 5} 秒；不倒搜、不自动重试，错误后暂停。`,
  ].join("\n\n"));
  if (!accepted) return;
  batchAction.value = "start";
  error.value = "";
  try {
    const payload = await startSearchRankingBatch(preview.snapshot_id);
    if (batchPreviewPayload.value) batchPreviewPayload.value.batch = payload.batch;
    scheduleBatchPoll();
  } catch (caught) {
    error.value = errorMessage(caught, "串行批次未启动");
    await loadBatchPreview();
  } finally {
    batchAction.value = "";
  }
}

async function runBatchControl(action: "pause" | "resume" | "stop") {
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号不能控制搜索定位批次");
    return;
  }
  if (action === "stop" && !window.confirm("确认保存进度并退出运行？当前商品族会先安全结束，之后可从断点继续，已处理商品族不会重跑。")) {
    return;
  }
  batchAction.value = action;
  error.value = "";
  try {
    const payload = await controlSearchRankingBatch(action);
    if (batchPreviewPayload.value) batchPreviewPayload.value.batch = payload.batch;
    scheduleBatchPoll();
  } catch (caught) {
    error.value = errorMessage(caught, "批次控制失败");
  } finally {
    batchAction.value = "";
  }
}

async function restartFullBatch() {
  const preview = batchPreview.value;
  if (!preview) return;
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号不能重新开始搜索定位批次");
    return;
  }
  const accepted = window.confirm([
    `确认丢弃旧批次的剩余进度，并从第 1 个商品族重新开始 ${preview.store_count} 店 ${preview.eligible_count} 个商品族？`,
    `预计约 ${formatWholeNumber(preview.estimated_usage.total_tokens)} Token，常见费用 ¥${preview.estimated_cost.typical_low_cny.toFixed(2)}–¥${preview.estimated_cost.typical_high_cny.toFixed(2)}。`,
    "已完成商品族也会重新分析；系统会再次核对快照，仍保持单并发且不自动重试。",
  ].join("\n\n"));
  if (!accepted) return;
  batchAction.value = "restart";
  error.value = "";
  try {
    const payload = await restartSearchRankingBatch(preview.snapshot_id);
    if (batchPreviewPayload.value) batchPreviewPayload.value.batch = payload.batch;
    scheduleBatchPoll();
  } catch (caught) {
    error.value = errorMessage(caught, "批次未能从头重新开始");
    await loadBatchPreview();
  } finally {
    batchAction.value = "";
  }
}

function syncDecisionParameterChoices(profile: SearchRankingDecisionParameterProfile | null) {
  decisionParameterChoices.value = Object.fromEntries(
    (profile?.candidates ?? []).map((item) => [item.parameter_key, item.manual_decision]),
  );
}

async function selectProduct(
  productOrOfferId: SearchRankingProduct | string,
  requestedStoreCode = selectedStoreCode.value,
) {
  const offerId = typeof productOrOfferId === "string"
    ? productOrOfferId
    : productOrOfferId.offer_id;
  const storeCode = typeof productOrOfferId === "string"
    ? requestedStoreCode
    : String(productOrOfferId.store_code ?? "");
  const requestSequence = ++detailRequestSequence;
  rankingDetailMinimumHeight.value = Math.max(
    rankingDetailMinimumHeight.value,
    Math.ceil(rankingDetailElement.value?.getBoundingClientRect().height ?? 0),
  );
  factConfirmationOpen.value = false;
  factRevocationTarget.value = null;
  selectedOfferId.value = offerId;
  selectedStoreCode.value = storeCode;
  loadingDetail.value = true;
  error.value = "";
  try {
    const payload = await fetchSearchRankingDetail(offerId, storeCode);
    if (
      selectedOfferId.value === offerId
      && selectedStoreCode.value === storeCode
    ) {
      detail.value = payload;
      syncDecisionParameterChoices(payload.decision_parameter_profile);
    }
  } catch (caught) {
    if (requestSequence === detailRequestSequence) {
      error.value = errorMessage(caught, "搜索定位详情加载失败");
    }
  } finally {
    if (requestSequence === detailRequestSequence) {
      loadingDetail.value = false;
      await nextTick();
      rankingDetailMinimumHeight.value = 0;
    }
  }
}

async function runAnalysis() {
  const product = selectedProduct.value;
  if (!product) return;
  const familyRepresentative = selectedFamily.value?.representative ?? product;
  const selectedBeforeAnalysis = product.offer_id;
  const selectedBeforeStoreCode = String(product.store_code ?? selectedStoreCode.value);
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号可以查看搜索定位，但不能调用模型或采集排名");
    return;
  }
  if (!detail.value?.status.configured) {
    error.value = "服务端尚未配置 DASHSCOPE_API_KEY 或 ARK_API_KEY，未发起任何模型调用";
    return;
  }
  analyzing.value = true;
  error.value = "";
  try {
    const analyzedDetail = await analyzeSearchRanking(
      familyRepresentative.offer_id,
      familyRepresentative.store_code,
    );
    detail.value = selectedBeforeAnalysis === familyRepresentative.offer_id
      ? analyzedDetail
      : await fetchSearchRankingDetail(selectedBeforeAnalysis, selectedBeforeStoreCode);
    syncDecisionParameterChoices(detail.value.decision_parameter_profile);
    await refreshListSummary(selectedBeforeAnalysis, selectedBeforeStoreCode);
    await loadRootExpansionLibrary();
  } catch (caught) {
    error.value = errorMessage(caught, "识别或排名采集失败");
    await selectProduct(selectedBeforeAnalysis, selectedBeforeStoreCode);
  } finally {
    analyzing.value = false;
  }
}

async function saveDecisionParameterConfirmation() {
  const product = selectedProduct.value;
  const profile = decisionParameterProfile.value;
  if (!product || !profile) return;
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号可以查看决策参数，但不能人工确认");
    return;
  }
  if (!decisionParametersFullyClassified.value) {
    error.value = "请先对当前标题中的每一项参数选择‘是’或‘不是’决策参数";
    return;
  }
  const choices = profile.candidates.map((item) => ({
    parameter_key: item.parameter_key,
    is_decision_parameter: decisionParameterChoices.value[item.parameter_key] === true,
  }));
  const positiveCount = choices.filter((item) => item.is_decision_parameter).length;
  if (positiveCount > profile.max_positive_decisions) {
    error.value = `一个标题最多确认 ${profile.max_positive_decisions} 项决策参数，避免关键词前置失去主次`;
    return;
  }
  const message = profile.candidates.length
    ? `确认保存当前标题的 ${profile.candidates.length} 项参数分类？保存后需要重新验证定位，只有通过同商品族搜索页验证的决策参数才会前置。`
    : "确认当前标题没有系统可识别的规格参数？标题变化后需要重新确认。";
  if (!window.confirm(message)) return;
  decisionParameterSaving.value = true;
  error.value = "";
  try {
    detail.value = await confirmSearchRankingDecisionParameters(
      product.offer_id,
      choices,
      product.store_code,
    );
    syncDecisionParameterChoices(detail.value.decision_parameter_profile);
  } catch (caught) {
    error.value = errorMessage(caught, "决策参数确认保存失败");
    await selectProduct(product.offer_id, String(product.store_code ?? ""));
  } finally {
    decisionParameterSaving.value = false;
  }
}

async function loadRootExpansionLibrary() {
  rootExpansionLibraryLoading.value = true;
  try {
    rootExpansionLibrary.value = await fetchSearchRankingRootExpansionLibrary(
      rootExpansionLibrarySearch.value,
      selectedStoreCode.value,
    );
  } catch (caught) {
    error.value = errorMessage(caught, "平台根词扩展库加载失败");
  } finally {
    rootExpansionLibraryLoading.value = false;
  }
}

function openProductFactConfirmation() {
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号可以查看商品事实档案，但不能人工确认商品事实");
    return;
  }
  if (!manualFactCanBeConfirmed.value) {
    error.value = "当前分析暂不能用于人工确认，请刷新或重新分析后再试";
    return;
  }
  error.value = "";
  factModalError.value = "";
  factDrafts.value = [{ fact_type: "product_type", fact_term: "", statement: "" }];
  factConfirmationOpen.value = true;
}

function closeProductFactConfirmation() {
  if (factSaving.value) return;
  factModalError.value = "";
  factConfirmationOpen.value = false;
}

function addFactDraft() {
  if (factDrafts.value.length >= 6) return;
  factDrafts.value.push({ fact_type: "product_type", fact_term: "", statement: "" });
}

function removeFactDraft(index: number) {
  if (factDrafts.value.length === 1) return;
  factDrafts.value.splice(index, 1);
}

async function confirmProductFacts() {
  const product = selectedProduct.value;
  const sourceAnalysis = analysis.value;
  const recommendation = factRecommendation.value;
  if (!product || !sourceAnalysis || !recommendation) {
    factModalError.value = "当前商品或分析已经变化，请关闭弹窗、刷新后重新确认";
    return;
  }
  const facts = factDrafts.value.map((item) => ({
    fact_type: item.fact_type,
    fact_term: item.fact_term.replace(/\s+/g, " ").trim(),
    statement: item.statement.replace(/\s+/g, " ").trim(),
  }));
  const invalid = facts.find((item) =>
    !/^[A-Za-z0-9]+(?: [A-Za-z0-9]+){0,5}$/.test(item.fact_term),
  );
  if (invalid) {
    factModalError.value = "事实词请填写2到100字符的英文或数字短语，最多6个词且不要标点";
    return;
  }
  factSaving.value = true;
  factModalError.value = "";
  error.value = "";
  try {
    detail.value = await confirmSearchRankingProductFacts(product.offer_id, {
      source_analysis_id: sourceAnalysis.id,
      reason_code: recommendation.reason_code,
      facts,
      confirmed: true,
      acknowledged_fact_accuracy: true,
      acknowledged_ranking_revalidation: true,
    }, product.store_code);
    syncDecisionParameterChoices(detail.value.decision_parameter_profile);
    factConfirmationOpen.value = false;
    factDrafts.value = [];
    await refreshListSummary(product.offer_id, String(product.store_code ?? ""));
    await loadRootExpansionLibrary();
  } catch (caught) {
    factModalError.value = errorMessage(caught, "商品事实保存或后续定位失败");
    try {
      const refreshed = await fetchSearchRankingDetail(
        product.offer_id,
        product.store_code,
      );
      detail.value = refreshed;
      syncDecisionParameterChoices(refreshed.decision_parameter_profile);
    } catch {
      // Keep the actionable request error in the modal even if refresh also fails.
    }
  } finally {
    factSaving.value = false;
  }
}

function openFactRevocation(fact: SearchRankingProductFactRecord) {
  if (!props.canOperate) {
    props.onPermissionDenied?.("当前账号不能停用商品事实");
    return;
  }
  factRevocationTarget.value = fact;
  factRevocationReason.value = "";
}

async function confirmFactRevocation() {
  const product = selectedProduct.value;
  const fact = factRevocationTarget.value;
  const reason = factRevocationReason.value.replace(/\s+/g, " ").trim();
  if (!product || !fact) return;
  if (reason.length < 2) {
    error.value = "请填写至少2个字符的停用原因";
    return;
  }
  factSaving.value = true;
  error.value = "";
  try {
    detail.value = await revokeSearchRankingProductFact(
      product.offer_id,
      fact.id,
      reason,
      product.store_code,
    );
    factRevocationTarget.value = null;
    factRevocationReason.value = "";
  } catch (caught) {
    error.value = errorMessage(caught, "商品事实停用失败");
  } finally {
    factSaving.value = false;
  }
}

function productFactTypeLabel(type: SearchRankingProductFactType) {
  const labels: Record<SearchRankingProductFactType, string> = {
    product_type: "商品类型",
    construction: "结构形态",
    material: "材质",
    function: "功能",
    packaging: "包装形态",
    usage: "使用场景",
  };
  return labels[type];
}

function productFactSourceLabel(_source: SearchRankingProductFactRecord["source_type"]) {
  return "运营人工确认";
}

function decisionParameterTypeLabel(type: SearchRankingDecisionParameterType) {
  const labels: Record<SearchRankingDecisionParameterType, string> = {
    power: "功率",
    voltage: "电压",
    current: "电流",
    capacity: "容量",
    size: "尺寸",
    dimensions: "长宽尺寸",
    weight: "重量",
    quantity: "数量或套装",
    resolution: "分辨率",
    protection_rating: "防护等级",
    specification: "其他规格",
  };
  return labels[type];
}

function variantParametersFor(offerId: string) {
  return selectedFamily.value?.variant_parameters_by_offer[offerId] ?? [];
}

function familyCompanySkuLabel(variants: SearchRankingProduct[]) {
  const values = [...new Set(
    variants.map((item) => String(item.company_sku ?? "").trim()).filter(Boolean),
  )];
  return values.length ? values.join("、") : "未关联";
}

function variantParameterSummary(values: string[]) {
  if (!values.length) return "无标题差异参数";
  const visible = values.slice(0, 4);
  return `${visible.join(" / ")}${values.length > visible.length ? ` / +${values.length - visible.length}` : ""}`;
}

function batchVariantParameterSummary(target: {
  variant_parameters?: Array<{ parameters: Array<{ value: string }> }>;
}) {
  return variantParameterSummary([
    ...new Set((target.variant_parameters ?? []).flatMap((item) =>
      item.parameters.map((parameter) => parameter.value),
    )),
  ]);
}

async function refreshListSummary(offerId: string, storeCode = selectedStoreCode.value) {
  try {
    listPayload.value = await fetchSearchRankingProducts(props.storeScope ?? "current");
    selectedOfferId.value = offerId;
    selectedStoreCode.value = storeCode;
  } catch {
    // The saved detail remains usable even if the lightweight list refresh fails.
  }
}

function imageUrl(product: SearchRankingProduct | null) {
  const source = product?.image_url?.trim() ?? "";
  if (!source || failedImages.value.has(source)) return "";
  return productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list);
}

function markImageFailed(source: string | null) {
  if (!source) return;
  failedImages.value = new Set([...failedImages.value, source]);
}

function resultLabel(item: SearchRankingKeywordResult) {
  if (item.relevance_status === "comparison_resample" && !item.found) return "本轮同词复采未找到";
  if (item.relevance_status === "rejected_irrelevant") {
    return item.validation_evidence.semantic_relation_grade === "C/I"
      ? "C/I级：互补品或无关结果 未采用"
      : "平台相关性未通过";
  }
  if (item.relevance_status === "model_low_confidence") return "图片识别置信度不足";
  if (!item.found) return `前 ${item.pages_scanned} 页未找到`;
  return `第 ${item.page_number} 页 · 第 ${item.row_number} 行第 ${item.column_number} 列`;
}

function scanScopeLabel(item: SearchRankingKeywordResult) {
  if (item.pages_scanned <= 0) return "未进入扫描范围";
  if (item.relevance_status === "rejected_irrelevant" && item.pages_scanned === 1) {
    return "仅完成首页筛选 未进入后续定位";
  }
  return `已扫描 ${item.pages_scanned} 页 未找到商品`;
}

function locatedButNotAdopted(item: SearchRankingKeywordResult) {
  return item.found
    && ["rejected_irrelevant", "model_low_confidence"].includes(item.relevance_status);
}

function hasFirstPageValidation(item: SearchRankingKeywordResult) {
  if (item.validation_evidence.page_validation_status === "not_run") return false;
  return item.validation_evidence.page_validation_status === "completed"
    || item.pages_scanned > 0;
}

function sameTypeRatioLabel(item: SearchRankingKeywordResult) {
  if (!hasFirstPageValidation(item)) return "未验证";
  const evaluated = item.validation_evidence.evaluated_first_page_results
    ?? item.validation_evidence.evaluated_top_results
    ?? 0;
  return evaluated > 0 ? percent(item.relevance_score) : "无自然商品";
}

function strategyLabel(item: SearchRankingKeywordResult) {
  const grade = item.validation_evidence.semantic_relation_grade;
  if (item.relevance_status === "accepted") return `${grade ?? "S"}级核心词`;
  if (item.relevance_status === "opportunity") return `${grade ?? "S/A"}级蓝海`;
  if (item.relevance_status === "comparison_resample") return "改后同词复采";
  return grade ? `${grade}级未采用` : "未采用";
}

function semanticGradeLabel(item: SearchRankingKeywordResult) {
  const grade = item.validation_evidence.semantic_relation_grade;
  if (grade === "S") return "S · 同一商品/直接别名";
  if (grade === "A") return "A · 同一任务的替代商品";
  if (grade === "C/I") return "C/I · 互补品或无关结果";
  return "未判级";
}

function semanticResultCountLabel(item: SearchRankingKeywordResult) {
  const evidence = item.validation_evidence;
  const evaluated = evidence.semantic_relation_evaluated_result_count ?? 0;
  if (!evaluated) return "未验证";
  return [
    `S ${evidence.semantic_relation_same_product_result_count ?? 0}`,
    `A ${evidence.semantic_relation_adjacent_result_count ?? 0}`,
    `C/I ${evidence.semantic_relation_rejected_result_count ?? 0}`,
  ].join(" / ");
}

function sourceChannelLabel(item: SearchRankingKeywordResult) {
  const hasPlatformExpansion = hasQuerySourceChannel(item, "takealot_root_expansion")
    || hasQuerySourceChannel(item, "takealot_autocomplete_path");
  const hasModelDirect = hasQuerySourceChannel(item, "model_south_african_direct");
  const hasTitleParameter = hasQuerySourceChannel(item, "title_verified_parameter");
  const hasHumanParameter = hasQuerySourceChannel(
    item,
    "human_confirmed_decision_parameter",
  );
  if (hasPlatformExpansion && hasHumanParameter) return "平台根词扩展 + 人工决策参数";
  if (hasPlatformExpansion && hasTitleParameter) return "平台根词扩展 + 标题确认参数";
  if (hasPlatformExpansion && hasModelDirect) return "平台根词扩展 + 南非模型";
  const channel = item.validation_evidence.query_source_channel;
  if (channel === "takealot_root_expansion") return "平台根词扩展词";
  if (channel === "takealot_autocomplete_path") return "历史补全路径词（旧口径）";
  if (channel === "model_south_african_direct") return "图文融合·南非完整搜索词";
  if (channel === "comparison_resample") return "历史同词复采";
  if (channel === "title_verified_parameter") return "标题确认参数词";
  if (channel === "human_confirmed_decision_parameter") return "人工确认决策参数词";
  return "来源待复核";
}

function rootSourceLabel(source: SearchRankingRootSource | string | null | undefined) {
  return {
    human_confirmed_product_fact: "人工确认商品事实",
    image_title_first_instinct: "图文融合模型预测",
    title_word_root: "主标题确定性拆词",
    result_page_learning: "搜索结果页反向学习",
    image_title_need_state: "相邻需求模型词根",
    title_cross_check: "图题交叉验证词",
  }[String(source ?? "")] ?? "历史来源";
}

function sourceLabel(item: SearchRankingKeywordResult) {
  const evidence = item.validation_evidence;
  if (item.relevance_status === "comparison_resample") {
    return "上一轮建议打法的同词排名基线复采";
  }
  const resampleSuffix = evidence.comparison_role != null
    || evidence.comparison_baseline_rank != null
    ? " · 同时用于上一轮打法的同词复采"
    : "";
  const recoveryPrefix = evidence.adaptive_recovery
    ? evidence.adaptive_recovery_source === "result_page_learning"
      ? "自适应补救词 · 偏差页根词扩展 · "
      : "自适应补救词 · 次优根词扩展 · "
    : "";
  const journeyTypes = new Set([
    ...(evidence.journey_types ?? []),
    ...(evidence.journey_type ? [evidence.journey_type] : []),
  ]);
  if (journeyTypes.has("human_confirmed_decision_parameter")) {
    return `运营已将当前标题中的该规格确认为购买决策参数；参数值不是模型猜测，只有通过同商品族完整搜索页后才可前置${resampleSuffix}`;
  }
  if (journeyTypes.has("title_decision_parameter")) {
    if (evidence.autocomplete_rank && evidence.autocomplete_seed) {
      return `当前标题已明确、且图片商品身份支持的关键选购参数；输入“${evidence.autocomplete_seed}”后的 Takealot 第 ${evidence.autocomplete_rank} 项${resampleSuffix}`;
    }
    return `当前标题已明确、且图片商品身份支持的关键选购参数精准词；参数值不由模型猜测，并经完整搜索页验证${resampleSuffix}`;
  }
  const expansionRoot = evidence.root_expansion_root ?? evidence.autocomplete_seed ?? "—";
  const expansionRank = evidence.root_expansion_rank ?? evidence.autocomplete_rank ?? "—";
  const rootSources = evidence.root_expansion_sources?.length
    ? evidence.root_expansion_sources
    : evidence.root_expansion_source
      ? [evidence.root_expansion_source]
      : [];
  const rootSourceTrail = rootSources.length
    ? `词根来源：${rootSources.map(rootSourceLabel).join(" + ")}；`
    : "";
  if (journeyTypes.has("platform_expansion_followup")) {
    return `${recoveryPrefix}${rootSourceTrail}先取得与商品相关的平台扩展词，再把完整词组“${expansionRoot}”作为新词根继续扩展，当前为第 ${expansionRank} 项${resampleSuffix}`;
  }
  if (journeyTypes.has("result_page_root_expansion") || journeyTypes.has("result_page_learning")) {
    return `${recoveryPrefix}${rootSourceTrail}从上一条偏差搜索页的同形态商品标题提炼完整根词，再取 Takealot 根词扩展第 ${expansionRank} 项${resampleSuffix}`;
  }
  if (journeyTypes.has("adjacent_opportunity")) {
    return `${recoveryPrefix}${rootSourceTrail}相邻需求完整根词“${evidence.journey_root ?? expansionRoot}”的 Takealot 平台扩展第 ${expansionRank} 项${resampleSuffix}`;
  }
  if (
    journeyTypes.has("platform_root_expansion")
    || journeyTypes.has("human_confirmed_fact_root_expansion")
    || journeyTypes.has("title_cross_check_root_expansion")
    || journeyTypes.has("model_fusion_root_expansion")
    || journeyTypes.has("title_root_expansion")
  ) {
    return `${recoveryPrefix}${rootSourceTrail}完整输入根词“${expansionRoot}”后的 Takealot 平台扩展第 ${expansionRank} 项${resampleSuffix}`;
  }
  if (journeyTypes.has("autocomplete_backtrack")) {
    return `${recoveryPrefix}历史逐字补全记录（旧口径），当时改选第 ${evidence.autocomplete_rank ?? "—"} 项${resampleSuffix}`;
  }
  if (journeyTypes.has("switched_instinct_root")) {
    return `${recoveryPrefix}历史补全路径（旧口径），更换词根“${evidence.journey_root ?? evidence.autocomplete_seed ?? "—"}”后取得的平台词${resampleSuffix}`;
  }
  if (journeyTypes.has("known_long_tail") && !evidence.autocomplete_rank) {
    return `${recoveryPrefix}目标明确买家会直接输入的长尾词，由图片识别提出并经完整搜索页验证${resampleSuffix}`;
  }
  if (evidence.autocomplete_rank && evidence.autocomplete_seed) {
    const directAlso = journeyTypes.has("known_long_tail") ? " · 同时命中图片长尾词" : "";
    return `${recoveryPrefix}历史输入“${evidence.autocomplete_seed}”后的 Takealot 第 ${evidence.autocomplete_rank} 项（旧补全口径）${directAlso}${resampleSuffix}`;
  }
  return `${recoveryPrefix}主图与当前标题融合生成的南非完整搜索词${resampleSuffix}`;
}

function journeyPathLabel(item: SearchRankingKeywordResult) {
  const evidence = item.validation_evidence;
  const path = evidence.journey_path?.length
    ? evidence.journey_path
    : evidence.journey_paths?.find((candidate) => candidate.length) ?? [];
  const compact = path.filter((value, index) => value && value !== path[index - 1]);
  if (compact.length <= 1) return "";
  return hasQuerySourceChannel(item, "takealot_root_expansion")
    ? `根词扩展：${compact.join(" → ")}`
    : `历史补全路径（旧口径）：${compact.join(" → ")}`;
}

function rootExpansionCacheLabel(item: SearchRankingKeywordResult) {
  const evidence = item.validation_evidence;
  if (!evidence.autocomplete_rank) return "";
  const statusLabels: Record<string, string> = {
    fresh_hit: "24 小时共享缓存命中",
    miss_refreshed: "首次命中后实时采集",
    stale_refreshed: "超过 24 小时后本次命中刷新",
    not_configured: "本次未使用共享缓存",
    not_recorded: "历史记录未保存缓存状态",
  };
  const status = statusLabels[evidence.autocomplete_cache_status ?? ""] ?? "共享根词扩展证据";
  const observed = evidence.autocomplete_observed_at
    ? ` · 扩展采集 ${formatChinaDateTime(evidence.autocomplete_observed_at)}`
    : "";
  return `${status}${observed}`;
}

function rootExpansionRelationLabel(relation: string | null | undefined) {
  if (relation === "same_product") return "相关·同品身份";
  if (relation === "adjacent_demand") return "相关·结构化相邻商品族";
  if (relation === "irrelevant") return "不相关·不进入搜索";
  return "旧记录·未保存逐项判定";
}

function rootExpansionReasonLabel(reason: string | null | undefined) {
  return {
    same_product_term_match: "命中已验证的同品名称或直接别名",
    current_title_direct_product_phrase: "该完整产品短语连续出现在当前标题中",
    product_identity_anchor_with_supported_context: "保留商品身份词，并带有标题或图片支持的上下文",
    structured_adjacent_product_family_match: "命中模型明确列出的同一买家任务替代商品族",
    matched_excluded_product_term: "命中配件、易混淆品或人工排除商品词",
    adjacent_family_requires_structured_opportunity_root: "虽像相邻商品，但不是由结构化相邻需求词根进入",
    result_page_followup_missing_primary_product_shape: "偏差页补救仍未保留当前商品的主要形态",
    no_product_identity_or_structured_adjacent_match: "只有修饰词重合，未保留商品身份，也未命中结构化相邻商品族",
    empty_or_unusable_phrase: "平台返回内容无法形成完整词根",
  }[String(reason ?? "")] ?? "旧记录没有保存逐项筛选原因";
}

function validationStatusLabel(status: string | null | undefined) {
  const labels: Record<string, string> = {
    baseline_created: "已建立修改前基线",
    pending_title_change: "建议待实际修改",
    changed_to_other_title: "检测到其他标题修改",
    insufficient_comparable_evidence: "修改后证据不足",
    observed_forward: "修改后已观察到同词前移",
    mixed_movement: "不同热词有升有降",
    no_observed_forward: "尚未观察到前移",
  };
  return labels[status ?? ""] ?? "等待后续验证";
}

function titleStrategyLabel(strategy: string | null | undefined) {
  const labels: Record<string, string> = {
    contiguous_core: "完整连续词组版",
    hot_term_coverage: "类目热词覆盖版",
    adjacent_opportunity: "S/A蓝海命名版",
    historical: "历史建议标题",
  };
  return labels[strategy ?? ""] ?? "历史建议打法";
}

function confidenceLabel(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function providerLabel(provider: string) {
  const labels: Record<string, string> = {
    qwen: "千问",
    doubao: "豆包",
    openai: "OpenAI（历史）",
  };
  return labels[provider] ?? provider;
}

function costLabel(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "¥0（缓存复用）";
  return `约 ¥${value.toFixed(4)}`;
}

function incurredCostLabel(value: number | null | undefined) {
  if (value === null || value === undefined) return "费用暂无法估算";
  return `估算 ¥${value.toFixed(4)}`;
}

function formatWholeNumber(value: number | null | undefined) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value ?? 0);
}

function batchStatusLabel(status: SearchRankingBatchStatusValue | null | undefined) {
  const labels: Record<SearchRankingBatchStatusValue, string> = {
    queued: "排队等待单任务锁",
    running: "严格串行运行中",
    pausing: "当前商品族结束后暂停",
    paused: "已暂停",
    paused_after_error: "遇错暂停，未自动重试",
    stopping: "当前商品族结束后保存进度",
    stopped: "已停止，可继续断点",
    interrupted: "ERP 重启中断，待人工确认",
    completed: "全部完成",
  };
  return status ? labels[status] : "尚未启动";
}

function errorMessage(caught: unknown, fallback: string) {
  return caught instanceof ApiRequestError ? caught.message : fallback;
}
</script>

<template>
  <div class="ranking-page">
    <section class="method-banner">
      <div class="method-banner-copy">
        <p class="method-eyebrow">IMAGE → TITLE → PLATFORM</p>
        <h2>图文融合搜索定位</h2>
        <span class="method-intro">
          主图独立识别，标题参与融合，候选再用 Takealot 实时结果验证。视觉事实只取自图片；地域语境不补造不可见事实。
        </span>
        <div v-if="listPayload" class="method-context" aria-label="模型预测语境">
          <span>
            {{ listPayload.status.model_market_context }} · {{ listPayload.status.model_language_variant }} · 本地客户语境
          </span>
          <span>普通查看只读本地数据</span>
        </div>
      </div>

      <div v-if="listPayload" class="method-overview">
        <div class="method-model-route" aria-label="模型服务链">
          <article>
            <span>主模型</span>
            <strong>{{ listPayload.status.provider_label }}</strong>
            <small>{{ listPayload.status.primary_model }}</small>
          </article>
          <span v-if="listPayload.status.fallback_model" class="method-model-arrow" aria-hidden="true">→</span>
          <article v-if="listPayload.status.fallback_model">
            <span>跨厂商备用</span>
            <strong>{{ listPayload.status.fallback_provider_label }}</strong>
            <small>{{ listPayload.status.fallback_model }}</small>
          </article>
        </div>

        <dl class="method-guardrail-grid">
          <div>
            <dt>扫描边界</dt>
            <dd>
              <strong>每词 ≤ {{ listPayload.status.max_pages }} 页</strong>
              <span>默认相关性 · 每页 ≤ 36 个自然商品 · 4 列坐标</span>
            </dd>
          </div>
          <div>
            <dt>探索预算</dt>
            <dd>
              <strong>{{ listPayload.status.root_expansion_input_limit }} 个词根/词组</strong>
              <span>{{ listPayload.status.search_query_attempt_limit }} 个搜索词</span>
            </dd>
          </div>
          <div>
            <dt>请求节流</dt>
            <dd>
              <strong>至少 {{ listPayload.status.public_request_min_interval_seconds }} 秒</strong>
              <span>+ 随机 0–{{ listPayload.status.public_request_jitter_seconds }} 秒</span>
            </dd>
          </div>
        </dl>

        <details class="method-details">
          <summary>
            <span>词根与平台扩展规则</span>
            <small>查看来源优先级与入选口径</small>
          </summary>
          <div class="method-detail-content">
            <article>
              <strong>平台扩展</strong>
              <span>
                原始返回不直接入选；仅保留同品身份或结构化相邻商品族，最多
                {{ listPayload.status.root_expansion_followup_root_limit }} 个可继续作为词组词根。
              </span>
            </article>
            <article>
              <strong>来源优先级</strong>
              <span>
                {{ listPayload.status.root_source_priority.map((source, index) => `${index + 1}. ${rootSourceLabel(source)}`).join(" → ") }}
              </span>
            </article>
            <span class="method-detail-note">
              主标题根词保留最低覆盖；第4级需先取得真实搜索结果页。地域语境不代表平台实测搜索量。
            </span>
          </div>
        </details>
      </div>
    </section>

    <p v-if="eligibility" class="eligibility-note">
      当前授权 Offer {{ eligibility.current_offer_count }} 条，严格在售 {{ eligibility.eligible_count }} 条；
      {{ eligibility.excluded_count }} 条已在模型调用前排除。最近完整刷新：
      {{ eligibility.latest_capture_at ? formatChinaDateTime(eligibility.latest_capture_at) : "暂无" }}，
      有效期 {{ eligibility.max_age_hours }} 小时。
    </p>

    <section class="batch-panel" aria-labelledby="search-ranking-batch-title">
      <div class="batch-heading">
        <div>
          <p>ALL ACCESSIBLE STORES · STRICT SERIAL</p>
          <h3 id="search-ranking-batch-title">全部授权店铺一键串行分析</h3>
        </div>
        <span v-if="batchState" :class="['batch-status', batchState.status]">
          {{ batchStatusLabel(batchState.status) }}
        </span>
      </div>
      <div v-if="batchLoading && !batchPreview" class="batch-loading">正在按当前授权店铺计算商品数、缓存与费用…</div>
      <template v-else-if="batchPreview">
        <div class="batch-metrics">
          <article>
            <span>本次范围</span>
            <strong>{{ batchPreview.store_count }} 店 · {{ batchPreview.eligible_count }} 商品族</strong>
            <small>
              {{ batchPreview.eligible_offer_count }} 条有效 Offer 按同店同 PLID 合并 ·
              {{ batchPreview.variant_family_count }} 个多变体商品族
            </small>
          </article>
          <article>
            <span>新双阶段分析</span>
            <strong>{{ batchPreview.fresh_vision_count }} 个商品族</strong>
            <small>
              已有同图同标题缓存 {{ batchPreview.existing_vision_cache_hit_count }} · 同批复用
              {{ batchPreview.same_batch_vision_reuse_count }}
            </small>
          </article>
          <article>
            <span>Token 预估</span>
            <strong>{{ formatWholeNumber(batchPreview.estimated_usage.total_tokens) }}</strong>
            <small>
              单次历史中位 {{ formatWholeNumber(batchPreview.estimated_usage.total_tokens_per_fresh_image) }} ·
              样本 {{ batchPreview.estimated_usage.historical_sample_count }} 次
            </small>
          </article>
          <article>
            <span>人民币预估</span>
            <strong>
              ¥{{ batchPreview.estimated_cost.typical_low_cny.toFixed(2) }}–¥{{ batchPreview.estimated_cost.typical_high_cny.toFixed(2) }}
            </strong>
            <small>
              基准 ¥{{ batchPreview.estimated_cost.base_cny.toFixed(2) }} ·
              历史保守上界 ¥{{ batchPreview.estimated_cost.conservative_upper_cny.toFixed(2) }}
            </small>
          </article>
          <article>
            <span>预计总时长</span>
            <strong>
              {{ batchPreview.estimated_duration.likely_min_hours }}–{{ batchPreview.estimated_duration.likely_max_hours }} 小时
            </strong>
            <small>仅 3–5 秒请求节流下限约 {{ batchPreview.estimated_duration.pacing_floor_hours }} 小时</small>
          </article>
        </div>

        <details class="batch-store-detail">
          <summary>查看各店商品与新识别数量</summary>
          <div>
            <span v-for="store in batchPreview.stores" :key="store.code">
              <strong>{{ store.display_name }}</strong>
              {{ store.eligible_count }} 商品族 / {{ store.eligible_offer_count }} Offer ·
              {{ store.fresh_vision_count }} 个需新双阶段分析
            </span>
          </div>
        </details>

        <div v-if="batchState?.details_available" class="batch-progress-panel">
          <div class="batch-progress-copy">
            <strong>
              {{ batchStatusLabel(batchState.status) }} ·
              {{ batchState.processed_count ?? 0 }} / {{ batchState.target_count ?? 0 }}
            </strong>
            <span>
              完成 {{ batchState.completed_count ?? 0 }} · 跳过 {{ batchState.skipped_count ?? 0 }} ·
              失败 {{ batchState.failed_count ?? 0 }} · 实际费用约
              ¥{{ (batchState.usage?.estimated_cost_cny ?? 0).toFixed(4) }}
            </span>
          </div>
          <div class="batch-progress-track" aria-label="批次进度">
            <span :style="{ width: `${batchProgressPercent}%` }"></span>
          </div>
          <small v-if="batchState.current_target">
            当前 {{ batchState.current_target.index }}：{{ batchState.current_target.store_name }} ·
            PLID{{ batchState.current_target.productline_id || "—" }} ·
            {{ batchState.current_target.shared_family_title || batchState.current_target.title || batchState.current_target.offer_id }} ·
            {{ batchState.current_target.variant_count ?? 1 }} 个变体共用本链路 ·
            变体参数 {{ batchVariantParameterSummary(batchState.current_target) }}
          </small>
          <small v-if="(batchState.deduplicated_pending_variant_count ?? 0) > 0" class="batch-resume-note">
            旧检查点已合并 {{ batchState.deduplicated_pending_variant_count }} 条尚未处理的重复变体；已处理记录保留，继续时不会重跑这些变体。
          </small>
          <small v-if="batchState.last_error" class="batch-error">{{ batchState.last_error }}</small>
          <small v-if="batchState.usage && !batchState.usage.cost_accounting_complete" class="batch-error">
            批次曾在响应完成前中断，当前实际费用累计可能不完整，请以供应商账单为准。
          </small>
        </div>
        <p v-else-if="batchState?.message" class="batch-owner-note">{{ batchState.message }}</p>

        <div class="batch-actions">
          <button
            v-if="!batchState"
            class="batch-start-button"
            :disabled="batchAction !== '' || !batchPreview.eligible_count"
            @click="startFullBatch"
          >
            {{ batchAction === "start" ? "正在锁定快照…" : "确认费用并一键启动" }}
          </button>
          <button
            v-if="batchState?.can_pause"
            :disabled="batchAction !== ''"
            @click="runBatchControl('pause')"
          >
            {{ batchAction === "pause" ? "正在请求暂停…" : "当前商品族后暂停" }}
          </button>
          <button
            v-if="batchState?.can_resume"
            class="batch-start-button"
            :disabled="batchAction !== ''"
            @click="runBatchControl('resume')"
          >
            {{ batchAction === "resume" ? "正在继续…" : `继续未完成任务（剩余 ${batchState.remaining_count ?? 0}）` }}
          </button>
          <button
            v-if="batchState?.can_stop"
            class="batch-stop-button"
            :disabled="batchAction !== ''"
            @click="runBatchControl('stop')"
          >
            {{ batchAction === "stop" ? "正在保存进度…" : "保存进度并退出" }}
          </button>
          <button
            v-if="batchState?.can_restart"
            :disabled="batchAction !== ''"
            @click="restartFullBatch"
          >
            {{ batchAction === "restart" ? "正在从头建立任务…" : "从头重新开始" }}
          </button>
        </div>
        <p class="batch-policy-note">
          同一店铺内相同 PLID 的变体合并为一个商品族，只选一个代表 Offer 走完整链路。启动前会重新核对同一快照。
          运行中全局最大并发为 1，补全与搜索页公开请求随机间隔
          {{ batchPreviewPayload?.policy.public_request_min_interval_seconds }}–{{ batchPreviewPayload?.policy.public_request_max_interval_seconds }} 秒；
          不调用倒搜、不自动重试，网络或供应商错误后暂停等待人工决定。费用按当前主模型价目与本机历史 Token 推算，实际账单以供应商为准。
        </p>
      </template>
    </section>

    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>

    <div class="ranking-layout">
      <aside class="product-rail">
        <div class="rail-title">
          <div><p>OWN BUYABLE PRODUCT FAMILIES</p><h3>自有在售商品族</h3></div>
          <span>{{ filteredProductFamilies.length }} 族 / {{ products.length }} Offer</span>
        </div>
        <input v-model="search" type="search" placeholder="商品名称支持模糊搜索，也可输入平台 SKU、公司 SKU 或 PLID" />
        <div class="rail-filters">
          <label>
            <span>交叉验证</span>
            <select v-model="identityDifferenceFilter">
              <option value="all">全部差异</option>
              <option value="high">差异大</option>
              <option value="moderate">中等差异</option>
              <option value="aligned">一致</option>
              <option value="manual">待人工事实</option>
              <option value="unanalysed">未分析</option>
            </select>
          </label>
          <label>
            <span>标题评分</span>
            <select v-model="titleScoreFilter">
              <option value="all">全部评分</option>
              <option value="85_plus">85–100</option>
              <option value="70_84">70–84</option>
              <option value="55_69">55–69</option>
              <option value="below_55">低于 55</option>
              <option value="insufficient">证据不足</option>
              <option value="unscored">未评分</option>
            </select>
          </label>
        </div>
        <div v-if="loadingList" class="empty-state">正在读取本地商品…</div>
        <div v-else-if="!filteredProductFamilies.length" class="empty-state">
          没有符合“自有、buyable、正数可售库存、快照新鲜”的商品
        </div>
        <template v-else>
          <button
            v-for="family in filteredProductFamilies"
            :key="family.key"
            class="product-row"
            :class="{ active: selectedFamily?.key === family.key }"
            @click="selectProduct(family.representative)"
          >
            <img
              v-if="imageUrl(family.representative)"
              :src="imageUrl(family.representative)"
              :alt="family.representative.title ?? family.representative.sku ?? '商品图片'"
              @error="markImageFailed(family.representative.image_url)"
            />
            <span v-else class="image-fallback">NO IMG</span>
            <span class="product-copy">
              <strong>{{ family.shared_title || family.representative.title || "未命名商品" }}</strong>
              <small>PLID{{ family.productline_id || "—" }} · {{ family.variant_count }} 个变体合并</small>
              <small v-if="family.variant_parameter_values.length" class="family-parameter-summary">
                变体参数 {{ variantParameterSummary(family.variant_parameter_values) }}
              </small>
              <small>合计可售 {{ family.total_available_stock }} · 代表平台 SKU {{ family.representative.sku || family.representative.offer_id }}</small>
              <small v-if="props.storeScope !== 'current'">店铺 {{ family.representative.store_name || family.representative.store_code || "—" }}</small>
              <small>公司 SKU {{ familyCompanySkuLabel(family.variants) }}</small>
              <em :class="family.latest_analysis?.status ?? 'untracked'">
                {{
                  family.latest_analysis?.status === "completed"
                    ? "已有定位"
                    : family.latest_analysis?.status === "failed"
                      ? "上次失败"
                      : family.latest_analysis?.status === "running"
                        ? "采集中"
                        : "未定位"
                }}
              </em>
              <small v-if="family.latest_analysis?.manual_fact_required" class="rail-warning">待人工事实 · 批次已跳过</small>
              <small v-else-if="family.latest_analysis?.identity_large_difference" class="rail-warning">图与标题差异大</small>
              <small v-if="family.latest_analysis?.title_score_value !== null && family.latest_analysis?.title_score_value !== undefined">
                代表标题 {{ family.latest_analysis.title_score_value }} 分 · 证据覆盖 {{ family.latest_analysis.title_score_evidence_coverage ?? 0 }}%
              </small>
            </span>
          </button>
        </template>
      </aside>

      <main
        ref="rankingDetailElement"
        class="ranking-detail"
        :style="rankingDetailMinimumHeight ? { minHeight: `${rankingDetailMinimumHeight}px` } : undefined"
      >
        <div v-if="loadingDetail" class="detail-loading">正在读取定位记录…</div>
        <template v-else-if="selectedProduct">
          <section class="product-hero">
            <img
              v-if="imageUrl(selectedProduct)"
              :src="imageUrl(selectedProduct)"
              :alt="selectedProduct.title ?? '商品图片'"
              @error="markImageFailed(selectedProduct.image_url)"
            />
            <span v-else class="hero-fallback">NO IMAGE</span>
            <div class="hero-copy">
              <p>
                PLID{{ selectedProduct.productline_id || "—" }} ·
                {{ selectedFamily?.variant_count ?? 1 }} 个变体 · 链路代表
                {{ selectedFamily?.representative.sku || selectedFamily?.representative.offer_id || selectedProduct.offer_id }}
              </p>
              <h2>{{ selectedProduct.title || "未命名商品" }}</h2>
              <p v-if="props.storeScope !== 'current'">店铺 {{ selectedProduct.store_name || selectedProduct.store_code || "—" }}</p>
              <p>公司 SKU {{ selectedProduct.company_sku || "未关联" }}</p>
              <span class="selected-variant-parameter">
                当前 Offer 变体参数：
                <b>{{ variantParameterSummary(variantParametersFor(selectedProduct.offer_id).map((item) => item.value)) }}</b>
                · 来自 Seller 标题差异，代表图不自动验证这些值
              </span>
              <span class="ownership-note">
                授权 Seller Offers 当前记录 · {{ selectedProduct.offer_status }} · 商品族合计可售
                {{ selectedFamily?.total_available_stock ?? selectedProduct.available_stock }} ·
                {{ formatChinaDateTime(selectedProduct.captured_at) }}
              </span>
              <details v-if="(selectedFamily?.variant_count ?? 1) > 1" class="family-variants">
                <summary>切换 {{ selectedFamily?.variant_count }} 个变体（共享一次完整链路）</summary>
                <button
                  v-for="variant in selectedFamily?.variants"
                  :key="variant.offer_id"
                  type="button"
                  :class="{ active: variant.offer_id === selectedProduct.offer_id }"
                  @click="selectProduct(variant)"
                >
                  <strong>{{ variant.title || "未命名变体" }}</strong>
                  <span>平台 {{ variant.sku || variant.offer_id }} · 公司 {{ variant.company_sku || "未关联" }} · 可售 {{ variant.available_stock }}</span>
                  <em>
                    变体参数：{{ variantParameterSummary(variantParametersFor(variant.offer_id).map((item) => item.value)) }}
                  </em>
                </button>
              </details>
              <span v-if="!selectedProduct.analyzable" class="blocked-note">
                当前链接已不满足自有在售闸门，模型不会被调用。
              </span>
            </div>
            <button
              class="analyze-button"
              :disabled="analyzing || factSaving || decisionParameterSaving || batchIsActive || !selectedProduct.analyzable"
              @click="runAnalysis"
            >
              {{
                analyzing
                  ? "正在识别并逐页定位…"
                  : decisionParameterSaving
                    ? "正在保存参数判断…"
                    : factSaving
                      ? "商品事实处理中…"
                    : analysis
                      ? "重新分析商品族"
                      : "分析商品族"
              }}
            </button>
          </section>

          <p v-if="analyzing" class="running-note">
            当前商品族只走一次链路：模型同时读取共同主体标题、代表标题和各 Offer 的标题变体参数，
            用代表 Offer 主图做隔离交叉验证，再生成完整搜索词，并把标题有效词和模型根词逐个提交平台扩展；
            颜色、尺寸、容量等差异值始终留在各自 Offer，不会互相套用。
            所有根词扩展和搜索页公开请求共用间隔。
          </p>
          <p v-if="analysis?.variant_projection?.applied" class="variant-projection-note">
            当前显示的是 {{ selectedProduct.sku || selectedProduct.offer_id }} 的独立标题评分与建议；
            图片识别、平台根词扩展和搜索页排名来自商品族代表 Offer 的同一次链路，没有再次调用模型或平台搜索。
          </p>
          <p
            v-if="analysis?.variant_projection && !analysis.variant_projection.family_snapshot_current"
            class="attempt-note"
          >
            当前商品族的变体成员、标题、参数或主图已不同于分析快照。旧证据仍保留审计，但请重新分析商品族后再采用标题建议。
          </p>
          <p
            v-if="analysis?.variant_projection && !analysis.variant_projection.decision_parameter_confirmation_current"
            class="attempt-note"
          >
            当前变体的决策参数人工确认晚于本次分析；新确认已本地留痕，但尚未用平台搜索重新验证，因此旧标题建议不会直接套用新参数。
          </p>
          <p v-if="detail && !detail.status.configured" class="config-note">
            当前仅可查看历史结果。服务端未配置 DASHSCOPE_API_KEY / ARK_API_KEY，点击时不会产生费用或外部请求。
          </p>
          <p
            v-if="detail?.latest_attempt && detail.latest_attempt.status !== 'completed'"
            class="attempt-note"
            role="status"
          >
            最近一次分析{{ detail.latest_attempt.status === "failed" ? "失败" : "仍在运行" }}。
            {{ analysis ? "页面继续保留最后一次成功的三种标题与排名结果。" : "目前还没有可显示的成功结果。" }}
            <template v-if="detail.latest_attempt.vision_stage_completed">
              模型阶段已成功并记账：{{ detail.latest_attempt.usage?.total_tokens ?? "—" }} tokens ·
              {{ costLabel(detail.latest_attempt.estimated_cost_cny ?? null) }}；后续重试可复用这次图片识别。
            </template>
            <template v-else-if="latestAttemptHasUnusableSpend">
              模型请求已产生 {{ detail.latest_attempt.usage?.total_tokens ?? "—" }} tokens ·
              {{ incurredCostLabel(detail.latest_attempt.estimated_cost_cny) }}，但识别结构不可用，重试不能复用本次结果。
            </template>
            <template v-if="detail.latest_attempt.error">原因：{{ detail.latest_attempt.error }}</template>
          </p>

          <section v-if="decisionParameterProfile" class="decision-parameter-section">
            <div class="section-heading decision-parameter-heading">
              <div>
                <p>HUMAN DECISION PARAMETER CLASSIFICATION</p>
                <h3>决策参数人工确认</h3>
              </div>
              <span :class="{ confirmed: decisionParameterProfile.current_title_confirmed }">
                {{
                  decisionParameterProfile.current_title_confirmed
                    ? `已确认 ${decisionParameterProfile.decision_parameter_count} 项决策参数`
                    : decisionParameterProfile.latest_confirmation
                      ? "标题变化 待重新确认"
                      : "当前标题待确认"
                }}
              </span>
            </div>
            <div v-if="decisionParameterProfile.candidates.length" class="decision-parameter-grid">
              <article
                v-for="item in decisionParameterProfile.candidates"
                :key="item.parameter_key"
                :class="{
                  positive: decisionParameterChoices[item.parameter_key] === true,
                  negative: decisionParameterChoices[item.parameter_key] === false,
                }"
              >
                <div class="decision-parameter-title">
                  <span>{{ decisionParameterTypeLabel(item.parameter_type) }}</span>
                  <strong>{{ item.parameter_value }}</strong>
                </div>
                <small :class="{ recommended: item.system_recommendation === 'decision_parameter' }">
                  {{ item.system_recommendation === "decision_parameter" ? "系统建议重点核对" : "系统默认按普通规格后置" }}
                  · {{ item.system_reason }}
                </small>
                <div class="decision-parameter-choice" role="radiogroup" :aria-label="`${item.parameter_value}是否为决策参数`">
                  <label>
                    <input
                      v-model="decisionParameterChoices[item.parameter_key]"
                      type="radio"
                      :name="`decision-${selectedProduct.offer_id}-${item.parameter_key}`"
                      :value="true"
                    />
                    是 决策参数
                  </label>
                  <label>
                    <input
                      v-model="decisionParameterChoices[item.parameter_key]"
                      type="radio"
                      :name="`decision-${selectedProduct.offer_id}-${item.parameter_key}`"
                      :value="false"
                    />
                    不是 保持后置
                  </label>
                </div>
              </article>
            </div>
            <p v-else class="decision-parameter-empty">
              当前标题没有识别到功率、容量、尺寸、数量、防护等级等明确规格。仍可人工确认“当前标题无可识别规格参数”，形成该标题的审计记录。
            </p>
            <div class="decision-parameter-actions">
              <button
                type="button"
                :disabled="analyzing || factSaving || decisionParameterSaving || !decisionParametersFullyClassified"
                @click="saveDecisionParameterConfirmation"
              >
                {{
                  decisionParameterSaving
                    ? "正在保存…"
                    : decisionParameterProfile.candidates.length
                      ? "保存当前标题参数判断"
                      : "确认当前标题无可识别规格"
                }}
              </button>
              <span v-if="decisionParameterProfile.latest_confirmation">
                最近确认：{{ decisionParameterProfile.latest_confirmation.confirmed_by_display_name || decisionParameterProfile.latest_confirmation.confirmed_by_username }} ·
                {{ formatChinaDateTime(decisionParameterProfile.latest_confirmation.confirmed_at) }}
              </span>
              <span v-else>尚无人工确认记录</span>
            </div>
            <p v-if="decisionParameterProfile.current_title_confirmed" class="decision-parameter-result">
              当前确认：
              <template v-if="decisionParameterProfile.applied_decision_values.length">
                {{ decisionParameterProfile.applied_decision_values.join(" / ") }} 为决策参数；需点击“重新验证定位”取得搜索页证据后才会前置。
              </template>
              <template v-else>当前标题没有参数被确认为决策参数，全部规格继续后置。</template>
            </p>
          </section>

          <template v-if="analysis">
            <section class="identity-grid">
              <article>
                <p>1 · 隔离图片观察</p>
                <h3>{{ analysis.visual_profile?.product_name || "未识别" }}</h3>
                <span>{{ analysis.visual_profile?.category || "类别未知" }} · 这一阶段不接收主标题或 SKU</span>
              </article>
              <article>
                <p>2 · 独立交叉验证</p>
                <h3>{{ analysis.recognition?.identity_difference_level === "high" ? "差异大" : analysis.recognition?.identity_difference_level === "moderate" ? "中等差异" : "一致" }}</h3>
                <span>{{ analysis.recognition?.title_identity_supported_terms?.join(" / ") || "未命中明确商品主体短语" }}</span>
                <small v-if="analysis.recognition?.identity_difference_warning">{{ analysis.recognition.identity_difference_warning }}</small>
                <small v-else>交叉验证结果单独保存，不会覆盖图片观察或主标题原文。</small>
              </article>
              <article>
                <p>3 · 图文融合生成</p>
                <h3>{{ analysis.product_name || "未识别" }}</h3>
                <span>{{ analysis.category || "类别未知" }} · {{ confidenceLabel(analysis.confidence) }}</span>
                <small v-if="providerFallbackSucceeded">
                  主服务未返回完整双阶段结构，已切换备用服务完成分析。
                </small>
                <small v-else>{{ providerLabel(analysis.provider) }} · {{ analysis.model }} · {{ analysis.vision_reused ? "复用同图同标题缓存" : "本次调用" }}</small>
              </article>
              <article>
                <p>模型用量</p>
                <h3>{{ analysis.usage.total_tokens ?? "—" }}</h3>
                <span>
                  输入 {{ analysis.usage.input_tokens ?? "—" }} · 输出 {{ analysis.usage.output_tokens ?? "—" }} tokens ·
                  {{ costLabel(analysis.estimated_cost_cny) }}（按 {{ detail?.status.pricing_snapshot_date }} 配置单价）
                </span>
              </article>
            </section>

            <p v-if="analysis.recognition?.manual_fact_required" class="comparison-warning">
              该商品缺少关键事实，批次已跳过且不会自动重试：{{ analysis.recognition.manual_fact_reason }}
              <template v-if="analysis.recognition.missing_facts?.length">（{{ analysis.recognition.missing_facts.join(" / ") }}）</template>
              可使用下方“人工确认商品事实”，但不强制补录。
            </p>

            <section v-if="productFactProfile" class="product-fact-section">
              <div class="section-heading product-fact-heading">
                <div>
                  <p>AUDITABLE PRODUCT FACT PROFILE</p>
                  <h3>商品事实档案</h3>
                </div>
                <span>
                  当前主图采用 {{ productFactProfile.applied_count }} 条 · 历史档案
                  {{ productFactProfile.archive_count }} 条
                </span>
              </div>
              <div v-if="productFactProfile.facts.length" class="product-fact-grid">
                <article
                  v-for="fact in productFactProfile.facts"
                  :key="fact.id"
                  :class="{ applied: fact.applied_to_current_image, archived: fact.status !== 'active' }"
                >
                  <div>
                    <span>{{ productFactTypeLabel(fact.fact_type) }}</span>
                    <em>
                      {{
                        fact.applied_to_current_image
                          ? "当前采用"
                          : fact.needs_image_reconfirmation
                            ? "主图变化待复核"
                            : fact.status === "revoked"
                              ? "已人工停用"
                              : "历史版本"
                      }}
                    </em>
                  </div>
                  <strong>{{ fact.fact_term }}</strong>
                  <p v-if="fact.statement && fact.statement !== fact.fact_term">{{ fact.statement }}</p>
                  <small>
                    {{ productFactSourceLabel(fact.source_type) }} ·
                    {{ fact.confirmed_by_display_name || fact.confirmed_by_username }} ·
                    {{ formatChinaDateTime(fact.confirmed_at) }}
                  </small>
                  <small v-if="fact.revoke_reason">停用说明：{{ fact.revoke_reason }}</small>
                  <button
                    v-if="fact.status === 'active'"
                    type="button"
                    :disabled="factSaving"
                    @click="openFactRevocation(fact)"
                  >
                    停用并保留档案
                  </button>
                </article>
              </div>
              <p v-else class="product-fact-empty">
                尚无人工确认事实。每个已分析商品都可在下方按需补充，不强制录入。
              </p>
            </section>

            <section v-if="factRecommendation" class="fact-recommendation-section">
              <div class="section-heading fact-recommendation-heading">
                <div>
                  <p>HUMAN PRODUCT FACT CONFIRMATION</p>
                  <h3>人工确认商品事实</h3>
                </div>
                <span class="fact-recommendation-status" :class="{ recommended: factRecommendation.recommended }">
                  {{ factRecommendation.recommended ? "建议人工核对" : "可选人工补充" }}
                </span>
              </div>
              <article class="fact-recommendation-reason">
                <p>系统判断原因</p>
                <strong>{{ factRecommendation.reason }}</strong>
              </article>
              <div class="fact-recommendation-actions">
                <button
                  v-if="manualFactCanBeConfirmed"
                  class="manual-fact-confirm-open"
                  :disabled="analyzing || factSaving"
                  @click="openProductFactConfirmation"
                >
                  人工确认商品事实
                </button>
                <span v-else>当前模型服务未配置，暂不能在确认后重新验证搜索页。</span>
              </div>
            </section>

            <section class="keyword-section">
              <div class="section-heading">
                <div><p>PLATFORM-EVIDENCED QUERY STRATEGY</p><h3>搜索词策略与自然位置</h3></div>
                <span>
                  S级 {{ semanticGradeCounts.S }} 个 · A级 {{ semanticGradeCounts.A }} 个 ·
                  C/I级 {{ semanticGradeCounts["C/I"] }} 个 · {{ blueOceanKeywords.length }} 个蓝海词 ·
                  {{ comparisonKeywords.length }} 个改后同词复采 ·
                  {{ rejectedKeywords.length }} 个未采用
                </span>
                <small>
                  证据覆盖：{{ platformExpansionKeywords.length }} 个含平台根词扩展 ·
                  {{ modelDirectKeywords.length }} 个含图文融合南非完整搜索词 ·
                  {{ titleParameterKeywords.length }} 个含标题确认参数（同词可同时属于多类）
                </small>
              </div>
              <details
                v-if="contextualRootExpansionChecks.length"
                class="root-expansion-selection-audit"
              >
                <summary>
                  本商品词根/词组与平台扩展筛选 ·
                  {{ contextualRootExpansionSummary.phraseRoots }} 个词组词根 ·
                  {{ contextualRootExpansionSummary.eligible }} 项相关候选 ·
                  {{ contextualRootExpansionSummary.rejected }} 项未入选
                </summary>
                <p>
                  Takealot 对每个词根返回的原始第1–5项都会留作审计，但不会盲选。
                  只有保留本商品身份，或命中结构化相邻需求所列替代商品族的扩展，才可进入搜索；
                  其中 {{ contextualRootExpansionSummary.followupRoots }} 个相关扩展已作为完整词组词根继续观察下一层平台扩展。
                </p>
                <div class="root-expansion-selection-grid">
                  <article
                    v-for="(check, checkIndex) in contextualRootExpansionChecks"
                    :key="`${rootExpansionCheckLabel(check)}-${check.journey_depth ?? 0}-${checkIndex}`"
                  >
                    <header>
                      <strong>{{ rootExpansionCheckLabel(check) }}</strong>
                      <span>{{ rootExpansionCheckIsPhrase(check) ? "词组词根" : "单词词根" }}</span>
                      <small>{{ rootSourceLabel(check.root_source) }}</small>
                    </header>
                    <small v-if="(check.journey_depth ?? 0) > 0">
                      相关平台扩展“{{ check.parent_root }} → {{ rootExpansionCheckLabel(check) }}”已继续作为词根
                    </small>
                    <div
                      v-for="expansion in check.expansions ?? []"
                      :key="`${rootExpansionCheckLabel(check)}-${expansion.rank}-${expansion.phrase}`"
                      class="root-expansion-decision"
                      :class="expansion.relevance_status"
                    >
                      <b>第 {{ expansion.rank }} 项</b>
                      <strong>{{ expansion.phrase }}</strong>
                      <em>{{ rootExpansionRelationLabel(expansion.relation) }}</em>
                      <small>{{ rootExpansionReasonLabel(expansion.reason) }}</small>
                      <mark v-if="expansion.used_as_followup_root">已作为词组词根继续扩展</mark>
                    </div>
                    <p v-if="check.status === 'unavailable'">该词根本次平台扩展不可用，没有扩展词被选中。</p>
                  </article>
                </div>
              </details>
              <div class="keyword-list">
                <article
                  v-for="item in analysis.keywords"
                  :key="item.id"
                  class="keyword-card"
                  :class="item.relevance_status"
                >
                  <div class="keyword-main">
                    <div>
                      <span class="strategy-badge">{{ strategyLabel(item) }}</span>
                      <span class="source-channel-badge">{{ sourceChannelLabel(item) }}</span>
                      <a :href="item.search_url" target="_blank" rel="noreferrer">{{ item.keyword }}</a>
                      <small class="query-source">{{ sourceLabel(item) }}</small>
                      <small v-if="journeyPathLabel(item)" class="query-path">{{ journeyPathLabel(item) }}</small>
                      <small v-if="rootExpansionCacheLabel(item)" class="query-path">{{ rootExpansionCacheLabel(item) }}</small>
                      <span v-if="!item.found">{{ resultLabel(item) }}</span>
                    </div>
                    <strong v-if="item.found" class="keyword-position">
                      <span>第 {{ item.page_number }} 页 · 第 {{ item.row_number }} 行 · 第 {{ item.column_number }} 列</span>
                      <small>
                        跨页自然排名 #{{ item.organic_rank }}（自然商品序列中的第 {{ item.organic_rank }} 个）
                      </small>
                      <small v-if="locatedButNotAdopted(item)">已定位，但该搜索词未进入推荐词</small>
                    </strong>
                    <strong v-else>{{ scanScopeLabel(item) }}</strong>
                  </div>
                  <dl>
                    <div><dt>语义关系等级</dt><dd>{{ semanticGradeLabel(item) }}</dd></div>
                    <div><dt>首页 S/A/C+I 结果</dt><dd>{{ semanticResultCountLabel(item) }}</dd></div>
                    <div><dt>S级同品占比</dt><dd>{{ sameTypeRatioLabel(item) }}</dd></div>
                    <div>
                      <dt>首页S级同品命中</dt>
                      <dd>
                        <template v-if="hasFirstPageValidation(item)">
                          {{ item.validation_evidence.matched_first_page_results ?? item.validation_evidence.matched_top_results ?? 0 }} /
                          {{ item.validation_evidence.evaluated_first_page_results ?? item.validation_evidence.evaluated_top_results ?? 0 }}
                        </template>
                        <template v-else>未验证</template>
                      </dd>
                    </div>
                    <div><dt>平台返回商品数（供给规模）</dt><dd>{{ item.total_num_found ?? "—" }}</dd></div>
                    <div><dt>采集时间</dt><dd>{{ formatChinaDateTime(item.observed_at) }}</dd></div>
                  </dl>
                  <small v-if="!hasFirstPageValidation(item)">
                    本项未请求 Takealot 搜索页，因此没有首页同类率；“未验证”不能解释为 0%。
                  </small>
                </article>
              </div>
              <p class="position-notice">
                本轮最多验证 {{ detail?.status.search_query_attempt_limit ?? 14 }} 个图文融合搜索词，
                其中完整搜索词 {{ detail?.status.query_source_targets.model_south_african_direct ?? 6 }} 个、
                平台根词扩展词 {{ detail?.status.query_source_targets.takealot_root_expansion ?? 6 }} 个。
                实际公开请求 {{ analysis.shopper_journey?.public_request_count ?? 0 }} 次，均按 3–5 秒严格串行；
                同一根词下的扩展顺序不是搜索量，自然位只统计非赞助 product_views，并会随时间、地区、库存与价格变化。
              </p>
            </section>

            <section v-if="analysis.title_score" class="title-score-section">
              <div class="section-heading">
                <div><p>EVIDENCE-BASED TITLE QUALITY</p><h3>现有主标题质量评分</h3></div>
                <strong>{{ analysis.title_score.score }} / 100 · {{ analysis.title_score.label }}</strong>
              </div>
              <p class="position-notice">
                只评价当前主标题文字与固定商品/搜索证据的匹配；自然排名、首页同类占比、竞争数、价格、库存、广告位和平台扩展顺序均不计分。
              </p>
              <p v-if="!analysis.title_score.current_title_match" class="comparison-warning">
                当前 Offer 标题已变化；该分数只对应分析时标题，重新分析后才会进入列表评分筛选。
              </p>
              <div class="title-score-overview">
                <span>证据覆盖 {{ analysis.title_score.evidence_coverage }}%</span>
                <span>可评分 {{ analysis.title_score.available_points }} 分项权重</span>
                <span>规则 {{ analysis.title_score.scoring_version }}</span>
              </div>
              <div class="title-score-grid">
                <article v-for="component in analysis.title_score.components" :key="component.key">
                  <header>
                    <strong>{{ component.label }}</strong>
                    <span>{{ component.available ? `${component.score} / ${component.max_points}` : "证据缺失·不计分" }}</span>
                  </header>
                  <p>{{ component.summary }}</p>
                </article>
              </div>
              <small v-if="analysis.title_score.compatibility_projection" class="score-limitations">
                这是旧版记录按新版标题质量规则进行的本地换算；未改写历史记录，也没有重新调用模型或平台。
              </small>
              <small class="score-limitations">
                不计分：{{ analysis.title_score.non_scoring_signals.map((item) => item.label).join("、") }}。
              </small>
              <small class="score-limitations">{{ analysis.title_score.limitations.join(" ") }}</small>
            </section>

            <section class="title-review">
              <div class="section-heading">
                <div><p>THREE TITLE PLAYBOOKS</p><h3>主标题三种打法</h3></div>
                <span>{{ validationStatusLabel(analysis.title_validation?.status) }}</span>
              </div>
              <article class="current-title-panel">
                <p>本次分析时标题</p>
                <strong>{{ analysis.source_title }}</strong>
                <small v-if="currentTitleChangedSinceAnalysis">
                  当前 Offer 标题已变为“{{ selectedProduct.title }}”。以下建议仍基于上方分析时标题，需重新分析后再采用。
                </small>
              </article>
              <div class="title-strategy-grid">
                <article
                  v-for="(strategy, index) in titleStrategies"
                  :key="strategy.strategy"
                  class="title-strategy-card"
                  :class="[strategy.strategy, { unavailable: !strategy.available }]"
                >
                  <header>
                    <span class="strategy-number">0{{ index + 1 }}</span>
                    <div>
                      <p>建议打法</p>
                      <h4>{{ strategy.label }}</h4>
                    </div>
                  </header>
                  <strong class="suggested-title">
                    {{ strategy.available && strategy.title ? strategy.title : "本轮暂无达标方案" }}
                  </strong>
                  <p class="strategy-explanation">{{ strategy.explanation }}</p>
                  <small v-if="strategy.strategy === 'contiguous_core'" class="strategy-boundary">
                    完整短语优先是低风险写法，不代表平台公开了连续词组加权规则。
                  </small>
                  <small v-else-if="strategy.strategy === 'hot_term_coverage'" class="strategy-boundary">
                    优先覆盖不同完整根词入口；每个词都必须来自当时平台扩展并通过类目页验证。扩展顺序不是公开搜索量。
                  </small>
                  <small v-else class="strategy-boundary">
                    入选前必须实际搜到本商品，并通过按窄形态标题词核算的首页低竞争门槛；
                    未获当前标题或运营人工确认事实支持的材质、尺寸、受众、兼容性或功效声明会被拦截。
                  </small>
                  <div class="strategy-evidence">
                    <span>证据词</span>
                    <div v-if="strategy.evidence_keywords.length">
                      <em v-for="keyword in strategy.evidence_keywords" :key="keyword">{{ keyword }}</em>
                    </div>
                    <small v-else>本轮暂无可用证据词</small>
                  </div>
                </article>
              </div>
              <p class="title-format-note">
                建议标题统一仅保留字母、数字和空格；商品类型与相关关键词前置，功能卖点居中，
                功率、电压、容量、尺寸、重量、数量及防护等级等明确规格参数默认后置。
                只有运营在上方逐项确认为决策参数，并且带该参数的同商品族搜索词通过实时搜索页验证后，才可适度前置；
                系统建议只用于提醒核对，不能代替人工判断。300W、IP66 等未确认或验证未通过时仍保持后置。
              </p>
              <article
                v-if="analysis.title_validation?.matched_strategy || analysis.title_validation?.matched_suggestion"
                class="matched-strategy-note"
              >
                <p>修改后复采归属</p>
                <strong>
                  上一轮实际采用：{{ titleStrategyLabel(analysis.title_validation.matched_strategy) }}
                </strong>
                <span v-if="analysis.title_validation.matched_suggestion">
                  {{ analysis.title_validation.matched_suggestion }}
                </span>
                <small>下方位次变化只验证这个历史标题，不代表本轮三张候选卡已经采用。</small>
              </article>
              <div
                v-if="analysis.title_validation?.comparisons?.length"
                class="movement-list"
              >
                <span v-for="row in analysis.title_validation.comparisons" :key="row.keyword">
                  {{ row.keyword }}：#{{ row.before_rank }} → #{{ row.after_rank }}
                  （{{ row.delta > 0 ? `前移 ${row.delta}` : row.delta < 0 ? `后移 ${-row.delta}` : "不变" }}）
                </span>
              </div>
              <p
                v-if="analysis.title_validation?.missing_baseline_keywords?.length || analysis.title_validation?.missing_keywords?.length"
                class="comparison-warning"
              >
                <template v-if="analysis.title_validation?.missing_baseline_keywords?.length">
                  以下目标词上轮没有可量化的自然位：{{ analysis.title_validation.missing_baseline_keywords.join(" / ") }}。
                </template>
                <template v-if="analysis.title_validation?.missing_keywords?.length">
                  以下基线词本轮没有取得可比自然位：{{ analysis.title_validation.missing_keywords.join(" / ") }}。
                </template>
                因此本轮只报告证据不足，不判断标题已带来前移。
              </p>
              <p class="causality-note">
                不作“修改后一定前移”的虚假保证：建议默认标记为待验证。只有检测到标题确实改成建议文本，且再次采集该打法的相同证据词后，
                才会显示实际前移/后移；即使前移也只是观察结果，不能单独证明因果。
              </p>
            </section>

            <section class="history-section" v-if="detail?.history.length">
              <div class="section-heading"><div><p>AUDIT TRAIL</p><h3>定位历史</h3></div></div>
              <div class="history-list">
                <span v-for="run in detail.history" :key="run.id">
                  #{{ run.id }} · {{ formatChinaDateTime(run.created_at) }} · {{ run.model }} ·
                  {{ providerLabel(run.provider) }} ·
                  {{ run.status === "completed" ? confidenceLabel(run.confidence) : run.status }}
                </span>
              </div>
            </section>
          </template>
          <section v-else class="first-run">
            <p>NO RANKING EVIDENCE YET</p>
            <h3>这个商品还没有搜索定位记录</h3>
            <span>首次运行会保存隔离图片观察、标题交叉验证、图文融合搜索词、平台位置和证据化标题评分。</span>
          </section>
        </template>
        <div v-else class="detail-loading">请选择一个商品</div>
      </main>
    </div>

    <section class="autocomplete-library-section">
      <div class="section-heading autocomplete-library-heading">
        <div>
          <p>SHARED TAKEALOT ROOT EXPANSION EVIDENCE</p>
          <h3>平台原始词根/词组扩展库</h3>
        </div>
        <form @submit.prevent="loadRootExpansionLibrary">
          <input v-model="rootExpansionLibrarySearch" type="search" placeholder="筛选根词或平台扩展词" />
          <button type="submit" :disabled="rootExpansionLibraryLoading">
            {{ rootExpansionLibraryLoading ? "读取中…" : "筛选本地库" }}
          </button>
        </form>
      </div>
      <p v-if="rootExpansionLibrary" class="autocomplete-library-policy">
        全店分析共享 {{ rootExpansionLibrary.summary.root_count }} 个已观察完整词根或词组；
        已隐藏 {{ rootExpansionLibrary.summary.legacy_partial_input_state_count }} 个历史逐字补全状态。
        缓存采集满 {{ rootExpansionLibrary.policy.ttl_hours }} 小时后不会定时刷新，只有分析再次实际输入该根词时才刷新一次；
        打开或筛选本区只读本地数据库，不访问 Takealot。
        本区展示平台原始返回，不代表为当前商品入选；逐商品相关性选择以上方分析中的筛选审计为准。
        {{ rootExpansionLibrary.policy.note }}
      </p>
      <div v-if="rootExpansionLibrary?.roots.length" class="autocomplete-phrase-table">
        <div class="autocomplete-table-head">
          <span>完整词根/词组</span><span>原始扩展数量</span><span>缓存状态</span><span>最近观察</span>
        </div>
        <article v-for="item in rootExpansionLibrary.roots" :key="item.root">
          <strong>{{ item.root }}</strong>
          <span>{{ item.expansions.length }} 项</span>
          <span>{{ item.stale ? "待下次分析刷新" : "24 小时内有效" }}</span>
          <span>{{ formatChinaDateTime(item.captured_at) }}</span>
          <small>
            系统分析命中该根词 {{ item.system_input_hit_count }} 次（不是买家搜索量）
          </small>
          <div class="root-expansion-list">
            <p v-for="expansion in item.expansions" :key="`${item.root}-${expansion.rank}-${expansion.phrase}`">
              <b>第 {{ expansion.rank }} 项</b> {{ expansion.phrase }}
            </p>
          </div>
        </article>
      </div>
      <p v-else class="product-fact-empty">
        {{ rootExpansionLibraryLoading ? "正在读取本地词根/词组扩展库…" : "暂无匹配的扩展证据；首次被一键分析实际输入完整词根或词组后才会入库。" }}
      </p>
    </section>

    <div
      v-if="factConfirmationOpen && factRecommendation"
      class="fact-modal-backdrop"
      @click.self="closeProductFactConfirmation"
    >
      <section
        class="fact-modal-dialog fact-confirm-dialog"
        role="dialog"
        aria-modal="true"
        :aria-busy="factSaving"
      >
        <div>
          <p>HUMAN PRODUCT FACT CONFIRMATION</p>
          <h3>人工确认商品事实</h3>
        </div>
        <article>
          <span>系统建议与人工确认边界</span>
          <strong>{{ factRecommendation.reason }}</strong>
        </article>
        <p class="fact-confirm-hint">
          填写会被南非站内搜索使用的英文短语，例如购买类型 <b>compressed sofa</b>；若只是不可见的工艺或出货状态，
          请另选“结构形态”并填写 <b>vacuum compressed</b>。每条最多 6 个词，不要标点；只确认你能从供应商资料或实物确定的事实。
        </p>
        <div class="fact-draft-list">
          <div v-for="(fact, index) in factDrafts" :key="index" class="fact-draft-row">
            <select v-model="fact.fact_type" :disabled="factSaving">
              <option value="product_type">商品类型</option>
              <option value="construction">结构形态</option>
              <option value="material">材质</option>
              <option value="function">功能</option>
              <option value="packaging">包装形态</option>
              <option value="usage">使用场景</option>
            </select>
            <input v-model="fact.fact_term" :disabled="factSaving" maxlength="100" placeholder="英文事实词 compressed sofa" />
            <input v-model="fact.statement" :disabled="factSaving" maxlength="500" placeholder="可选说明或供应商依据" />
            <button type="button" :disabled="factSaving || factDrafts.length === 1" @click="removeFactDraft(index)">移除</button>
          </div>
        </div>
        <button v-if="factDrafts.length < 6" type="button" class="fact-add-button" :disabled="factSaving" @click="addFactDraft">
          添加另一条事实
        </button>
        <ul>
          <li>确认后写入当前 PLID 的商品事实档案，记录确认人、时间、当时标题与主图；不会调用外部图搜图服务。</li>
          <li>系统会重新读取 Takealot 根词扩展与搜索页，只有相关性和位置门槛通过后才形成标题建议。</li>
          <li>人工事实不等于排名保证；修改标题后仍必须同词复采，报告实际前移、持平或后移。</li>
        </ul>
        <p v-if="factModalError" class="fact-modal-error" role="alert">{{ factModalError }}</p>
        <div class="fact-modal-actions">
          <button type="button" :disabled="factSaving" @click="closeProductFactConfirmation">取消</button>
          <button type="button" class="primary" :disabled="factSaving" @click="confirmProductFacts">
            {{ factSaving ? "正在保存并重新验证…" : "我确认事实准确并重新验证" }}
          </button>
        </div>
      </section>
    </div>

    <div
      v-if="factRevocationTarget"
      class="fact-modal-backdrop"
      @click.self="factRevocationTarget = null"
    >
      <section class="fact-modal-dialog fact-revoke-dialog" role="dialog" aria-modal="true">
        <div><p>PRODUCT FACT ARCHIVE</p><h3>停用商品事实</h3></div>
        <article>
          <span>将停止用于新分析的事实</span>
          <strong>{{ factRevocationTarget.fact_term }}</strong>
        </article>
        <textarea v-model="factRevocationReason" maxlength="500" placeholder="填写停用原因，例如供应商资料已更正" />
        <p class="fact-confirm-hint">记录会保留在档案中；现有分析仍是历史快照，请随后重新验证定位。</p>
        <div class="fact-modal-actions">
          <button type="button" @click="factRevocationTarget = null">取消</button>
          <button type="button" class="primary" :disabled="factSaving" @click="confirmFactRevocation">确认停用并留痕</button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.ranking-page { display: grid; gap: 18px; color: #18221c; overflow-anchor: none; }
.method-banner { display: grid; grid-template-columns: minmax(0, .88fr) minmax(440px, 1.12fr); align-items: start; gap: 28px; padding: 24px 28px; border: 1px solid #c8d4cb; border-radius: 18px; background: linear-gradient(135deg, #f5f7ed, #e4eee8); }
.method-eyebrow, .section-heading p, .rail-title p, .first-run p { margin: 0 0 5px; color: #64746a; font-size: 11px; font-weight: 800; letter-spacing: .14em; }
.method-banner h2, .section-heading h3, .rail-title h3, .first-run h3 { margin: 0; }
.method-banner-copy { min-width: 0; padding-top: 3px; }
.method-intro { display: block; max-width: 620px; margin-top: 10px; color: #536158; line-height: 1.7; }
.method-context { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 15px; }
.method-context span { padding: 6px 9px; border: 1px solid rgba(117, 139, 124, .28); border-radius: 999px; color: #4d6256; background: rgba(255, 255, 255, .55); font-size: 11px; font-weight: 750; }
.method-overview { display: grid; min-width: 0; gap: 11px; }
.method-model-route { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 9px; }
.method-model-route article { display: grid; min-width: 0; gap: 2px; padding: 11px 13px; border: 1px solid rgba(112, 137, 121, .28); border-radius: 11px; background: rgba(255, 255, 255, .66); }
.method-model-route article span { color: #748077; font-size: 10px; font-weight: 850; letter-spacing: .08em; text-transform: uppercase; }
.method-model-route article strong { overflow: hidden; color: #294f3b; text-overflow: ellipsis; white-space: nowrap; }
.method-model-route article small { overflow-wrap: anywhere; color: #68756d; line-height: 1.35; }
.method-model-arrow { color: #76907f; font-size: 17px; font-weight: 800; }
.method-guardrail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; gap: 8px; }
.method-guardrail-grid > div { display: grid; align-content: start; gap: 6px; min-width: 0; padding: 11px 12px; border: 1px solid rgba(112, 137, 121, .24); border-radius: 10px; background: rgba(247, 250, 247, .7); }
.method-guardrail-grid dd { display: grid; gap: 2px; min-width: 0; }
.method-guardrail-grid dd strong { color: #2d503e; font-size: 13px; }
.method-guardrail-grid dd span { color: #66736b; font-size: 11px; font-weight: 600; line-height: 1.4; }
.method-details { border-top: 1px solid rgba(108, 130, 114, .28); }
.method-details summary { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 2px 0; color: #40584a; cursor: pointer; font-size: 12px; font-weight: 800; }
.method-details summary small { color: #738078; font-weight: 600; }
.method-detail-content { display: grid; gap: 10px; padding: 11px 2px 2px; }
.method-detail-content article { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; color: #59675e; font-size: 12px; line-height: 1.55; }
.method-detail-content article strong { color: #354c3f; }
.method-detail-note { padding-top: 8px; border-top: 1px dashed rgba(108, 130, 114, .3); color: #6c776f; font-size: 11px; line-height: 1.55; }
dt { color: #738078; font-size: 12px; } dd { margin: 0; font-weight: 750; }
.error-banner, .config-note, .attempt-note, .running-note, .variant-projection-note { margin: 0; padding: 12px 16px; border-radius: 10px; }
.error-banner { color: #8e2f25; background: #fff0ed; border: 1px solid #efc2bb; }
.config-note { color: #755713; background: #fff8dc; border: 1px solid #ead58b; }
.attempt-note { color: #755713; background: #fff8e8; border: 1px solid #e7cea2; line-height: 1.6; }
.running-note { color: #285c47; background: #e8f4ed; border: 1px solid #b9d7c7; }
.variant-projection-note { color: #315f70; background: #edf6f8; border: 1px solid #bfd7de; line-height: 1.6; }
.eligibility-note { margin: 0; padding: 11px 15px; border: 1px solid #c9d9cf; border-radius: 10px; color: #355e49; background: #f0f6f2; font-size: 12px; line-height: 1.6; }
.batch-panel { display: grid; gap: 16px; padding: 20px; border: 1px solid #c7d5cc; border-radius: 16px; background: linear-gradient(135deg, #fbfcf8, #eef5f0); }
.batch-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.batch-heading p { margin: 0 0 4px; color: #6f7d74; font-size: 10px; font-weight: 850; letter-spacing: .12em; }
.batch-heading h3 { margin: 0; }
.batch-status { padding: 6px 10px; border-radius: 999px; color: #446052; background: #dfeae3; font-size: 11px; font-weight: 850; white-space: nowrap; }
.batch-status.running, .batch-status.queued { color: #fff; background: #2f7254; }
.batch-status.paused_after_error, .batch-status.interrupted { color: #7a4d21; background: #f5dfc8; }
.batch-status.stopped, .batch-status.paused { color: #285c47; background: #dceee3; }
.batch-status.completed { color: #fff; background: #376b82; }
.batch-loading, .batch-owner-note { margin: 0; color: #64736a; font-size: 12px; }
.batch-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.batch-metrics article { display: grid; align-content: start; gap: 5px; min-width: 0; padding: 13px; border: 1px solid #d4dfd7; border-radius: 10px; background: rgba(255, 255, 255, .8); }
.batch-metrics span { color: #6f7d74; font-size: 10px; font-weight: 850; letter-spacing: .07em; text-transform: uppercase; }
.batch-metrics strong { color: #244f3b; font-size: 17px; }
.batch-metrics small, .batch-policy-note { color: #6c7971; font-size: 11px; line-height: 1.55; }
.batch-store-detail { color: #4f6257; font-size: 12px; }
.batch-store-detail summary { font-weight: 800; cursor: pointer; }
.batch-store-detail > div { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.batch-store-detail span { display: grid; gap: 3px; padding: 9px 11px; border-radius: 8px; background: #fff; }
.batch-progress-panel { display: grid; gap: 9px; padding: 13px 15px; border: 1px solid #c9d8cf; border-radius: 10px; background: #fff; }
.batch-progress-copy { display: flex; justify-content: space-between; gap: 16px; color: #53645a; font-size: 12px; }
.batch-progress-copy strong { color: #28583f; }
.batch-progress-track { height: 8px; overflow: hidden; border-radius: 999px; background: #dfe8e2; }
.batch-progress-track span { display: block; height: 100%; border-radius: inherit; background: #347355; transition: width .25s ease; }
.batch-progress-panel > small { color: #68766e; line-height: 1.5; }
.batch-progress-panel .batch-error { color: #8a4c26; }
.batch-progress-panel .batch-resume-note { color: #285c47; font-weight: 750; }
.batch-actions { display: flex; flex-wrap: wrap; gap: 9px; }
.batch-actions button { padding: 10px 14px; border: 1px solid #b9c8bf; border-radius: 9px; color: #40584b; background: #fff; font-weight: 800; cursor: pointer; }
.batch-actions button:disabled { cursor: not-allowed; opacity: .55; }
.batch-actions .batch-start-button { border-color: #2f7254; color: #fff; background: #2f7254; }
.batch-actions .batch-stop-button { border-color: #be8a72; color: #874a2c; background: #fff8f3; }
.batch-policy-note { margin: 0; padding-top: 11px; border-top: 1px solid #d5dfd8; }
.ranking-layout { display: grid; grid-template-columns: minmax(260px, 325px) minmax(0, 1fr); gap: 18px; align-items: start; }
.product-rail, .ranking-detail > section, .detail-loading { border: 1px solid #d9dfdb; border-radius: 16px; background: #fff; }
.product-rail { position: sticky; top: 18px; max-height: calc(100vh - 36px); overflow: auto; padding: 16px; }
.rail-title, .section-heading { display: flex; justify-content: space-between; align-items: end; gap: 16px; }
.rail-title > span, .section-heading > span { color: #66736b; font-size: 12px; font-weight: 700; }
.product-rail > input { box-sizing: border-box; width: 100%; margin: 14px 0; padding: 10px 12px; border: 1px solid #ccd4cf; border-radius: 9px; }
.rail-filters { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: -5px 0 12px; }
.rail-filters label { display: grid; gap: 4px; color: #6a766f; font-size: 11px; font-weight: 800; }
.rail-filters select { min-width: 0; padding: 8px; border: 1px solid #ccd4cf; border-radius: 8px; color: #405047; background: #fff; }
.rail-warning { color: #9a522f !important; font-weight: 800; }
.product-row { width: 100%; display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; padding: 10px; border: 1px solid transparent; border-bottom-color: #e9edea; background: transparent; text-align: left; cursor: pointer; }
.product-row.active { border-color: #4c8068; border-radius: 10px; background: #edf5f0; }
.product-row img, .image-fallback { width: 52px; height: 52px; border-radius: 8px; object-fit: contain; background: #f1f3f1; }
.image-fallback { display: grid; place-items: center; color: #929c96; font-size: 8px; }
.product-copy { min-width: 0; display: grid; gap: 4px; }
.product-copy strong { overflow: hidden; font-size: 12px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.product-copy small { color: #78847c; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.product-copy .family-parameter-summary { color: #315f70; font-weight: 750; }
.product-copy em { width: fit-content; color: #747e78; font-size: 10px; font-style: normal; }
.product-copy em.completed { color: #26704e; } .product-copy em.failed { color: #a14036; }
.ranking-detail { display: grid; gap: 16px; min-width: 0; }
.ranking-detail > section, .detail-loading { padding: 22px; }
.product-hero { display: grid; grid-template-columns: 110px minmax(0, 1fr) auto; gap: 20px; align-items: center; }
.product-hero > img, .hero-fallback { width: 110px; height: 110px; object-fit: contain; border-radius: 12px; background: #f3f4f1; }
.hero-fallback { display: grid; place-items: center; color: #98a199; font-size: 11px; }
.hero-copy p { margin: 0 0 6px; color: #78857c; font-size: 12px; }.hero-copy h2 { margin: 0; font-size: 22px; line-height: 1.35; }
.ownership-note { display: block; margin-top: 8px; color: #50705e; font-size: 12px; line-height: 1.5; }
.selected-variant-parameter { display: block; margin-top: 8px; color: #315f70; font-size: 12px; line-height: 1.5; }
.family-variants { margin-top: 9px; color: #50675a; font-size: 11px; }
.family-variants summary { cursor: pointer; font-weight: 800; }
.family-variants button { display: grid; width: 100%; gap: 3px; margin-top: 6px; padding: 8px 10px; border: 1px solid #d4dfd8; border-radius: 8px; color: #42564a; background: #fff; text-align: left; cursor: pointer; }
.family-variants button.active { border-color: #4c8068; background: #edf5f0; }
.family-variants button span { color: #6b7870; }
.family-variants button em { color: #315f70; font-style: normal; }
.blocked-note { display: block; margin-top: 8px; color: #9d3c31; font-size: 12px; }
.analyze-button { padding: 12px 16px; border: 0; border-radius: 10px; color: #fff; background: #235c45; font-weight: 800; cursor: pointer; }
.analyze-button:disabled { cursor: not-allowed; opacity: .55; }
.decision-parameter-section { display: grid; gap: 15px; border-color: #c9d5df !important; background: linear-gradient(145deg, #fbfdff, #eef4f7) !important; }
.decision-parameter-heading { align-items: center; }
.decision-parameter-heading > span { padding: 6px 10px; border-radius: 999px; color: #72532f; background: #f4e4ce; font-size: 11px; font-weight: 850; }
.decision-parameter-heading > span.confirmed { color: #285c47; background: #dceee3; }
.decision-parameter-empty, .decision-parameter-result { margin: 0; color: #53645b; font-size: 12px; line-height: 1.72; }
.decision-parameter-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.decision-parameter-grid article { display: grid; gap: 11px; padding: 14px; border: 1px solid #cfdae0; border-radius: 11px; background: rgba(255, 255, 255, .82); }
.decision-parameter-grid article.positive { border-color: #6fa184; box-shadow: inset 3px 0 #397557; }
.decision-parameter-grid article.negative { border-color: #c9c7bd; box-shadow: inset 3px 0 #918b78; }
.decision-parameter-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.decision-parameter-title span { padding: 4px 7px; border-radius: 999px; color: #45677a; background: #e5eef3; font-size: 10px; font-weight: 850; }
.decision-parameter-title strong { font-size: 17px; }
.decision-parameter-grid article > small { color: #68766e; line-height: 1.55; }
.decision-parameter-grid article > small.recommended { color: #7b5421; }
.decision-parameter-choice { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.decision-parameter-choice label { display: flex; align-items: center; gap: 7px; padding: 9px 10px; border: 1px solid #d4ddd8; border-radius: 8px; color: #47584e; background: #fff; font-size: 12px; font-weight: 750; cursor: pointer; }
.decision-parameter-choice input { margin: 0; accent-color: #2f7254; }
.decision-parameter-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 11px; }
.decision-parameter-actions button { padding: 10px 14px; border: 0; border-radius: 9px; color: #fff; background: #355f74; font-weight: 800; cursor: pointer; }
.decision-parameter-actions button:disabled { cursor: not-allowed; opacity: .55; }
.decision-parameter-actions span { color: #66766d; font-size: 11px; }
.decision-parameter-result { padding: 10px 13px; border-radius: 8px; color: #315e49; background: #e7f2eb; }
.identity-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 0 !important; border: 0 !important; background: transparent !important; }
.identity-grid article { padding: 17px; border: 1px solid #d9dfdb; border-radius: 14px; background: #fff; }
.identity-grid p { margin: 0 0 7px; color: #758179; font-size: 11px; font-weight: 800; text-transform: uppercase; }
.identity-grid h3 { margin: 0 0 5px; }.identity-grid span, .identity-grid small { display: block; color: #68746c; font-size: 12px; line-height: 1.5; }.identity-grid small { margin-top: 5px; color: #8a5a34; }
.fact-recommendation-section { display: grid; gap: 15px; border-color: #d8ccb1 !important; background: linear-gradient(145deg, #fffdf7, #f7f1e3) !important; }
.product-fact-section, .autocomplete-library-section { display: grid; gap: 14px; padding: 20px; border: 1px solid #c9d8cf; border-radius: 14px; background: linear-gradient(145deg, #fbfdfb, #edf5ef); }
.product-fact-heading, .autocomplete-library-heading { align-items: center; }
.product-fact-heading > span { padding: 6px 10px; border-radius: 999px; color: #315e49; background: #dceee2; font-size: 11px; font-weight: 800; }
.autocomplete-library-policy, .product-fact-empty, .fact-confirm-hint { margin: 0; color: #596a60; font-size: 12px; line-height: 1.7; }
.product-fact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.product-fact-grid article { display: grid; gap: 7px; padding: 13px; border: 1px solid #d2ddd6; border-radius: 10px; background: rgba(255, 255, 255, .78); }
.product-fact-grid article.applied { border-color: #8ab49c; box-shadow: inset 3px 0 #4c8a67; }
.product-fact-grid article.archived { opacity: .72; }
.product-fact-grid article > div { display: flex; justify-content: space-between; gap: 8px; }
.product-fact-grid span, .product-fact-grid em { padding: 3px 6px; border-radius: 999px; font-size: 10px; font-style: normal; font-weight: 800; }
.product-fact-grid span { color: #315e49; background: #e2efe6; }
.product-fact-grid em { color: #75664d; background: #eee8dc; }
.product-fact-grid strong { font-size: 16px; }
.product-fact-grid p { margin: 0; color: #516158; font-size: 12px; }
.product-fact-grid small { color: #68766d; line-height: 1.5; }
.product-fact-grid button { width: fit-content; padding: 6px 9px; border: 1px solid #c7d0ca; border-radius: 7px; color: #53645a; background: #fff; font-size: 11px; font-weight: 750; cursor: pointer; }
.fact-recommendation-heading { align-items: center; }
.fact-recommendation-status { padding: 6px 10px; border-radius: 999px; color: #6f695d !important; background: #eee9de; font-size: 11px !important; font-weight: 800 !important; }
.fact-recommendation-status.recommended { color: #7d521d !important; background: #f4dfb9; }
.fact-recommendation-reason { display: grid; gap: 7px; padding: 14px 16px; border-left: 4px solid #9a6f2e; border-radius: 8px; background: rgba(255, 255, 255, .72); }
.fact-recommendation-reason p { margin: 0; color: #7d6e54; font-size: 10px; font-weight: 850; letter-spacing: .08em; text-transform: uppercase; }
.fact-recommendation-reason strong { color: #4d412f; font-size: 13px; line-height: 1.7; }
.fact-recommendation-actions { display: flex; align-items: center; gap: 12px; }
.fact-recommendation-actions > span { color: #776a55; font-size: 12px; }
.manual-fact-confirm-open { padding: 10px 14px; border: 1px solid #397557; border-radius: 9px; color: #245f45; background: #e2f1e7; font-weight: 800; cursor: pointer; }
.fact-modal-backdrop { position: fixed; z-index: 80; inset: 0; display: grid; place-items: center; padding: 22px; background: rgba(20, 28, 23, .58); }
.fact-modal-dialog { box-sizing: border-box; display: grid; gap: 16px; width: min(680px, 100%); max-height: calc(100vh - 44px); overflow: auto; padding: 24px; border: 1px solid #d5c29e; border-radius: 16px; background: #fffdf8; box-shadow: 0 24px 70px rgba(25, 30, 26, .24); }
.fact-modal-dialog > div:first-child p { margin: 0 0 5px; color: #806a43; font-size: 10px; font-weight: 850; letter-spacing: .1em; }
.fact-modal-dialog h3 { margin: 0; }
.fact-modal-dialog article { display: grid; gap: 7px; padding: 14px; border-left: 4px solid #956722; border-radius: 8px; background: #f8edda; }
.fact-modal-dialog article span { color: #806a43; font-size: 10px; font-weight: 850; text-transform: uppercase; }
.fact-modal-dialog article strong { color: #4f412d; font-size: 13px; line-height: 1.65; }
.fact-modal-dialog ul { display: grid; gap: 9px; margin: 0; padding-left: 21px; color: #59645d; font-size: 12px; line-height: 1.65; }
.fact-modal-error { margin: 0; padding: 10px 12px; border: 1px solid #e0a69c; border-radius: 8px; color: #8e2f25; background: #fff0ed; font-size: 12px; line-height: 1.55; }
.fact-modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
.fact-modal-actions button { padding: 10px 14px; border: 1px solid #c9cec9; border-radius: 9px; color: #4c5951; background: #fff; font-weight: 800; cursor: pointer; }
.fact-modal-actions button.primary { border-color: #83591f; color: #fff; background: #83591f; }
.fact-draft-list { display: grid; gap: 10px; }
.fact-draft-row { display: grid; grid-template-columns: 130px minmax(160px, 1fr) minmax(180px, 1.25fr) auto; gap: 8px; }
.fact-draft-row select, .fact-draft-row input, .fact-revoke-dialog textarea, .autocomplete-library-heading input { box-sizing: border-box; width: 100%; padding: 10px 11px; border: 1px solid #cbd5ce; border-radius: 8px; background: #fff; }
.fact-draft-row button, .fact-add-button { padding: 9px 11px; border: 1px solid #cbd5ce; border-radius: 8px; color: #53645a; background: #fff; font-weight: 750; cursor: pointer; }
.fact-add-button { width: fit-content; }
.fact-revoke-dialog textarea { min-height: 100px; resize: vertical; }
.autocomplete-library-heading form { display: flex; gap: 8px; width: min(440px, 100%); }
.autocomplete-library-heading button { padding: 9px 12px; border: 0; border-radius: 8px; color: #fff; background: #315e49; font-weight: 800; white-space: nowrap; cursor: pointer; }
.autocomplete-phrase-table { display: grid; gap: 7px; }
.autocomplete-table-head, .autocomplete-phrase-table article { display: grid; grid-template-columns: minmax(220px, 1.5fr) 130px 130px 170px; gap: 12px; align-items: center; }
.autocomplete-table-head { padding: 0 12px; color: #6f7d74; font-size: 10px; font-weight: 850; letter-spacing: .06em; text-transform: uppercase; }
.autocomplete-phrase-table article { padding: 12px; border: 1px solid #d4dfd7; border-radius: 9px; background: rgba(255, 255, 255, .8); }
.autocomplete-phrase-table article > span { color: #41554a; font-size: 12px; }
.autocomplete-phrase-table article > small, .autocomplete-phrase-table details { grid-column: 1 / -1; color: #68766d; font-size: 11px; }
.autocomplete-phrase-table summary { color: #315e49; font-weight: 800; cursor: pointer; }
.autocomplete-phrase-table details p { margin: 6px 0 0; }
.root-expansion-list { grid-column: 1 / -1; display: grid; gap: 5px; padding-top: 5px; border-top: 1px dashed #d5dfd8; }
.root-expansion-list p { margin: 0; color: #43564b; font-size: 12px; line-height: 1.55; }
.root-expansion-list b { display: inline-block; min-width: 58px; color: #315e49; }
.keyword-section, .title-review, .title-score-section, .history-section { display: grid; gap: 16px; }
.title-score-overview { display: flex; flex-wrap: wrap; gap: 8px; }
.title-score-overview span { padding: 7px 10px; border-radius: 999px; color: #315a46; background: #e8f1eb; font-size: 12px; font-weight: 800; }
.title-score-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.title-score-grid article { padding: 13px; border: 1px solid #dfe5e1; border-radius: 11px; background: #fbfcfb; }
.title-score-grid header { display: flex; justify-content: space-between; gap: 12px; }
.title-score-grid header span { color: #2f7254; font-weight: 900; }
.title-score-grid p { margin: 8px 0 0; color: #68746c; font-size: 12px; line-height: 1.5; }
.score-limitations { color: #737e77; line-height: 1.6; }
.root-expansion-selection-audit { padding: 14px; border: 1px solid #cbd9d0; border-radius: 11px; background: #f7faf8; }
.root-expansion-selection-audit > summary { color: #285a43; font-weight: 850; cursor: pointer; }
.root-expansion-selection-audit > p { margin: 10px 0; color: #637169; font-size: 12px; line-height: 1.65; }
.root-expansion-selection-grid { display: grid; gap: 10px; }
.root-expansion-selection-grid > article { display: grid; gap: 7px; padding: 12px; border: 1px solid #d8e1db; border-radius: 9px; background: #fff; }
.root-expansion-selection-grid header { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.root-expansion-selection-grid header span { padding: 2px 7px; border-radius: 999px; color: #315e49; background: #e8f2ec; font-size: 10px; font-weight: 850; }
.root-expansion-selection-grid header small, .root-expansion-selection-grid > article > small { color: #6c7971; }
.root-expansion-decision { display: grid; grid-template-columns: 58px minmax(150px, 1fr) minmax(128px, auto); gap: 6px 10px; align-items: center; padding: 8px 10px; border-left: 3px solid #39765a; border-radius: 7px; background: #f3f8f5; }
.root-expansion-decision.rejected_irrelevant { border-left-color: #bb6a3b; background: #fff8f2; }
.root-expansion-decision b { color: #607168; font-size: 11px; }.root-expansion-decision em { color: #32644d; font-size: 11px; font-style: normal; font-weight: 850; }
.root-expansion-decision.rejected_irrelevant em { color: #9a552f; }.root-expansion-decision small { grid-column: 2 / -1; color: #6b7770; }.root-expansion-decision mark { grid-column: 2 / -1; width: fit-content; padding: 2px 6px; border-radius: 6px; color: #24583f; background: #dfeee5; font-size: 10px; font-weight: 850; }
.keyword-list { display: grid; gap: 10px; }
.keyword-card { padding: 16px; border: 1px solid #d5ded8; border-left: 4px solid #35765a; border-radius: 11px; background: #fbfdfb; }
.keyword-card.opportunity { border-left-color: #8f6b24; background: #fffdf4; }
.keyword-card.comparison_resample { border-left-color: #50708c; background: #f7fbfe; }
.keyword-card.rejected_irrelevant, .keyword-card.model_low_confidence { border-left-color: #bb6a3b; background: #fffaf4; }
.keyword-main { display: flex; justify-content: space-between; gap: 18px; }
.keyword-main > div { display: grid; justify-items: start; gap: 4px; }.keyword-main a { color: #194f3a; font-size: 17px; font-weight: 850; }.keyword-main span { color: #5d6c63; font-size: 12px; }
.strategy-badge { padding: 3px 7px; border-radius: 999px; color: #fff !important; background: #35765a; font-size: 10px !important; font-weight: 800; letter-spacing: .04em; }
.source-channel-badge { margin-left: 5px; padding: 3px 7px; border: 1px solid #b8cdc0; border-radius: 999px; color: #315e49 !important; background: #f3f8f4; font-size: 10px !important; font-weight: 800; }
.keyword-card.opportunity .strategy-badge { background: #8f6b24; }
.keyword-card.comparison_resample .strategy-badge { background: #50708c; }
.keyword-card.rejected_irrelevant .strategy-badge, .keyword-card.model_low_confidence .strategy-badge { background: #a85d35; }
.query-source { color: #5f7166; font-weight: 700; }
.query-path { color: #7a6a4b; font-weight: 650; }
.keyword-main > strong { color: #235c45; white-space: nowrap; }.keyword-position { display: grid; gap: 4px; text-align: right; }.keyword-position > span { color: #194f3a; font-size: 15px; font-weight: 850; }.keyword-position > small { color: #65746b; font-size: 12px; font-weight: 700; }.keyword-card dl { display: flex; gap: 28px; margin: 14px 0 8px; }.keyword-card dl div { display: grid; gap: 3px; }
.keyword-card p { margin: 7px 0; color: #4e5c53; }.keyword-card small, .position-notice, .causality-note { color: #6e7a72; line-height: 1.6; }
.position-notice, .causality-note { margin: 0; padding: 12px 14px; border-radius: 9px; background: #f3f5f3; font-size: 12px; }
.current-title-panel { display: grid; gap: 7px; padding: 14px 16px; border: 1px solid #dbe2dd; border-radius: 11px; background: #f7f9f7; }
.current-title-panel p { margin: 0; color: #748078; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.current-title-panel strong { line-height: 1.55; }
.current-title-panel small { color: #8a5a34; line-height: 1.55; }
.title-strategy-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; }
.title-strategy-card { display: grid; align-content: start; gap: 13px; min-width: 0; padding: 18px; border: 1px solid #cddbd2; border-top: 3px solid #2e7254; border-radius: 13px; background: linear-gradient(180deg, #fbfdfb, #f5f8f6); }
.title-strategy-card.hot_term_coverage { border-color: #c9d8df; border-top-color: #3a7085; background: linear-gradient(180deg, #fbfdfe, #f2f7f9); }
.title-strategy-card.adjacent_opportunity { border-color: #e1d3ad; border-top-color: #98712b; background: linear-gradient(180deg, #fffdf7, #fbf6e9); }
.title-strategy-card.unavailable { filter: saturate(.72); }
.title-strategy-card header { display: flex; align-items: center; gap: 11px; }
.title-strategy-card header p { margin: 0 0 2px; color: #748078; font-size: 10px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
.title-strategy-card h4 { margin: 0; font-size: 15px; }
.strategy-number { display: grid; flex: 0 0 34px; height: 34px; place-items: center; border-radius: 10px; color: #fff; background: #2e7254; font-size: 12px; font-weight: 850; letter-spacing: .05em; }
.hot_term_coverage .strategy-number { background: #3a7085; }
.adjacent_opportunity .strategy-number { background: #98712b; }
.suggested-title { min-height: 4.65em; color: #16251d; line-height: 1.55; overflow-wrap: anywhere; }
.title-strategy-card.unavailable .suggested-title { color: #7b837e; }
.strategy-explanation { margin: 0; color: #526159; font-size: 12px; line-height: 1.65; }
.strategy-boundary { color: #78847d; line-height: 1.55; }
.strategy-evidence { display: grid; gap: 7px; padding-top: 11px; border-top: 1px solid rgba(99, 119, 107, .18); }
.strategy-evidence > span { color: #6e7b73; font-size: 10px; font-weight: 850; letter-spacing: .08em; text-transform: uppercase; }
.strategy-evidence > div { display: flex; flex-wrap: wrap; gap: 6px; }
.strategy-evidence em { padding: 4px 7px; border-radius: 999px; color: #315c48; background: #e7f0ea; font-size: 11px; font-style: normal; font-weight: 700; }
.hot_term_coverage .strategy-evidence em { color: #315e70; background: #e4eff3; }
.adjacent_opportunity .strategy-evidence em { color: #72551d; background: #f4e9c9; }
.strategy-evidence small { color: #818a84; }
.title-format-note { padding: 10px 13px; border-left: 3px solid #617c6c; color: #5e6d64; background: #f5f7f5; font-size: 12px; }
.matched-strategy-note { display: grid; gap: 6px; padding: 13px 15px; border: 1px solid #cbd9e0; border-radius: 10px; background: #f3f8fa; }
.matched-strategy-note p { margin: 0; color: #607782; font-size: 10px; font-weight: 850; letter-spacing: .08em; text-transform: uppercase; }
.matched-strategy-note strong { color: #315e70; }
.matched-strategy-note span { color: #384b43; line-height: 1.55; }
.matched-strategy-note small { color: #6c7d75; line-height: 1.5; }
.comparison-warning { padding: 10px 13px; border-left: 3px solid #a66a2d; color: #75502d; background: #fff8ef; font-size: 12px; }
.title-review > p { margin: 0; line-height: 1.7; }.movement-list, .history-list { display: flex; flex-wrap: wrap; gap: 8px; }.movement-list span, .history-list span { padding: 7px 10px; border-radius: 999px; background: #e8f1eb; color: #315a46; font-size: 12px; }
.first-run { text-align: center; padding: 70px 20px !important; }.first-run span, .empty-state, .detail-loading { color: #748077; }
@media (max-width: 1050px) { .ranking-layout { grid-template-columns: 1fr; }.product-rail { position: static; max-height: 420px; }.method-banner { grid-template-columns: 1fr; }.batch-metrics, .title-score-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.product-hero { grid-template-columns: 90px minmax(0, 1fr); }.analyze-button { grid-column: 1 / -1; }.identity-grid { grid-template-columns: 1fr; }.autocomplete-table-head, .autocomplete-phrase-table article { grid-template-columns: minmax(190px, 1fr) 110px 110px 150px; } }
@media (max-width: 780px) { .method-guardrail-grid { grid-template-columns: 1fr; }.title-strategy-grid, .product-fact-grid, .decision-parameter-grid { grid-template-columns: 1fr; }.decision-parameter-choice { grid-template-columns: 1fr; }.suggested-title { min-height: 0; }.fact-draft-row { grid-template-columns: 1fr; }.autocomplete-library-heading { align-items: stretch; flex-direction: column; }.autocomplete-table-head { display: none; }.autocomplete-phrase-table article { grid-template-columns: 1fr 1fr; }.autocomplete-phrase-table article strong { grid-column: 1 / -1; } }
@media (max-width: 650px) { .method-banner { padding: 18px; }.method-model-route { grid-template-columns: 1fr; }.method-model-arrow { display: none; }.method-details summary { align-items: flex-start; flex-direction: column; gap: 3px; }.method-detail-content article { grid-template-columns: 1fr; gap: 3px; }.batch-heading, .batch-progress-copy { flex-direction: column; }.batch-metrics, .batch-store-detail > div { grid-template-columns: 1fr; }.product-hero { grid-template-columns: 1fr; }.keyword-main { flex-direction: column; }.keyword-position { text-align: left; }.keyword-card dl { flex-wrap: wrap; }.fact-modal-actions { flex-direction: column-reverse; }.fact-modal-actions button { width: 100%; } }
</style>
