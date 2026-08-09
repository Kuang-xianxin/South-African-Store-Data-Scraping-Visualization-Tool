<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import {
  analyzeSearchRanking,
  ApiRequestError,
  fetchSearchRankingDetail,
  fetchSearchRankingProducts,
} from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { formatChinaDateTime } from "../time";
import type {
  SearchRankingDetailPayload,
  SearchRankingKeywordResult,
  SearchRankingListPayload,
  SearchRankingProduct,
  SearchRankingTitleStrategy,
  SearchRankingTitleStrategyKey,
} from "../types";

const props = defineProps<{
  canOperate: boolean;
  onPermissionDenied?: (message: string) => void;
}>();

const listPayload = ref<SearchRankingListPayload | null>(null);
const detail = ref<SearchRankingDetailPayload | null>(null);
const selectedOfferId = ref("");
const search = ref("");
const loadingList = ref(false);
const loadingDetail = ref(false);
const analyzing = ref(false);
const error = ref("");
const failedImages = ref(new Set<string>());

const products = computed(() => listPayload.value?.items ?? []);
const eligibility = computed(() => listPayload.value?.eligibility ?? null);
const filteredProducts = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase();
  if (!needle) return products.value;
  return products.value.filter((item) =>
    [item.title, item.sku, item.offer_id, item.productline_id]
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(needle)),
  );
});
const selectedProduct = computed(() => detail.value?.product ?? null);
const analysis = computed(() => detail.value?.analysis ?? null);
const currentTitleChangedSinceAnalysis = computed(() => {
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
const opportunityKeywords = computed(() =>
  analysis.value?.keywords.filter((item) => item.relevance_status === "opportunity") ?? [],
);
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
const platformSearchCollected = computed(() =>
  analysis.value?.keywords.some((item) => item.pages_scanned > 0) ?? false,
);
const providerFallbackSucceeded = computed(() => {
  const attempts = analysis.value?.provider_attempts ?? [];
  return platformSearchCollected.value
    && attempts.some((item) => item.status === "accepted")
    && attempts.some((item) => item.status !== "accepted");
});
const providerIdentityBlocked = computed(() => {
  const attempts = analysis.value?.provider_attempts ?? [];
  return !platformSearchCollected.value
    && attempts.some((item) =>
      ["identity_conflict", "cached_identity_conflict"].includes(item.status),
    );
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
          || "保留已验证核心短语的完整连续性并前置，卖点与参数后置，侧重词面相关性。",
      evidence_keywords: acceptedKeywords.value.map((item) => item.keyword),
    },
    hot_term_coverage: {
      label: "类目热词覆盖版",
      title: null,
      available: false,
      explanation: hasStrategyContract
        ? "本轮没有形成与连续词组版足够不同且证据完整的热词覆盖方案。"
        : "该历史记录尚未生成独立的类目热词覆盖方案；重新分析后才会按平台补全证据评估。",
      evidence_keywords: acceptedKeywords.value
        .filter((item) => item.validation_evidence.autocomplete_rank != null)
        .map((item) => item.keyword),
    },
    adjacent_opportunity: {
      label: "相邻需求蓝海版",
      title: null,
      available: false,
      explanation: hasStrategyContract
        ? "本轮没有通过实际命中与首页低竞争门槛的相邻需求方案。"
        : "旧记录中的相邻需求建议没有经过现行的实际命中与首页低竞争门槛，已安全停用；重新分析后再评估。",
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

onMounted(() => void loadProducts());

async function loadProducts(preferredOfferId = "") {
  loadingList.value = true;
  error.value = "";
  try {
    const payload = await fetchSearchRankingProducts();
    listPayload.value = payload;
    const next = payload.items.find((item) => item.offer_id === preferredOfferId)
      ?? payload.items.find((item) => item.latest_analysis?.status === "completed")
      ?? payload.items.find((item) => item.analyzable)
      ?? payload.items[0];
    if (next) await selectProduct(next.offer_id);
    else detail.value = null;
  } catch (caught) {
    error.value = errorMessage(caught, "搜索定位商品列表加载失败");
  } finally {
    loadingList.value = false;
  }
}

async function selectProduct(offerId: string) {
  selectedOfferId.value = offerId;
  loadingDetail.value = true;
  error.value = "";
  try {
    const payload = await fetchSearchRankingDetail(offerId);
    if (selectedOfferId.value === offerId) detail.value = payload;
  } catch (caught) {
    error.value = errorMessage(caught, "搜索定位详情加载失败");
  } finally {
    loadingDetail.value = false;
  }
}

async function runAnalysis() {
  const product = selectedProduct.value;
  if (!product) return;
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
    detail.value = await analyzeSearchRanking(product.offer_id);
    await refreshListSummary(product.offer_id);
  } catch (caught) {
    error.value = errorMessage(caught, "识别或排名采集失败");
    await selectProduct(product.offer_id);
  } finally {
    analyzing.value = false;
  }
}

async function refreshListSummary(offerId: string) {
  try {
    listPayload.value = await fetchSearchRankingProducts();
    selectedOfferId.value = offerId;
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
  if (item.relevance_status === "rejected_irrelevant") return "平台相关性未通过";
  if (item.relevance_status === "model_low_confidence") return "图片识别置信度不足";
  if (!item.found) return `前 ${item.pages_scanned} 页未找到`;
  return `第 ${item.page_number} 页 · 第 ${item.row_number} 行第 ${item.column_number} 列`;
}

function strategyLabel(item: SearchRankingKeywordResult) {
  if (item.relevance_status === "accepted") return "核心词";
  if (item.relevance_status === "opportunity") return "相邻需求蓝海";
  if (item.relevance_status === "comparison_resample") return "改后同词复采";
  return "未采用";
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
  if (evidence.autocomplete_rank && evidence.autocomplete_seed) {
    return `Takealot 补全 · 输入“${evidence.autocomplete_seed}”后的第 ${evidence.autocomplete_rank} 项${resampleSuffix}`;
  }
  return `图片独立识别的精准词${resampleSuffix}`;
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
    adjacent_opportunity: "相邻需求蓝海版",
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

function errorMessage(caught: unknown, fallback: string) {
  return caught instanceof ApiRequestError ? caught.message : fallback;
}
</script>

<template>
  <div class="ranking-page">
    <section class="method-banner">
      <div>
        <p>IMAGE IDENTITY → TAKEALOT AUTOCOMPLETE → FIRST-PAGE FIT → ORGANIC POSITION</p>
        <h2>图片独立识别，再由平台补全与完整第一页共同验词</h2>
        <span>
          只有当前店铺授权 Seller Offers 中仍为 buyable、明确有可售库存且快照新鲜的链接才会进入；
          模型阶段看不到主标题和 SKU，识别完成后才用标题交叉核对，并用 Takealot 搜索框补全与自然结果验证。
          普通查看不会调用模型或访问平台。
        </span>
      </div>
      <dl v-if="listPayload">
        <div><dt>主服务</dt><dd>{{ listPayload.status.provider_label }} · {{ listPayload.status.primary_model }}</dd></div>
        <div v-if="listPayload.status.fallback_model">
          <dt>跨厂商备用</dt><dd>{{ listPayload.status.fallback_provider_label }} · {{ listPayload.status.fallback_model }}</dd>
        </div>
        <div><dt>单词最多扫描</dt><dd>{{ listPayload.status.max_pages }} 页</dd></div>
        <div>
          <dt>自然排名坐标</dt>
          <dd>默认相关性序列 · 平台游标页最多 36 个自然商品 · 页内四列坐标</dd>
        </div>
      </dl>
    </section>

    <p v-if="eligibility" class="eligibility-note">
      当前授权 Offer {{ eligibility.current_offer_count }} 条，严格在售 {{ eligibility.eligible_count }} 条；
      {{ eligibility.excluded_count }} 条已在模型调用前排除。最近完整刷新：
      {{ eligibility.latest_capture_at ? formatChinaDateTime(eligibility.latest_capture_at) : "暂无" }}，
      有效期 {{ eligibility.max_age_hours }} 小时。
    </p>

    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>

    <div class="ranking-layout">
      <aside class="product-rail">
        <div class="rail-title">
          <div><p>OWN BUYABLE OFFERS</p><h3>自有在售商品</h3></div>
          <span>{{ filteredProducts.length }} / {{ products.length }}</span>
        </div>
        <input v-model="search" type="search" placeholder="搜索标题、SKU、PLID" />
        <div v-if="loadingList" class="empty-state">正在读取本地商品…</div>
        <div v-else-if="!filteredProducts.length" class="empty-state">
          没有符合“自有、buyable、正数可售库存、快照新鲜”的商品
        </div>
        <template v-else>
          <button
            v-for="product in filteredProducts"
            :key="product.offer_id"
            class="product-row"
            :class="{ active: selectedOfferId === product.offer_id }"
            @click="selectProduct(product.offer_id)"
          >
            <img
              v-if="imageUrl(product)"
              :src="imageUrl(product)"
              :alt="product.title ?? product.sku ?? '商品图片'"
              @error="markImageFailed(product.image_url)"
            />
            <span v-else class="image-fallback">NO IMG</span>
            <span class="product-copy">
              <strong>{{ product.title || "未命名商品" }}</strong>
              <small>{{ product.sku || product.offer_id }} · PLID{{ product.productline_id || "—" }}</small>
              <small>可售 {{ product.available_stock }} · {{ formatChinaDateTime(product.captured_at) }}</small>
              <em :class="product.latest_analysis?.status ?? 'untracked'">
                {{
                  product.latest_analysis?.status === "completed"
                    ? "已有定位"
                    : product.latest_analysis?.status === "failed"
                      ? "上次失败"
                      : product.latest_analysis?.status === "running"
                        ? "采集中"
                        : "未定位"
                }}
              </em>
            </span>
          </button>
        </template>
      </aside>

      <main class="ranking-detail">
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
              <p>PLID{{ selectedProduct.productline_id || "—" }} · {{ selectedProduct.sku || selectedProduct.offer_id }}</p>
              <h2>{{ selectedProduct.title || "未命名商品" }}</h2>
              <span class="ownership-note">
                授权 Seller Offers 当前记录 · {{ selectedProduct.offer_status }} · 可售库存
                {{ selectedProduct.available_stock }} · {{ formatChinaDateTime(selectedProduct.captured_at) }}
              </span>
              <span v-if="!selectedProduct.analyzable" class="blocked-note">
                当前链接已不满足自有在售闸门，模型不会被调用。
              </span>
            </div>
            <button
              class="analyze-button"
              :disabled="analyzing || !selectedProduct.analyzable"
              @click="runAnalysis"
            >
              {{ analyzing ? "正在识别并逐页定位…" : analysis ? "重新验证定位" : "识别图片并定位" }}
            </button>
          </section>

          <p v-if="analyzing" class="running-note">
            系统会先再次确认链接仍自有在售，再独立识别图片、读取 Takealot 补全并逐词验证；同一主图优先复用模型结果。
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

          <template v-if="analysis">
            <section class="identity-grid">
              <article>
                <p>图片独立识别</p>
                <h3>{{ analysis.product_name || "未识别" }}</h3>
                <span>{{ analysis.category || "类别未知" }} · 模型未接收主标题或 SKU</span>
              </article>
              <article>
                <p>主标题交叉核对</p>
                <h3>{{ analysis.recognition?.title_reference_terms?.length ?? 0 }} 个一致短语</h3>
                <span>
                  {{ analysis.recognition?.title_reference_terms?.join(" / ") || "未找到可确认短语" }}
                  · 仅在图片识别完成后参考
                </span>
              </article>
              <article>
                <p>识别置信度</p>
                <h3>{{ confidenceLabel(analysis.confidence) }}</h3>
                <span>{{ providerLabel(analysis.provider) }} · {{ analysis.model }} · {{ analysis.vision_reused ? "复用缓存" : "本次调用" }}</span>
                <small v-if="providerFallbackSucceeded">
                  主服务未通过后已切换跨厂商备用，并继续完成平台搜索采集。
                </small>
                <small v-else-if="providerIdentityBlocked">
                  图片识别与当前标题的商品身份明显冲突，本轮已在平台搜索前停止。
                </small>
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

            <section class="keyword-section">
              <div class="section-heading">
                <div><p>PLATFORM-EVIDENCED QUERY STRATEGY</p><h3>搜索词策略与自然位置</h3></div>
                <span>
                  {{ acceptedKeywords.length }} 个核心词 · {{ opportunityKeywords.length }} 个相邻需求蓝海词 ·
                  {{ comparisonKeywords.length }} 个改后同词复采 ·
                  {{ rejectedKeywords.length }} 个未采用
                </span>
              </div>
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
                      <a :href="item.search_url" target="_blank" rel="noreferrer">{{ item.keyword }}</a>
                      <small class="query-source">{{ sourceLabel(item) }}</small>
                      <span v-if="!item.found">{{ resultLabel(item) }}</span>
                    </div>
                    <strong v-if="item.found" class="keyword-position">
                      <span>第 {{ item.page_number }} 页 · 第 {{ item.row_number }} 行 · 第 {{ item.column_number }} 列</span>
                      <small>
                        跨页自然排名 #{{ item.organic_rank }}（自然商品序列中的第 {{ item.organic_rank }} 个）
                      </small>
                    </strong>
                    <strong v-else-if="['accepted', 'opportunity', 'comparison_resample'].includes(item.relevance_status)">未进入扫描范围</strong>
                    <strong v-else>已拦截</strong>
                  </div>
                  <dl>
                    <div><dt>首页同类占比</dt><dd>{{ percent(item.relevance_score) }}</dd></div>
                    <div>
                      <dt>首页窄形态词命中</dt>
                      <dd>
                        {{ item.validation_evidence.matched_first_page_results ?? item.validation_evidence.matched_top_results ?? "—" }} /
                        {{ item.validation_evidence.evaluated_first_page_results ?? item.validation_evidence.evaluated_top_results ?? "—" }}
                      </dd>
                    </div>
                    <div><dt>平台返回商品数（供给规模）</dt><dd>{{ item.total_num_found ?? "—" }}</dd></div>
                    <div><dt>采集时间</dt><dd>{{ formatChinaDateTime(item.observed_at) }}</dd></div>
                  </dl>
                  <p>{{ item.validation_evidence.candidate_rationale || item.validation_evidence.reason }}</p>
                  <small v-if="item.validation_evidence.evaluated_first_page_results || item.validation_evidence.evaluated_top_results">
                    完整第一页自然商品中，
                    {{ item.validation_evidence.matched_first_page_results ?? item.validation_evidence.matched_top_results }} 个命中同类型判定词
                    {{ item.validation_evidence.validation_terms?.join(" / ") }}。
                    <template v-if="item.relevance_status === 'accepted'">多数同类型，纳入核心词。</template>
                    <template v-else-if="item.relevance_status === 'opportunity'">
                      本商品确实进入扫描范围，且按窄形态标题词判定的首页直接同类极少，作为相邻需求赛道单列观察。
                    </template>
                    <template v-else-if="item.relevance_status === 'comparison_resample'">
                      该词只用于复查已采用打法的原始排名基线，不会反向进入新标题建议。
                    </template>
                  </small>
                </article>
              </div>
              <p class="position-notice">
                核心词门槛：完整第一页至少 {{ percent(detail?.status.core_first_page_threshold ?? 0.6) }} 为同类型商品。
                相邻需求蓝海词必须来自 Takealot 搜索框补全，在前
                {{ detail?.status.opportunity_max_organic_rank ?? 72 }} 个自然位置内实际找到本商品，且按窄形态标题词判定、扣除本商品后首页直接同类不超过
                {{ detail?.status.opportunity_max_direct_competitors ?? 2 }} 个。
                “补全第几项”只是平台搜索意图证据，不是公开搜索量；“平台返回商品数”只代表供给规模，也不是热度。
                第几页来自平台 after 游标页；每个游标页最多纳入 36 个自然商品，页内从左到右每行四项。
                跨页自然排名只按实际纳入的自然商品连续累计；若页内有被排除的赞助或非商品记录，不会机械地用页码乘以 36。
                广告并非靠样式猜测；程序只纳入平台搜索 API 的 products.results 中 type=product_views 且未带 sponsored/promoted 标记的项目，其他赞助或推荐区不计入。
                页面插入内容仍可能改变视觉距离，排名也会随时间、地区、个性化、库存和价格变化。
              </p>
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
                    Takealot 补全仅作为平台搜索意图证据，不等同于公开搜索量。
                  </small>
                  <small v-else class="strategy-boundary">
                    入选前必须实际搜到本商品，并通过按窄形态标题词核算的首页低竞争门槛；
                    未获当前标题支持的材质、尺寸、受众、兼容性或功效声明会被拦截。
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
              <p class="title-format-note">建议标题统一仅保留字母、数字和空格；相关关键词前置，卖点与参数后置。</p>
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
            <span>首次运行会识别主图、生成少量精确候选词，并用真实 Takealot 搜索结果逐个验收。</span>
          </section>
        </template>
        <div v-else class="detail-loading">请选择一个商品</div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.ranking-page { display: grid; gap: 18px; color: #18221c; }
.method-banner { display: flex; justify-content: space-between; gap: 24px; padding: 24px 28px; border: 1px solid #c8d4cb; border-radius: 18px; background: linear-gradient(135deg, #f5f7ed, #e4eee8); }
.method-banner p, .section-heading p, .rail-title p, .first-run p { margin: 0 0 5px; color: #64746a; font-size: 11px; font-weight: 800; letter-spacing: .14em; }
.method-banner h2, .section-heading h3, .rail-title h3, .first-run h3 { margin: 0; }
.method-banner > div > span { display: block; max-width: 720px; margin-top: 9px; color: #59675e; line-height: 1.7; }
.method-banner dl { display: grid; min-width: 275px; margin: 0; gap: 8px; }
.method-banner dl div { display: flex; justify-content: space-between; gap: 18px; padding-bottom: 8px; border-bottom: 1px solid #cbd7ce; }
dt { color: #738078; font-size: 12px; } dd { margin: 0; font-weight: 750; }
.error-banner, .config-note, .attempt-note, .running-note { margin: 0; padding: 12px 16px; border-radius: 10px; }
.error-banner { color: #8e2f25; background: #fff0ed; border: 1px solid #efc2bb; }
.config-note { color: #755713; background: #fff8dc; border: 1px solid #ead58b; }
.attempt-note { color: #755713; background: #fff8e8; border: 1px solid #e7cea2; line-height: 1.6; }
.running-note { color: #285c47; background: #e8f4ed; border: 1px solid #b9d7c7; }
.eligibility-note { margin: 0; padding: 11px 15px; border: 1px solid #c9d9cf; border-radius: 10px; color: #355e49; background: #f0f6f2; font-size: 12px; line-height: 1.6; }
.ranking-layout { display: grid; grid-template-columns: minmax(260px, 325px) minmax(0, 1fr); gap: 18px; align-items: start; }
.product-rail, .ranking-detail > section, .detail-loading { border: 1px solid #d9dfdb; border-radius: 16px; background: #fff; }
.product-rail { position: sticky; top: 18px; max-height: calc(100vh - 36px); overflow: auto; padding: 16px; }
.rail-title, .section-heading { display: flex; justify-content: space-between; align-items: end; gap: 16px; }
.rail-title > span, .section-heading > span { color: #66736b; font-size: 12px; font-weight: 700; }
.product-rail > input { box-sizing: border-box; width: 100%; margin: 14px 0; padding: 10px 12px; border: 1px solid #ccd4cf; border-radius: 9px; }
.product-row { width: 100%; display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; padding: 10px; border: 1px solid transparent; border-bottom-color: #e9edea; background: transparent; text-align: left; cursor: pointer; }
.product-row.active { border-color: #4c8068; border-radius: 10px; background: #edf5f0; }
.product-row img, .image-fallback { width: 52px; height: 52px; border-radius: 8px; object-fit: contain; background: #f1f3f1; }
.image-fallback { display: grid; place-items: center; color: #929c96; font-size: 8px; }
.product-copy { min-width: 0; display: grid; gap: 4px; }
.product-copy strong { overflow: hidden; font-size: 12px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.product-copy small { color: #78847c; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.product-copy em { width: fit-content; color: #747e78; font-size: 10px; font-style: normal; }
.product-copy em.completed { color: #26704e; } .product-copy em.failed { color: #a14036; }
.ranking-detail { display: grid; gap: 16px; min-width: 0; }
.ranking-detail > section, .detail-loading { padding: 22px; }
.product-hero { display: grid; grid-template-columns: 110px minmax(0, 1fr) auto; gap: 20px; align-items: center; }
.product-hero > img, .hero-fallback { width: 110px; height: 110px; object-fit: contain; border-radius: 12px; background: #f3f4f1; }
.hero-fallback { display: grid; place-items: center; color: #98a199; font-size: 11px; }
.hero-copy p { margin: 0 0 6px; color: #78857c; font-size: 12px; }.hero-copy h2 { margin: 0; font-size: 22px; line-height: 1.35; }
.ownership-note { display: block; margin-top: 8px; color: #50705e; font-size: 12px; line-height: 1.5; }
.blocked-note { display: block; margin-top: 8px; color: #9d3c31; font-size: 12px; }
.analyze-button { padding: 12px 16px; border: 0; border-radius: 10px; color: #fff; background: #235c45; font-weight: 800; cursor: pointer; }
.analyze-button:disabled { cursor: not-allowed; opacity: .55; }
.identity-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 0 !important; border: 0 !important; background: transparent !important; }
.identity-grid article { padding: 17px; border: 1px solid #d9dfdb; border-radius: 14px; background: #fff; }
.identity-grid p { margin: 0 0 7px; color: #758179; font-size: 11px; font-weight: 800; text-transform: uppercase; }
.identity-grid h3 { margin: 0 0 5px; }.identity-grid span, .identity-grid small { display: block; color: #68746c; font-size: 12px; line-height: 1.5; }.identity-grid small { margin-top: 5px; color: #8a5a34; }
.keyword-section, .title-review, .history-section { display: grid; gap: 16px; }
.keyword-list { display: grid; gap: 10px; }
.keyword-card { padding: 16px; border: 1px solid #d5ded8; border-left: 4px solid #35765a; border-radius: 11px; background: #fbfdfb; }
.keyword-card.opportunity { border-left-color: #8f6b24; background: #fffdf4; }
.keyword-card.comparison_resample { border-left-color: #50708c; background: #f7fbfe; }
.keyword-card.rejected_irrelevant, .keyword-card.model_low_confidence { border-left-color: #bb6a3b; background: #fffaf4; }
.keyword-main { display: flex; justify-content: space-between; gap: 18px; }
.keyword-main > div { display: grid; justify-items: start; gap: 4px; }.keyword-main a { color: #194f3a; font-size: 17px; font-weight: 850; }.keyword-main span { color: #5d6c63; font-size: 12px; }
.strategy-badge { padding: 3px 7px; border-radius: 999px; color: #fff !important; background: #35765a; font-size: 10px !important; font-weight: 800; letter-spacing: .04em; }
.keyword-card.opportunity .strategy-badge { background: #8f6b24; }
.keyword-card.comparison_resample .strategy-badge { background: #50708c; }
.keyword-card.rejected_irrelevant .strategy-badge, .keyword-card.model_low_confidence .strategy-badge { background: #a85d35; }
.query-source { color: #5f7166; font-weight: 700; }
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
@media (max-width: 1050px) { .ranking-layout { grid-template-columns: 1fr; }.product-rail { position: static; max-height: 420px; }.method-banner { flex-direction: column; }.product-hero { grid-template-columns: 90px minmax(0, 1fr); }.analyze-button { grid-column: 1 / -1; }.identity-grid { grid-template-columns: 1fr; } }
@media (max-width: 780px) { .title-strategy-grid { grid-template-columns: 1fr; }.suggested-title { min-height: 0; } }
@media (max-width: 650px) { .method-banner { padding: 18px; }.product-hero { grid-template-columns: 1fr; }.keyword-main { flex-direction: column; }.keyword-position { text-align: left; }.keyword-card dl { flex-wrap: wrap; } }
</style>
