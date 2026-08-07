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
const acceptedKeywords = computed(() =>
  analysis.value?.keywords.filter((item) => item.relevance_status === "accepted") ?? [],
);
const rejectedKeywords = computed(() =>
  analysis.value?.keywords.filter((item) => item.relevance_status !== "accepted") ?? [],
);

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
  if (item.relevance_status === "rejected_irrelevant") return "平台相关性未通过";
  if (item.relevance_status === "model_low_confidence") return "图片识别置信度不足";
  if (!item.found) return `前 ${item.pages_scanned} 页未找到`;
  return `第 ${item.page_number} 页 · 第 ${item.row_number} 行第 ${item.column_number} 列`;
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

function errorMessage(caught: unknown, fallback: string) {
  return caught instanceof ApiRequestError ? caught.message : fallback;
}
</script>

<template>
  <div class="ranking-page">
    <section class="method-banner">
      <div>
        <p>IMAGE → VERIFIED QUERY → ORGANIC POSITION</p>
        <h2>图片识别只是候选，平台结果才是热词验收</h2>
        <span>
          只有当前店铺授权 Seller Offers 中仍为 buyable、明确有可售库存且快照新鲜的链接才会进入；
          模型先理解商品，再用 Takealot 自然结果验证词义，普通查看不会调用模型或访问平台。
        </span>
      </div>
      <dl v-if="listPayload">
        <div><dt>主服务</dt><dd>{{ listPayload.status.provider_label }} · {{ listPayload.status.primary_model }}</dd></div>
        <div v-if="listPayload.status.fallback_model">
          <dt>跨厂商备用</dt><dd>{{ listPayload.status.fallback_provider_label }} · {{ listPayload.status.fallback_model }}</dd>
        </div>
        <div><dt>单词最多扫描</dt><dd>{{ listPayload.status.max_pages }} 页</dd></div>
        <div>
          <dt>位置计算方式</dt>
          <dd>每页最多 36 个自然商品 · 从左到右每行 4 个 · 广告不计</dd>
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
            系统会先再次确认链接仍自有在售，再调用多模态模型并逐个验证热词；同图同标题优先复用缓存。
          </p>
          <p v-if="detail && !detail.status.configured" class="config-note">
            当前仅可查看历史结果。服务端未配置 DASHSCOPE_API_KEY / ARK_API_KEY，点击时不会产生费用或外部请求。
          </p>

          <template v-if="analysis">
            <section class="identity-grid">
              <article>
                <p>模型识别</p>
                <h3>{{ analysis.product_name || "未识别" }}</h3>
                <span>{{ analysis.category || "类别未知" }}</span>
              </article>
              <article>
                <p>识别置信度</p>
                <h3>{{ confidenceLabel(analysis.confidence) }}</h3>
                <span>{{ providerLabel(analysis.provider) }} · {{ analysis.model }} · {{ analysis.vision_reused ? "复用缓存" : "本次调用" }}</span>
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
                <div><p>PLATFORM-VALIDATED QUERIES</p><h3>热词与自然搜索位置</h3></div>
                <span>{{ acceptedKeywords.length }} 个通过 · {{ rejectedKeywords.length }} 个拦截</span>
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
                      <a :href="item.search_url" target="_blank" rel="noreferrer">{{ item.keyword }}</a>
                      <span v-if="!item.found">{{ resultLabel(item) }}</span>
                    </div>
                    <strong v-if="item.found" class="keyword-position">
                      <span>第 {{ item.page_number }} 页 · 第 {{ item.row_number }} 行 · 第 {{ item.column_number }} 列</span>
                      <small>
                        跨页自然排名 #{{ item.organic_rank }}（排除广告后按序第 {{ item.organic_rank }} 个）
                      </small>
                    </strong>
                    <strong v-else-if="item.relevance_status === 'accepted'">未进入扫描范围</strong>
                    <strong v-else>已拦截</strong>
                  </div>
                  <dl>
                    <div><dt>平台相关度</dt><dd>{{ percent(item.relevance_score) }}</dd></div>
                    <div><dt>结果量</dt><dd>{{ item.total_num_found ?? "—" }}</dd></div>
                    <div><dt>采集时间</dt><dd>{{ formatChinaDateTime(item.observed_at) }}</dd></div>
                  </dl>
                  <p>{{ item.validation_evidence.candidate_rationale || item.validation_evidence.reason }}</p>
                  <small v-if="item.validation_evidence.evaluated_top_results">
                    前 {{ item.validation_evidence.evaluated_top_results }} 个自然结果中，
                    {{ item.validation_evidence.matched_top_results }} 个匹配商品类型词
                    {{ item.validation_evidence.validation_terms?.join(" / ") }}。
                  </small>
                </article>
              </div>
              <p class="position-notice">
                位置按固定 1365×900 桌面视口和默认 Relevance 计算：每页最多36个自然商品，
                从左到右、从上到下排列，每行4个；第1页对应跨页自然排名1–36，第2页对应37–72，广告不参与排名或行列计算。
                实际屏幕行列可能被当时广告插入推后，排名也会随时间、地区、个性化、库存和价格变化。
              </p>
            </section>

            <section class="title-review">
              <div class="section-heading">
                <div><p>TITLE HYPOTHESIS</p><h3>主标题修改建议</h3></div>
                <span>{{ validationStatusLabel(analysis.title_validation?.status) }}</span>
              </div>
              <div class="title-compare">
                <article><p>当前标题</p><strong>{{ selectedProduct.title }}</strong></article>
                <article><p>建议标题</p><strong>{{ analysis.title_suggestion || "暂无建议" }}</strong></article>
              </div>
              <p>{{ analysis.title_reason }}</p>
              <div
                v-if="analysis.title_validation?.comparisons?.length"
                class="movement-list"
              >
                <span v-for="row in analysis.title_validation.comparisons" :key="row.keyword">
                  {{ row.keyword }}：#{{ row.before_rank }} → #{{ row.after_rank }}
                  （{{ row.delta > 0 ? `前移 ${row.delta}` : row.delta < 0 ? `后移 ${-row.delta}` : "不变" }}）
                </span>
              </div>
              <p class="causality-note">
                不作“修改后一定前移”的虚假保证：建议默认标记为待验证。只有检测到标题确实改成建议文本，且再次采集相同热词后，
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
.error-banner, .config-note, .running-note { margin: 0; padding: 12px 16px; border-radius: 10px; }
.error-banner { color: #8e2f25; background: #fff0ed; border: 1px solid #efc2bb; }
.config-note { color: #755713; background: #fff8dc; border: 1px solid #ead58b; }
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
.identity-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 0 !important; border: 0 !important; background: transparent !important; }
.identity-grid article { padding: 17px; border: 1px solid #d9dfdb; border-radius: 14px; background: #fff; }
.identity-grid p, .title-compare p { margin: 0 0 7px; color: #758179; font-size: 11px; font-weight: 800; text-transform: uppercase; }
.identity-grid h3 { margin: 0 0 5px; }.identity-grid span { color: #68746c; font-size: 12px; }
.keyword-section, .title-review, .history-section { display: grid; gap: 16px; }
.keyword-list { display: grid; gap: 10px; }
.keyword-card { padding: 16px; border: 1px solid #d5ded8; border-left: 4px solid #35765a; border-radius: 11px; background: #fbfdfb; }
.keyword-card.rejected_irrelevant, .keyword-card.model_low_confidence { border-left-color: #bb6a3b; background: #fffaf4; }
.keyword-main { display: flex; justify-content: space-between; gap: 18px; }
.keyword-main > div { display: grid; gap: 4px; }.keyword-main a { color: #194f3a; font-size: 17px; font-weight: 850; }.keyword-main span { color: #5d6c63; font-size: 12px; }
.keyword-main > strong { color: #235c45; white-space: nowrap; }.keyword-position { display: grid; gap: 4px; text-align: right; }.keyword-position > span { color: #194f3a; font-size: 15px; font-weight: 850; }.keyword-position > small { color: #65746b; font-size: 12px; font-weight: 700; }.keyword-card dl { display: flex; gap: 28px; margin: 14px 0 8px; }.keyword-card dl div { display: grid; gap: 3px; }
.keyword-card p { margin: 7px 0; color: #4e5c53; }.keyword-card small, .position-notice, .causality-note { color: #6e7a72; line-height: 1.6; }
.position-notice, .causality-note { margin: 0; padding: 12px 14px; border-radius: 9px; background: #f3f5f3; font-size: 12px; }
.title-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.title-compare article { padding: 15px; border-radius: 10px; background: #f4f6f4; }.title-compare strong { line-height: 1.55; }
.title-review > p { margin: 0; line-height: 1.7; }.movement-list, .history-list { display: flex; flex-wrap: wrap; gap: 8px; }.movement-list span, .history-list span { padding: 7px 10px; border-radius: 999px; background: #e8f1eb; color: #315a46; font-size: 12px; }
.first-run { text-align: center; padding: 70px 20px !important; }.first-run span, .empty-state, .detail-loading { color: #748077; }
@media (max-width: 1050px) { .ranking-layout { grid-template-columns: 1fr; }.product-rail { position: static; max-height: 420px; }.method-banner { flex-direction: column; }.product-hero { grid-template-columns: 90px minmax(0, 1fr); }.analyze-button { grid-column: 1 / -1; }.identity-grid { grid-template-columns: 1fr; } }
@media (max-width: 650px) { .method-banner { padding: 18px; }.product-hero { grid-template-columns: 1fr; }.title-compare { grid-template-columns: 1fr; }.keyword-main { flex-direction: column; }.keyword-position { text-align: left; }.keyword-card dl { flex-wrap: wrap; } }
</style>
