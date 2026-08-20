<script setup lang="ts">
import { computed, ref, shallowRef, watch } from "vue";

import {
  ANOMALY_PRODUCT_VIEWS,
  ANOMALY_VIEW_LABELS,
  countForAnomalyView,
  itemsForAnomalyView,
  type AnomalyProductView,
} from "../anomalyProducts";
import { fetchAnomalyProducts } from "../api";
import { openOwnStoreDetailTab } from "../moduleNavigation";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { matchesProductSearch } from "../productSearch";
import { formatChinaDateTime } from "../time";
import type {
  AnomalyProductItem,
  AnomalyProductPayload,
  OwnStoreScope,
} from "../types";

const props = defineProps<{
  asOf: string;
  canViewCompetitors?: boolean;
  currentStoreCode?: string;
  currentStoreName?: string;
  storeScope?: OwnStoreScope;
  multiStoreLabel?: string;
  onPermissionDenied?: () => void;
}>();

const payload = shallowRef<AnomalyProductPayload | null>(null);
const activeView = ref<AnomalyProductView>("sudden_sales_stop");
const slowDays = ref(7);
const query = ref("");
const loading = ref(true);
const error = ref("");
const detailTabError = ref("");
const failedImages = ref(new Set<string>());
const anomalyPage = ref(1);
const anomalyPageSize = 30;
const integerFormatter = new Intl.NumberFormat("zh-CN");
const currencyFormatter = new Intl.NumberFormat("en-ZA", {
  style: "currency",
  currency: "ZAR",
  maximumFractionDigits: 2,
});
let loadRequestRevision = 0;

const slowDayOptions = computed(
  () => payload.value?.rules.slow_day_options ?? [4, 7, 10, 15, 20, 30],
);
const viewItems = computed(() =>
  itemsForAnomalyView(payload.value, activeView.value, slowDays.value),
);
const filteredItems = computed(() => {
  if (!query.value.trim()) return viewItems.value;
  return viewItems.value.filter((item) => matchesProductSearch(
    {
      productNames: [item.title, item.company_product_name],
      otherValues: [
        item.sku,
        item.company_sku,
        item.offer_id,
        item.plid,
        item.tsin_id,
        item.store_name,
        item.store_code,
        ...(item.platform_skus ?? []),
        ...(item.company_skus ?? []),
      ],
    },
    query.value,
  ));
});
const anomalyPageCount = computed(() =>
  Math.max(1, Math.ceil(filteredItems.value.length / anomalyPageSize)),
);
const visibleItems = computed(() => {
  const start = (anomalyPage.value - 1) * anomalyPageSize;
  return filteredItems.value.slice(start, start + anomalyPageSize);
});
const visibleItemStart = computed(() =>
  filteredItems.value.length ? (anomalyPage.value - 1) * anomalyPageSize + 1 : 0,
);
const visibleItemEnd = computed(() =>
  Math.min(anomalyPage.value * anomalyPageSize, filteredItems.value.length),
);

watch(
  [() => props.asOf, () => props.storeScope],
  loadAnomalies,
  { immediate: true },
);
watch([activeView, slowDays, query], () => {
  anomalyPage.value = 1;
});
watch(anomalyPageCount, (pageCount) => {
  if (anomalyPage.value > pageCount) anomalyPage.value = pageCount;
});

async function loadAnomalies(): Promise<void> {
  const requestRevision = ++loadRequestRevision;
  const requestedAsOf = props.asOf;
  const requestedStoreScope = props.storeScope ?? "current";
  loading.value = true;
  error.value = "";
  try {
    const nextPayload = await fetchAnomalyProducts(
      requestedAsOf,
      requestedStoreScope,
    );
    if (
      requestRevision !== loadRequestRevision
      || requestedAsOf !== props.asOf
      || requestedStoreScope !== (props.storeScope ?? "current")
    ) return;
    payload.value = nextPayload;
    if (!slowDayOptions.value.includes(slowDays.value)) {
      slowDays.value = slowDayOptions.value[0] ?? 4;
    }
  } catch (reason) {
    if (requestRevision !== loadRequestRevision) return;
    payload.value = null;
    error.value = reason instanceof Error ? reason.message : "异常商品读取失败";
  } finally {
    if (requestRevision === loadRequestRevision) loading.value = false;
  }
}

function viewCount(view: AnomalyProductView): number {
  return countForAnomalyView(payload.value, view, slowDays.value);
}

function openOwnLinkDetail(item: AnomalyProductItem): void {
  if (!props.canViewCompetitors) {
    props.onPermissionDenied?.();
    return;
  }
  if (!item.plid?.trim()) {
    detailTabError.value = "该公司 SKU 的退货明细暂未解析到 PLID，当前不能打开商品详情。";
    return;
  }
  const scope = props.storeScope ?? "current";
  const currentStoreCode = props.currentStoreCode?.trim()
    || item.store_code?.trim()
    || "";
  detailTabError.value = "";
  const opened = openOwnStoreDetailTab({
    plid: item.plid,
    scope,
    ...(scope === "current" && currentStoreCode
      ? { storeCode: currentStoreCode }
      : {}),
  });
  if (!opened) {
    detailTabError.value = "浏览器阻止了自有链接详情新标签页，请允许此站点打开新标签页后重试。";
  }
}

function imageUrl(item: AnomalyProductItem): string {
  const source = item.image_url?.trim() ?? "";
  if (!source || failedImages.value.has(source)) return "";
  return productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list);
}

function markImageUnavailable(item: AnomalyProductItem): void {
  const source = item.image_url?.trim() ?? "";
  if (!source) return;
  failedImages.value = new Set([...failedImages.value, source]);
}

function number(value: number | null | undefined): string {
  return typeof value === "number" ? integerFormatter.format(value) : "—";
}

function itemKey(item: AnomalyProductItem): string {
  return item.store_scope_key
    || item.company_sku
    || `${item.store_code || "current"}:${item.offer_id}`;
}

function currency(value: number | null): string {
  return typeof value === "number" ? currencyFormatter.format(value) : "—";
}

function percent(value: number | null): string {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "—";
}

function detailAriaLabel(item: AnomalyProductItem): string {
  if (!item.plid?.trim()) return `${item.title} 暂无可打开的 PLID 商品详情`;
  if (item.anomaly_type === "high_return_volume") {
    return `在新标签页查看 ${item.company_sku || item.title} 的代表 PLID 商品详情`;
  }
  return `在新标签页查看 ${item.title} 的完整自有链接详情`;
}

function reviewStars(value: number | null | undefined): string {
  return typeof value === "number" ? `${value} 星` : "星级未知";
}

function returnCoverageLabel(): string {
  const coverage = payload.value?.return_coverage;
  if (!coverage) return "退货明细尚未读取";
  if (coverage.data_status === "collected") {
    return `已完整覆盖 ${coverage.covered_store_count ?? coverage.store_count ?? 1} 个店铺`;
  }
  if (coverage.data_status === "stale") return "完整历史可用，但最新采集尝试失败";
  if (coverage.data_status === "partial") {
    return `仅完整覆盖 ${coverage.covered_store_count ?? 0}/${coverage.store_count ?? 0} 个店铺`;
  }
  if (coverage.data_status === "failed") return "退货明细最近采集失败";
  return "退货明细尚未完整采集";
}

function noSalesLabel(item: AnomalyProductItem): string {
  const prefix = item.no_sales_days_exact ? "" : "至少 ";
  return `${prefix}${item.no_sales_days} 天`;
}

function statusInventoryLabel(item: AnomalyProductItem): string {
  const parts = [
    `可售 ${number(item.available_stock)}`,
    item.receiving_stock > 0
      ? `收货中 ${number(item.receiving_stock)}（不计入）`
      : "",
    item.on_way_stock > 0 ? `在途 ${number(item.on_way_stock)}（不计入）` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

function sourceCollectionLabel(source: string, value: string | null | undefined): string {
  if (!value?.trim()) return `${source}拉取时间暂无`;
  return `${source}拉取 ${formatChinaDateTime(value)} · 北京时间`;
}

function cardCollectionLabel(item: AnomalyProductItem): string {
  const times = payload.value?.collection_times;
  if (item.anomaly_type === "sudden_sales_stop" || item.anomaly_type === "slow_moving") {
    return `${sourceCollectionLabel("销量", item.sales_collected_at ?? times?.sales_at)} · 完整证据至 ${item.data_through || "暂无"}`;
  }
  if (item.anomaly_type === "daily_bad_review") {
    return `${sourceCollectionLabel("评论", item.review_collected_at ?? times?.reviews_at)} · 新发现日 ${item.review_discovered_on || payload.value?.requested_as_of || "—"}`;
  }
  if (item.anomaly_type === "poor_review_quality") {
    return `${sourceCollectionLabel("评论", item.review_collected_at ?? times?.reviews_at)} · 发现截至 ${payload.value?.review_discovery_through || "暂无"}`;
  }
  if (item.anomaly_type === "high_return_volume") {
    return `${sourceCollectionLabel("退货", item.return_collected_at ?? times?.returns_at)} · 明细窗口 ${item.return_window_start || "—"} 至 ${item.return_window_end || "—"}`;
  }
  return sourceCollectionLabel("库存", item.offer_collected_at ?? times?.offers_at);
}

function emptyMessage(): string {
  if (query.value.trim()) return "当前类型下没有匹配搜索条件的商品。";
  if (activeView.value === "slow_moving") {
    return `当前没有有库存且连续 ${slowDays.value} 天及以上未动销的可售商品。`;
  }
  if (activeView.value === "daily_bad_reviews") {
    return "当前数据日没有发现建立评论基线后新增的低于五星评论。";
  }
  if (activeView.value === "high_returns") {
    const status = payload.value?.return_coverage.data_status;
    if (status && !["collected", "stale"].includes(status)) {
      return "退货明细覆盖不完整，暂不能把未出现的公司 SKU 解释为零退货。";
    }
  }
  return `当前没有“${ANOMALY_VIEW_LABELS[activeView.value]}”商品。`;
}
</script>

<template>
  <div class="erp-page anomaly-page">
    <section class="anomaly-hero">
      <div>
        <p class="section-kicker">EXCEPTION PRODUCT RADAR</p>
        <h2>异常商品</h2>
      </div>
      <div class="evidence-card">
        <span>最近一次拉取</span>
        <strong>{{ formatChinaDateTime(payload?.collection_times.latest_at ?? null, "暂无") }}</strong>
        <small>
          库存 {{ formatChinaDateTime(payload?.collection_times.offers_at ?? null, "暂无") }} ·
          销量 {{ formatChinaDateTime(payload?.collection_times.sales_at ?? null, "暂无") }} ·
          评论 {{ formatChinaDateTime(payload?.collection_times.reviews_at ?? null, "暂无") }} ·
          退货 {{ formatChinaDateTime(payload?.collection_times.returns_at ?? null, "暂无") }}
        </small>
        <small>
          均为北京时间 · 完整销量证据至 {{ payload?.data_through || "暂无完整业务日" }} · 所选日期
          {{ payload?.requested_as_of || props.asOf }}（北京时间）
        </small>
      </div>
    </section>

    <section class="anomaly-switcher" aria-label="异常类型选择器">
      <button
        v-for="view in ANOMALY_PRODUCT_VIEWS"
        :key="view"
        type="button"
        :class="{ active: activeView === view }"
        :aria-pressed="activeView === view"
        @click="activeView = view"
      >
        <span>{{ ANOMALY_VIEW_LABELS[view] }}</span>
        <strong>{{ viewCount(view) }}</strong>
      </button>
    </section>

    <section class="workspace-toolbar">
      <div class="active-rule">
        <template v-if="activeView === 'sudden_sales_stop'">
          <span>当前规则</span>
          <strong>
            前 7 个完整日中至少 5 天有单、合计至少 7 件，随后连续 3 个完整日零销量
          </strong>
        </template>
        <template v-else-if="activeView === 'slow_moving'">
          <label for="slow-days">滞销门槛</label>
          <select id="slow-days" v-model.number="slowDays">
            <option v-for="days in slowDayOptions" :key="days" :value="days">
              {{ days }} 天及以上未动销
            </option>
          </select>
        </template>
        <template v-else-if="activeView === 'daily_bad_reviews'">
          <span>当前规则</span>
          <strong>低于 5 星 · 按所选日期首次发现</strong>
        </template>
        <template v-else-if="activeView === 'poor_review_quality'">
          <span>当前规则</span>
          <strong>
            累计低于 5 星至少 {{ payload?.rules.poor_review_min_bad_count ?? 5 }} 条，且占已抓评论至少
            {{ payload?.rules.poor_review_min_bad_rate_percentage ?? 20 }}%
          </strong>
        </template>
        <template v-else-if="activeView === 'high_returns'">
          <span>当前规则</span>
          <strong>
            近 {{ payload?.rules.return_window_days ?? 30 }} 天同一公司 SKU 的 Seller Returns 明细合计至少
            {{ payload?.rules.high_return_min_units ?? 5 }} 件
          </strong>
          <small>{{ returnCoverageLabel() }}</small>
        </template>
        <template v-else>
          <span>当前规则</span>
          <strong>{{ ANOMALY_VIEW_LABELS[activeView] }}</strong>
          <small>只统计可售库存</small>
        </template>
      </div>
      <label class="anomaly-search">
        <span class="sr-only">搜索异常商品</span>
        <input
          v-model="query"
          type="search"
          placeholder="商品名称支持模糊搜索，也可输入平台 SKU、公司 SKU、Offer ID 或 PLID"
        />
        <strong>{{ filteredItems.length }} 个商品</strong>
      </label>
    </section>

    <p v-if="detailTabError" class="anomaly-window-error" role="alert">
      {{ detailTabError }}
    </p>
    <div v-if="loading" class="anomaly-state">正在核对销量、库存、评论与退货证据……</div>
    <div v-else-if="error" class="anomaly-state error" role="alert">
      <strong>异常商品暂时无法读取</strong>
      <span>{{ error }}</span>
      <button type="button" @click="loadAnomalies">重新读取</button>
    </div>
    <template v-else-if="filteredItems.length">
      <section class="anomaly-grid">
        <button
        v-for="item in visibleItems"
        :key="`${activeView}-${itemKey(item)}`"
        v-memo="[item, failedImages.size]"
        type="button"
        class="anomaly-card"
        :aria-label="detailAriaLabel(item)"
        @click="openOwnLinkDetail(item)"
      >
        <div class="card-topline">
          <span class="type-badge" :class="`type-${item.anomaly_type}`">
            {{ item.anomaly_label }}
          </span>
          <span class="status-badge">
            {{ item.anomaly_type === "high_return_volume"
              ? "公司 SKU 级"
              : ["daily_bad_review", "poor_review_quality"].includes(item.anomaly_type)
                ? "PLID 级"
                : item.offer_status_label }}
          </span>
        </div>

        <div class="product-identity">
          <div class="product-image">
            <img
              v-if="imageUrl(item)"
              :src="imageUrl(item)"
              :alt="`${item.title} 商品图片`"
              width="192"
              height="192"
              loading="lazy"
              decoding="async"
              referrerpolicy="no-referrer"
              @error="markImageUnavailable(item)"
            />
            <span v-else>暂无图片</span>
          </div>
          <div class="identity-copy">
            <h3>{{ item.title }}</h3>
            <p v-if="props.storeScope !== 'current'">店铺 {{ item.store_name || item.store_code || "—" }}</p>
            <p v-if="item.anomaly_type === 'high_return_volume'">
              公司 {{ item.company_sku || "未关联" }} · 平台 SKU
              {{ item.platform_skus?.slice(0, 3).join("、") || item.sku || "未知" }}
            </p>
            <p v-else>平台 {{ item.sku || "无 SKU" }} · 公司 {{ item.company_sku || "未关联" }}</p>
            <small>
              Offer {{ item.offer_id || "—" }} · PLID {{ item.plid || "未解析" }}
            </small>
          </div>
        </div>

        <div v-if="item.anomaly_type === 'sudden_sales_stop'" class="primary-signal sudden">
          <span>突然停销</span>
          <strong>连续 {{ item.zero_sales_dates?.length || 3 }} 天零销量</strong>
          <small>
            停销前 7 天售出 {{ number(item.baseline_total_units) }} 件，
            {{ number(item.baseline_selling_days) }} 天有单
          </small>
        </div>
        <div v-else-if="item.anomaly_type === 'slow_moving'" class="primary-signal slow">
          <span>实际滞销天数</span>
          <strong>{{ noSalesLabel(item) }}</strong>
          <small>
            滞销起算 {{ item.slow_moving_started_on || "库存历史边界待补充" }} ·
            上次动销 {{ item.last_sale_on || "现有完整历史内未见销量" }}
          </small>
        </div>
        <template v-else-if="item.anomaly_type === 'daily_bad_review'">
          <div class="primary-signal review-signal">
            <span>所选日期新发现</span>
            <strong>{{ number(item.new_bad_review_count) }} 条</strong>
            <small>低于五星 · 首次抓取基线不计入</small>
          </div>
          <div class="review-evidence-list">
            <article
              v-for="review in item.new_bad_reviews || []"
              :key="review.review_id"
              class="review-evidence"
            >
              <div>
                <strong>{{ reviewStars(review.rating) }}</strong>
                <span>{{ review.title || "未填写标题" }}</span>
              </div>
              <p>{{ review.body || "买家未留下文字内容" }}</p>
              <small>
                {{ review.customer_name || "匿名买家" }} · 平台评论日
                {{ review.review_date || "未提供" }} · 北京时间首见 {{ review.first_seen_on || "—" }}
              </small>
            </article>
          </div>
        </template>
        <template v-else-if="item.anomaly_type === 'poor_review_quality'">
          <div class="primary-signal review-signal quality">
            <span>累计低于五星</span>
            <strong>
              {{ number(item.bad_review_count) }} / {{ number(item.review_count) }} 条
            </strong>
            <small>
              占已抓评论 {{ percent(item.bad_review_rate_percentage ?? null) }}，达到产品力重点核查门槛
            </small>
          </div>
          <div class="review-evidence-list compact">
            <article
              v-for="review in (item.recent_bad_reviews || []).slice(0, 3)"
              :key="review.review_id"
              class="review-evidence"
            >
              <div>
                <strong>{{ reviewStars(review.rating) }}</strong>
                <span>{{ review.title || "近期低星评论" }}</span>
              </div>
              <p>{{ review.body || "买家未留下文字内容" }}</p>
              <small>平台评论日 {{ review.review_date || "未提供" }} · 北京时间首见 {{ review.first_seen_on || "—" }}</small>
            </article>
          </div>
        </template>
        <template v-else-if="item.anomaly_type === 'high_return_volume'">
          <div class="primary-signal return-signal">
            <span>近 30 天详细退货</span>
            <strong>{{ number(item.return_units_30_days) }} 件</strong>
            <small>
              {{ number(item.return_record_count) }} 条 Seller Returns 记录 ·
              {{ number(item.affected_platform_sku_count) }} 个平台 SKU
            </small>
          </div>
          <div class="return-reasons">
            <span
              v-for="reason in item.return_reason_counts || []"
              :key="reason.reason"
            >
              {{ reason.label }} {{ number(reason.units) }} 件
            </span>
          </div>
          <div v-if="item.recent_returns?.length" class="return-evidence-list">
            <article
              v-for="returnItem in item.recent_returns.slice(0, 3)"
              :key="`${returnItem.store_code || ''}-${returnItem.seller_return_id}`"
            >
              <strong>{{ returnItem.return_date || "日期未知" }} · {{ number(returnItem.quantity) }} 件</strong>
              <span>{{ returnItem.return_reason_label }}</span>
              <small v-if="returnItem.customer_comment">{{ returnItem.customer_comment }}</small>
            </article>
          </div>
        </template>
        <div v-else class="primary-signal disabled">
          <span>当前可售库存</span>
          <strong>{{ number(item.inventory_units) }} 件</strong>
          <small>{{ statusInventoryLabel(item) }}</small>
        </div>

        <dl
          v-if="['daily_bad_review', 'poor_review_quality'].includes(item.anomaly_type)"
          class="card-metrics"
        >
          <div>
            <dt>已抓评论</dt>
            <dd>{{ number(item.review_count) }}</dd>
          </div>
          <div>
            <dt>低于五星</dt>
            <dd>{{ number(item.bad_review_count) }}</dd>
          </div>
          <div>
            <dt>其中四星</dt>
            <dd>{{ number(item.bad_review_rating_counts?.["4"]) }}</dd>
          </div>
          <div>
            <dt>低星占比</dt>
            <dd>{{ percent(item.bad_review_rate_percentage ?? null) }}</dd>
          </div>
        </dl>
        <dl v-else-if="item.anomaly_type === 'high_return_volume'" class="card-metrics">
          <div>
            <dt>退货件数</dt>
            <dd>{{ number(item.return_units_30_days) }}</dd>
          </div>
          <div>
            <dt>退货记录</dt>
            <dd>{{ number(item.return_record_count) }}</dd>
          </div>
          <div>
            <dt>平台 SKU</dt>
            <dd>{{ number(item.affected_platform_sku_count) }}</dd>
          </div>
          <div>
            <dt>覆盖店铺</dt>
            <dd>{{ number(item.store_codes?.length) }}</dd>
          </div>
        </dl>
        <dl v-else class="card-metrics">
          <div>
            <dt>售价</dt>
            <dd>{{ currency(item.selling_price) }}</dd>
          </div>
          <div>
            <dt>可售</dt>
            <dd>{{ number(item.available_stock) }}</dd>
          </div>
          <div>
            <dt>近 30 天浏览</dt>
            <dd>{{ number(item.page_views_30_days) }}</dd>
          </div>
          <div>
            <dt>近 30 天转化</dt>
            <dd>{{ percent(item.conversion_percentage_30_days) }}</dd>
          </div>
        </dl>

        <div class="card-footer">
          <span>{{ cardCollectionLabel(item) }}</span>
          <strong>
            {{ !item.plid
              ? "退货身份暂未解析到 PLID"
              : item.anomaly_type === "high_return_volume"
                ? "新标签页查看代表 PLID 详情"
                : "新标签页查看完整商品详情" }}
          </strong>
        </div>
        </button>
      </section>
      <nav
        v-if="filteredItems.length > anomalyPageSize"
        class="anomaly-pagination"
        aria-label="异常商品分页"
      >
        <button
          type="button"
          :disabled="anomalyPage <= 1"
          @click="anomalyPage -= 1"
        >
          上一页
        </button>
        <span>
          第 {{ anomalyPage }} / {{ anomalyPageCount }} 页 ·
          当前 {{ visibleItemStart }}–{{ visibleItemEnd }} / {{ filteredItems.length }} 个商品
        </span>
        <button
          type="button"
          :disabled="anomalyPage >= anomalyPageCount"
          @click="anomalyPage += 1"
        >
          下一页
        </button>
      </nav>
    </template>
    <div v-else class="anomaly-state empty">
      <strong>{{ emptyMessage() }}</strong>
      <span>切换上方异常类型或调整搜索条件继续查看。</span>
    </div>

  </div>
</template>

<style scoped>
.anomaly-page {
  display: grid;
  gap: 18px;
}

.anomaly-window-error {
  margin: 0;
  padding: 12px 16px;
  border: 1px solid #e6b7ab;
  border-radius: 12px;
  color: #8f3324;
  background: #fff4f1;
  font-weight: 700;
}

.anomaly-hero {
  display: flex;
  align-items: stretch;
  justify-content: space-between;
  gap: 24px;
  padding: 26px 28px;
  border: 1px solid #d8e0d8;
  border-radius: 22px;
  background:
    radial-gradient(circle at 84% 16%, rgb(194 140 51 / 14%), transparent 30%),
    linear-gradient(135deg, #fdfcf6, #f1f5ed);
  box-shadow: 0 14px 34px rgb(42 67 55 / 7%);
}

.section-kicker {
  margin: 0 0 7px;
  color: #a46a22;
  font-size: 11px;
  font-weight: 900;
  letter-spacing: 0.16em;
}

.anomaly-hero h2 {
  margin: 0;
  color: #20382e;
  font-size: clamp(28px, 3vw, 40px);
  letter-spacing: -0.04em;
}

.hero-copy {
  max-width: 700px;
  margin: 10px 0 0;
  color: #65776e;
  line-height: 1.7;
}

.evidence-card {
  min-width: 230px;
  display: grid;
  align-content: center;
  gap: 5px;
  padding: 17px 20px;
  border: 1px solid rgb(49 82 69 / 13%);
  border-radius: 16px;
  background: rgb(255 255 255 / 76%);
}

.evidence-card span,
.evidence-card small {
  color: #78877f;
  font-size: 12px;
}

.evidence-card strong {
  color: #315245;
  font-size: 19px;
}

.anomaly-switcher {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.anomaly-switcher button {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 15px;
  border: 1px solid #d8e0d8;
  border-radius: 14px;
  color: #53675e;
  background: #fff;
  cursor: pointer;
  text-align: left;
  transition: border-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}

.anomaly-switcher button:hover {
  border-color: #9bb0a6;
  transform: translateY(-1px);
}

.anomaly-switcher button.active {
  border-color: #315245;
  color: #fff;
  background: #315245;
  box-shadow: 0 10px 24px rgb(49 82 69 / 18%);
}

.anomaly-switcher span {
  overflow: hidden;
  font-size: 13px;
  font-weight: 750;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.anomaly-switcher strong {
  min-width: 28px;
  padding: 3px 7px;
  border-radius: 999px;
  color: #315245;
  background: #edf3ee;
  text-align: center;
}

.anomaly-switcher button.active strong {
  color: #315245;
  background: #f8e3b4;
}

.workspace-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 15px 18px;
  border: 1px solid #dfe5df;
  border-radius: 16px;
  background: #fff;
}

.active-rule {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  color: #61736a;
  font-size: 12px;
}

.active-rule > span,
.active-rule > label {
  flex: 0 0 auto;
  color: #9a6b2d;
  font-weight: 850;
  text-transform: uppercase;
}

.active-rule strong {
  color: #315245;
  line-height: 1.5;
}

.active-rule select {
  padding: 8px 34px 8px 11px;
  border: 1px solid #b9c8c0;
  border-radius: 10px;
  color: #29493c;
  background: #f8faf7;
  font: inherit;
  font-weight: 800;
}

.anomaly-search {
  min-width: min(360px, 42%);
  display: flex;
  align-items: center;
  gap: 10px;
}

.anomaly-search input {
  min-width: 0;
  flex: 1;
  padding: 10px 12px;
  border: 1px solid #cdd8d1;
  border-radius: 11px;
  color: #29493c;
  background: #fafcf9;
}

.anomaly-search strong {
  flex: 0 0 auto;
  color: #6a7b72;
  font-size: 12px;
}

.anomaly-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 16px;
}

.anomaly-card {
  width: 100%;
  min-width: 0;
  display: grid;
  gap: 15px;
  margin: 0;
  padding: 18px;
  border: 1px solid #dce4dd;
  border-radius: 18px;
  color: inherit;
  background: #fff;
  box-shadow: 0 9px 26px rgb(42 67 55 / 6%);
  font: inherit;
  text-align: left;
  text-decoration: none;
  appearance: none;
  cursor: pointer;
  content-visibility: auto;
  contain-intrinsic-size: auto 350px;
  transition: border-color 180ms ease, box-shadow 180ms ease, transform 180ms ease;
}

.anomaly-pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 12px 16px;
  border: 1px solid #dce4dd;
  border-radius: 14px;
  color: #65776e;
  background: rgb(255 255 255 / 82%);
  font-size: 12px;
  font-weight: 750;
}

.anomaly-pagination button {
  min-width: 74px;
  padding: 8px 12px;
  border: 1px solid #cbd7cf;
  border-radius: 10px;
  color: #315245;
  background: #f7faf7;
  font: inherit;
  cursor: pointer;
}

.anomaly-pagination button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.anomaly-card:hover,
.anomaly-card:focus-visible {
  border-color: #9caf9f;
  box-shadow: 0 16px 34px rgb(42 67 55 / 12%);
  outline: none;
  transform: translateY(-2px);
}

.card-topline,
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.type-badge,
.status-badge {
  display: inline-flex;
  align-items: center;
  min-height: 25px;
  padding: 4px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 850;
}

.type-badge {
  color: #8b4b28;
  background: #fde9dc;
}

.type-slow_moving {
  color: #86601e;
  background: #fff0c9;
}

.type-not_buyable_with_stock,
.type-disabled_by_takealot_with_stock,
.type-disabled_by_seller_with_stock {
  color: #8a3740;
  background: #fbe2e4;
}

.type-daily_bad_review,
.type-poor_review_quality {
  color: #8d3c63;
  background: #f9e3ee;
}

.type-high_return_volume {
  color: #76501b;
  background: #f9e8c8;
}

.status-badge {
  color: #5e7067;
  background: #eef3ef;
}

.product-identity {
  min-width: 0;
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr);
  gap: 14px;
}

.product-image {
  width: 82px;
  height: 82px;
  display: grid;
  place-items: center;
  overflow: hidden;
  border: 1px solid #e1e7e2;
  border-radius: 14px;
  color: #9ba8a1;
  background: #f6f8f5;
  font-size: 11px;
}

.product-image img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.identity-copy {
  min-width: 0;
  align-self: center;
}

.identity-copy h3 {
  display: -webkit-box;
  overflow: hidden;
  margin: 0 0 8px;
  color: #243d32;
  font-size: 15px;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.identity-copy p,
.identity-copy small {
  overflow: hidden;
  display: block;
  margin: 0;
  color: #798981;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.identity-copy small {
  margin-top: 4px;
}

.primary-signal {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 3px 12px;
  padding: 13px 14px;
  border-radius: 13px;
  background: #f7f4ea;
}

.primary-signal span {
  color: #8b6b31;
  font-size: 11px;
  font-weight: 850;
}

.primary-signal strong {
  justify-self: end;
  color: #6e4e18;
  font-size: 17px;
}

.primary-signal small {
  grid-column: 1 / -1;
  color: #7d7868;
  line-height: 1.45;
}

.primary-signal.sudden {
  background: #fff0e7;
}

.primary-signal.sudden span,
.primary-signal.sudden strong {
  color: #a04d2a;
}

.primary-signal.disabled {
  background: #fbecee;
}

.primary-signal.disabled span,
.primary-signal.disabled strong {
  color: #923d47;
}

.primary-signal.review-signal {
  background: #faedf4;
}

.primary-signal.review-signal span,
.primary-signal.review-signal strong {
  color: #8d3c63;
}

.primary-signal.review-signal.quality {
  background: #f7e8ef;
}

.primary-signal.return-signal {
  background: #fcf0dc;
}

.primary-signal.return-signal span,
.primary-signal.return-signal strong {
  color: #82591e;
}

.review-evidence-list,
.return-evidence-list {
  max-height: 340px;
  display: grid;
  gap: 8px;
  overflow: auto;
  scrollbar-width: thin;
}

.review-evidence-list.compact {
  max-height: 260px;
}

.review-evidence,
.return-evidence-list article {
  display: grid;
  gap: 5px;
  padding: 10px 11px;
  border: 1px solid #eadfe4;
  border-radius: 11px;
  background: #fffafb;
}

.review-evidence > div {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.review-evidence strong {
  flex: 0 0 auto;
  color: #9b466c;
  font-size: 11px;
}

.review-evidence span,
.return-evidence-list span {
  overflow: hidden;
  color: #54675e;
  font-size: 11px;
  font-weight: 800;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.review-evidence p {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  color: #4e5e57;
  font-size: 12px;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 4;
}

.review-evidence small,
.return-evidence-list small {
  color: #87938c;
  font-size: 10px;
  line-height: 1.45;
}

.return-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.return-reasons span {
  padding: 5px 8px;
  border-radius: 999px;
  color: #76501b;
  background: #fbf0db;
  font-size: 10px;
  font-weight: 800;
}

.return-evidence-list article {
  border-color: #ece1cd;
  background: #fffcf6;
}

.return-evidence-list strong {
  color: #75511e;
  font-size: 11px;
}

.card-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}

.card-metrics div {
  min-width: 0;
  padding: 9px 8px;
  border-radius: 10px;
  background: #f6f8f5;
}

.card-metrics dt {
  overflow: hidden;
  margin-bottom: 5px;
  color: #839087;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-metrics dd {
  overflow: hidden;
  margin: 0;
  color: #315245;
  font-size: 12px;
  font-weight: 800;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-footer {
  padding-top: 12px;
  border-top: 1px solid #edf0ed;
  color: #859189;
  font-size: 10px;
}

.card-footer strong {
  color: #426b59;
  font-size: 11px;
}

.card-footer b {
  color: #b47729;
}

.anomaly-state {
  min-height: 240px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 9px;
  padding: 32px;
  border: 1px dashed #cdd8d1;
  border-radius: 18px;
  color: #728078;
  background: #fbfcfa;
  text-align: center;
}

.anomaly-state strong {
  color: #40594e;
}

.anomaly-state.error {
  border-color: #e1b8b8;
  color: #8b4a4a;
  background: #fff8f7;
}

.anomaly-state button {
  padding: 8px 13px;
  border: 0;
  border-radius: 9px;
  color: #fff;
  background: #315245;
  cursor: pointer;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 1100px) {
  .anomaly-switcher {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .workspace-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .anomaly-search {
    width: 100%;
    min-width: 0;
  }
}

@media (max-width: 720px) {
  .anomaly-hero {
    flex-direction: column;
    padding: 20px;
  }

  .evidence-card {
    min-width: 0;
  }

  .anomaly-switcher {
    grid-template-columns: 1fr 1fr;
  }

  .active-rule {
    align-items: flex-start;
    flex-direction: column;
  }

  .anomaly-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .card-metrics {
    grid-template-columns: 1fr 1fr;
  }

  .card-footer {
    align-items: flex-start;
    flex-direction: column;
  }

  .anomaly-pagination {
    flex-wrap: wrap;
  }
}
</style>
