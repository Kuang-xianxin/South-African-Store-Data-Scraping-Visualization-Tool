<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";

import { ApiRequestError, fetchContainerSelection } from "../api";
import { ownStoreDetailPageHref } from "../moduleNavigation";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { formatChinaDateTime } from "../time";
import type {
  CompetitorObservedSalesWindowKey,
  ContainerSelectionLink,
  ContainerSelectionPayload,
  ContainerSelectionRadarCategory,
  ContainerSelectionRadarRepresentative,
  ContainerSelectionReplenishmentItem,
} from "../types";

const loadEmbeddedCompetitorDetail = () => import("./CompetitorsPage.vue");
const EmbeddedCompetitorDetail = defineAsyncComponent(
  loadEmbeddedCompetitorDetail,
);

const props = defineProps<{
  asOf: string;
  canOperate?: boolean;
  canControlCollection?: boolean;
  isAdmin?: boolean;
  currentUsername?: string;
  accessibleConnectedStoreCount?: number;
  operatingConnectedStoreCount?: number;
  onPermissionDenied?: () => void;
}>();

const payload = ref<ContainerSelectionPayload | null>(null);
const loading = ref(true);
const error = ref("");
const activeView = ref<"replenishment" | "radar">("replenishment");
const recommendationFilter = ref("all");
const selectedRadarCategoryId = ref("all");
const search = ref("");
const expandedSku = ref("");
const expandedRadarCategoryIds = ref<Set<string>>(new Set());
const failedImageKeys = ref<Set<string>>(new Set());
const stockOutflowWindowDays = [7, 15, 30, 60, 90] as const;
const radarDetailPlid = ref("");
const radarDetailRevision = ref(0);
const radarDetailPrefetchPlid = ref("");
const radarDetailPrefetchRevision = ref(0);
const radarDetailTrigger = ref<HTMLElement | null>(null);
const radarDetailHostReady = ref(false);
const imageRetryDelaysMs = [500, 1_500] as const;
const radarDetailPrefetchDelayMs = 140;
let controller: AbortController | null = null;
let radarDetailIdleHandle: number | null = null;
let radarDetailWarmupTimer: number | null = null;
let radarDetailPrefetchTimer: number | null = null;
let radarDetailWarmupDisposed = false;

function cancelRadarDetailWarmup(): void {
  if (radarDetailIdleHandle !== null && "cancelIdleCallback" in window) {
    window.cancelIdleCallback(radarDetailIdleHandle);
  }
  if (radarDetailWarmupTimer !== null) {
    window.clearTimeout(radarDetailWarmupTimer);
  }
  radarDetailIdleHandle = null;
  radarDetailWarmupTimer = null;
}

function ensureRadarDetailHost(): void {
  cancelRadarDetailWarmup();
  radarDetailHostReady.value = true;
}

function cancelRadarDetailPrefetchIntent(): void {
  if (radarDetailPrefetchTimer !== null) {
    window.clearTimeout(radarDetailPrefetchTimer);
    radarDetailPrefetchTimer = null;
  }
}

function prefetchRadarDetail(item: ContainerSelectionRadarRepresentative): void {
  const plid = item.plid.trim();
  if (!plid) return;
  ensureRadarDetailHost();
  radarDetailPrefetchPlid.value = plid;
  radarDetailPrefetchRevision.value += 1;
}

function scheduleRadarDetailPrefetch(
  item: ContainerSelectionRadarRepresentative,
): void {
  cancelRadarDetailPrefetchIntent();
  radarDetailPrefetchTimer = window.setTimeout(() => {
    radarDetailPrefetchTimer = null;
    prefetchRadarDetail(item);
  }, radarDetailPrefetchDelayMs);
}

function scheduleRadarDetailWarmup(): void {
  if (radarDetailHostReady.value || radarDetailWarmupDisposed) return;
  void loadEmbeddedCompetitorDetail()
    .then(() => {
      if (radarDetailHostReady.value || radarDetailWarmupDisposed) return;
      const mountHost = () => {
        radarDetailIdleHandle = null;
        radarDetailWarmupTimer = null;
        if (!radarDetailWarmupDisposed) radarDetailHostReady.value = true;
      };
      if (typeof window.requestIdleCallback === "function") {
        radarDetailIdleHandle = window.requestIdleCallback(mountHost, {
          timeout: 1_500,
        });
      } else {
        radarDetailWarmupTimer = window.setTimeout(mountHost, 250);
      }
    })
    .catch(() => {
      // A later explicit click lets the async component surface/retry the error.
    });
}

const replenishmentItems = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase();
  return (payload.value?.replenishment_items ?? []).filter((item) => {
    if (
      recommendationFilter.value !== "all"
      && item.recommendation.status !== recommendationFilter.value
    ) return false;
    if (!needle) return true;
    return [item.product_name, item.company_sku, ...item.plids]
      .some((value) => value.toLocaleLowerCase().includes(needle));
  });
});

const radarCategories = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase();
  return (payload.value?.radar_categories ?? []).filter((category) => {
    if (
      selectedRadarCategoryId.value !== "all"
      && category.category_id !== selectedRadarCategoryId.value
    ) return false;
    if (!needle) return true;
    return [
      category.category_name,
      category.market_leaf_name,
      category.economics_anchor.name,
      ...category.representatives.flatMap((item) => [
        item.name,
        item.current.title ?? "",
        item.plid,
        ...item.role_labels,
      ]),
    ].some((value) => value.toLocaleLowerCase().includes(needle));
  });
});

function pickRadarCategoryCover(
  category: ContainerSelectionRadarCategory,
): ContainerSelectionRadarRepresentative | null {
  const withUsableImage = (item: ContainerSelectionRadarRepresentative) => {
    const imageUrl = item.current.image_url?.trim() ?? "";
    return imageUrl && !failedImageKeys.value.has(selectionImageKey(imageUrl));
  };
  return category.representatives.find(
    (item) => item.monitoring.qualified_recent_signal && withUsableImage(item),
  ) ?? category.representatives.find(
    (item) => item.monitoring.recent_signal && withUsableImage(item),
  ) ?? category.representatives.find(withUsableImage)
    ?? null;
}

const radarCategoryCovers = computed(() => new Map(
  (payload.value?.radar_categories ?? []).map((category) => [
    category.category_id,
    pickRadarCategoryCover(category),
  ]),
));

function radarCategoryCoverImage(category: ContainerSelectionRadarCategory): string {
  return radarCategoryCovers.value.get(category.category_id)?.current.image_url?.trim() ?? "";
}

async function load(): Promise<void> {
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchContainerSelection(props.asOf, controller.signal);
    if (
      selectedRadarCategoryId.value !== "all"
      && !payload.value.radar_categories.some(
        (category) => category.category_id === selectedRadarCategoryId.value,
      )
    ) selectedRadarCategoryId.value = "all";
    expandedRadarCategoryIds.value = new Set();
    failedImageKeys.value = new Set();
    void nextTick(scheduleRadarDetailWarmup);
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") return;
    error.value = caught instanceof ApiRequestError
      ? caught.message
      : "配柜选品数据暂时无法读取";
  } finally {
    loading.value = false;
  }
}

function toggleDetail(item: ContainerSelectionReplenishmentItem): void {
  expandedSku.value = expandedSku.value === item.company_sku ? "" : item.company_sku;
}

function isRadarCategoryLinksExpanded(categoryId: string): boolean {
  return expandedRadarCategoryIds.value.has(categoryId);
}

function toggleRadarCategoryLinks(categoryId: string): void {
  const next = new Set(expandedRadarCategoryIds.value);
  if (next.has(categoryId)) next.delete(categoryId);
  else next.add(categoryId);
  expandedRadarCategoryIds.value = next;
}

function radarCategoryLinksPanelId(categoryId: string): string {
  return `radar-category-links-${categoryId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function recommendationTone(status: string): string {
  if (status === "replenish") return "positive";
  if (status === "hold") return "neutral";
  if (["low_velocity", "mapping_missing", "coverage_insufficient"].includes(status)) {
    return "muted";
  }
  return "warning";
}

function monitoringTone(status: string): string {
  if (["recent_hot", "recent_signal"].includes(status)) return "positive";
  if (["recent_cold", "recent_no_motion"].includes(status)) return "neutral";
  if (["not_added", "monitoring_incomplete"].includes(status)) return "danger";
  return "warning";
}

function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

function observedStockOutflow(
  item: ContainerSelectionRadarRepresentative,
  days: typeof stockOutflowWindowDays[number],
): number | null {
  const key = String(days) as CompetitorObservedSalesWindowKey;
  const value = item.monitoring.recent_observed_sales?.[key];
  return typeof value === "number" ? value : null;
}

function observedStockOutflowLabel(
  item: ContainerSelectionRadarRepresentative,
  days: typeof stockOutflowWindowDays[number],
): string {
  const value = observedStockOutflow(item, days);
  return value === null ? "数据不足" : `流出 ${number(value)}`;
}

function money(value: number | null | undefined, currency: "R" | "¥" = "¥"): string {
  return value === null || value === undefined ? "—" : `${currency}${number(value, 2)}`;
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${number(value, 2)}%`;
}

function chinaDateTime(value: string | null | undefined): string {
  return value ? formatChinaDateTime(value) : "—";
}

function ownLinkDetailHref(link: ContainerSelectionLink): string {
  return ownStoreDetailPageHref({
    plid: link.plid,
    scope: "current",
    storeCode: link.store_code,
  });
}

function selectionImageKey(
  source: string | null | undefined,
  storeCode?: string | null,
): string {
  return `${String(storeCode ?? "competitor").trim().toLocaleLowerCase()}|${String(source ?? "").trim()}`;
}

function selectionImageUrl(
  source: string | null | undefined,
  storeCode?: string | null,
  retryAttempt = 0,
): string {
  const normalized = String(source ?? "").trim();
  if (!normalized || failedImageKeys.value.has(selectionImageKey(normalized, storeCode))) {
    return "";
  }
  return productThumbnailUrl(
    normalized,
    PRODUCT_IMAGE_SIZE.list,
    storeCode,
    retryAttempt,
  );
}

function markSelectionImageUnavailable(
  source: string | null | undefined,
  storeCode?: string | null,
): void {
  const normalized = String(source ?? "").trim();
  if (!normalized) return;
  failedImageKeys.value = new Set([
    ...failedImageKeys.value,
    selectionImageKey(normalized, storeCode),
  ]);
}

function retrySelectionImage(
  event: Event,
  source: string | null | undefined,
  storeCode?: string | null,
): void {
  const image = event.currentTarget;
  if (!(image instanceof HTMLImageElement)) {
    markSelectionImageUnavailable(source, storeCode);
    return;
  }
  const currentAttempt = Number.parseInt(image.dataset.imageRetryAttempt ?? "0", 10);
  if (
    !Number.isFinite(currentAttempt)
    || currentAttempt >= imageRetryDelaysMs.length
  ) {
    markSelectionImageUnavailable(source, storeCode);
    return;
  }
  const nextAttempt = currentAttempt + 1;
  image.dataset.imageRetryAttempt = String(nextAttempt);
  window.setTimeout(() => {
    if (!image.isConnected) return;
    const nextUrl = selectionImageUrl(source, storeCode, nextAttempt);
    if (nextUrl) image.src = nextUrl;
  }, imageRetryDelaysMs[currentAttempt]);
}

function openRadarDetail(
  item: ContainerSelectionRadarRepresentative,
  event: Event,
): void {
  const trigger = event.currentTarget;
  radarDetailTrigger.value = trigger instanceof HTMLElement ? trigger : null;
  cancelRadarDetailPrefetchIntent();
  ensureRadarDetailHost();
  const plid = item.plid.trim();
  radarDetailPrefetchPlid.value = plid;
  radarDetailPrefetchRevision.value += 1;
  radarDetailPlid.value = plid;
  radarDetailRevision.value += 1;
}

async function closeRadarDetail(): Promise<void> {
  radarDetailPlid.value = "";
  await nextTick();
  radarDetailTrigger.value?.focus({ preventScroll: true });
  radarDetailTrigger.value = null;
}

function categoryOpeningTone(category: ContainerSelectionRadarCategory): string {
  return category.decision.status === "opening_review" ? "positive" : "neutral";
}

function trend(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "无可比基线";
  const sign = value > 0 ? "+" : "";
  return `${sign}${number(value, 1)}%`;
}

watch(() => props.asOf, () => void load());
onMounted(() => void load());
onBeforeUnmount(() => {
  radarDetailWarmupDisposed = true;
  cancelRadarDetailWarmup();
  cancelRadarDetailPrefetchIntent();
  controller?.abort();
});
</script>

<template>
  <main class="container-selection-page">
    <header class="selection-hero">
      <div>
        <p class="eyebrow">CHRISTMAS CONTAINER FILL</p>
        <h1>配柜选品</h1>
        <p>
          用大体积、快动销、非带电商品释放国内仓并填充普货舱位；新品先监控，补货先看真实链路。
        </p>
      </div>
      <button type="button" :disabled="loading" @click="load">
        {{ loading ? "正在计算…" : "重新计算" }}
      </button>
    </header>

    <section v-if="payload" class="policy-strip">
      <strong>带电体积上限 {{ payload.policy.electrified_volume_limit_percent }}%</strong>
      <span>备货目标 {{ payload.policy.replenishment_cover_days }} 天</span>
      <span>清货观察 {{ payload.policy.clearance_window_days }} 天</span>
      <span>{{ payload.scope.label }} · {{ payload.scope.store_count }} 店</span>
      <small>数据截止 {{ payload.as_of }}（北京时间）</small>
    </section>

    <section v-if="payload" class="summary-grid" aria-label="配柜选品摘要">
      <article>
        <span>建议加急补货</span>
        <strong>{{ payload.summary.replenishment_count }}</strong>
        <small>{{ payload.summary.recommended_units }} 件 · {{ number(payload.summary.recommended_cbm, 3) }} m³</small>
      </article>
      <article>
        <span>新品类目 / 代表链接</span>
        <strong>{{ payload.summary.radar_category_count }}/{{ payload.summary.radar_link_count }}</strong>
        <small>{{ payload.summary.radar_active_link_count }} 条已进入竞品雷达</small>
      </article>
      <article>
        <span>近期亮眼 / 可进复核</span>
        <strong>{{ payload.summary.radar_recent_hot_category_count }}/{{ payload.summary.radar_opening_review_count }}</strong>
        <small>{{ payload.summary.radar_waiting_category_count }} 类仍在等近期证据</small>
      </article>
      <article>
        <span>利润换算</span>
        <strong>{{ payload.exchange_rate.rate ? number(payload.exchange_rate.rate, 4) : "—" }}</strong>
        <small>CNY→ZAR · {{ payload.exchange_rate.rate_date || "缓存不可用" }}</small>
      </article>
    </section>

    <p v-if="error" class="state-card error" role="alert">
      {{ error }}
      <button type="button" @click="load">重试</button>
    </p>
    <p v-else-if="loading && !payload" class="state-card" role="status">
      正在按店铺、PLID、Offer 重建近30天销售、前30天趋势与库存链路…
    </p>

    <template v-if="payload">
      <nav class="view-tabs" aria-label="配柜选品视图">
        <button
          type="button"
          :class="{ active: activeView === 'replenishment' }"
          @click="activeView = 'replenishment'"
        >
          补货建议
          <b>{{ payload.summary.replenishment_count }}</b>
        </button>
        <button
          type="button"
          :class="{ active: activeView === 'radar' }"
          @click="activeView = 'radar'"
        >
          新品监控
          <b>{{ payload.summary.radar_category_count }}</b>
        </button>
      </nav>

      <section class="toolbar">
        <label>
          <span>搜索</span>
          <input
            v-model="search"
            type="search"
            :placeholder="activeView === 'radar' ? '类目 / 商品名 / PLID' : '商品名 / SKU / PLID'"
          />
        </label>
        <label v-if="activeView === 'replenishment'">
          <span>结论</span>
          <select v-model="recommendationFilter">
            <option value="all">全部结论</option>
            <option value="replenish">建议加急补货</option>
            <option value="hold">暂缓补货</option>
            <option value="profit_unverified">先核利润</option>
            <option value="recent_momentum_weak">近期转弱</option>
            <option value="coverage_insufficient">近期覆盖不足</option>
            <option value="low_velocity">低动销</option>
            <option value="mapping_missing">主档未匹配</option>
          </select>
        </label>
        <label v-else class="radar-category-selector">
          <span>类目选择</span>
          <select v-model="selectedRadarCategoryId">
            <option value="all">全部类目（{{ payload.radar_categories.length }}）</option>
            <option
              v-for="category in payload.radar_categories"
              :key="category.category_id"
              :value="category.category_id"
            >
              {{ category.category_name }}（{{ category.representatives.length }} 条）
            </option>
          </select>
        </label>
        <small v-if="activeView === 'replenishment'">
          月动销先逐链路按有效覆盖计算，再跨店汇总；缺失日期不补 0。
        </small>
        <small v-else>
          评论必须看日期；累计评论只用于价格带，近30天评论和库存流出才参与新品判断。
        </small>
      </section>

      <section v-if="activeView === 'replenishment'" class="table-shell">
        <table>
          <thead>
            <tr>
              <th>商品</th>
              <th>配柜体积</th>
              <th>动销证据</th>
              <th>分阶段库存</th>
              <th>利润下限</th>
              <th>建议</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in replenishmentItems" :key="item.company_sku">
              <tr>
                <td class="product-cell">
                  <span class="rank">{{ item.rank }}</span>
                  <div class="selection-product-image">
                    <img
                      v-if="selectionImageUrl(item.image_url, item.image_store_code)"
                      :src="selectionImageUrl(item.image_url, item.image_store_code)"
                      :alt="`${item.product_name} 商品图片`"
                      width="72"
                      height="72"
                      loading="lazy"
                      decoding="async"
                      referrerpolicy="no-referrer"
                      @error="retrySelectionImage($event, item.image_url, item.image_store_code)"
                    />
                    <span v-else>暂无图片</span>
                  </div>
                  <div>
                    <strong>{{ item.product_name }}</strong>
                    <code>{{ item.company_sku }}</code>
                    <small>{{ item.electrical.evidence }}</small>
                    <div class="tag-row">
                      <span v-for="tag in item.risk_tags" :key="tag">{{ tag }}</span>
                    </div>
                  </div>
                </td>
                <td>
                  <strong>{{ number(item.logistics.unit_cbm, 4) }} m³/件</strong>
                  <small>{{ number(item.logistics.cbm_per_100_units, 2) }} m³/百件</small>
                  <small>{{ item.source.measured ? "实测箱规" : "工作簿箱规，待实测" }}</small>
                </td>
                <td>
                  <strong>{{ number(item.sales.recent_monthly_velocity, 1) }} 件/月近期月化</strong>
                  <small>近30天 {{ item.sales.recent_30_units }} 件 · {{ item.sales.recent_known_link_count }}/{{ item.sales.link_count }} 条链路达标</small>
                  <small>前30天月化 {{ number(item.sales.previous_monthly_velocity, 1) }} 件 · 环比 {{ trend(item.sales.recent_vs_previous_change_percentage) }}</small>
                  <small>90天 {{ item.sales.ordered_units }} 件，仅作背景</small>
                </td>
                <td>
                  <strong>可售 {{ item.inventory.sellable_stock }}</strong>
                  <small>收货中 {{ item.inventory.stock_in_receiving }} · 在途 {{ item.inventory.stock_on_way }}</small>
                  <small>按近期月化，可售覆盖 {{ number(item.inventory.stock_cover_days, 1) }} 天</small>
                </td>
                <td>
                  <strong :class="{ negative: (item.profit.minimum_profit_rmb ?? 0) <= 0 }">
                    {{ money(item.profit.minimum_profit_rmb) }}/件
                  </strong>
                  <small>最低利润率 {{ percent(item.profit.minimum_margin_percentage) }}</small>
                  <small>{{ item.profit.calculated_offer_count }} 个 Offer 可复算</small>
                </td>
                <td>
                  <span class="status-pill" :class="recommendationTone(item.recommendation.status)">
                    {{ item.recommendation.label }}
                  </span>
                  <strong v-if="item.recommendation.recommended_units">
                    {{ item.recommendation.recommended_units }} 件
                  </strong>
                  <small v-if="item.recommendation.recommended_units">
                    {{ item.recommendation.recommended_cartons }} 箱 · {{ number(item.recommendation.recommended_cbm, 3) }} m³
                  </small>
                  <small>{{ item.recommendation.reason }}</small>
                </td>
                <td class="action-cell">
                  <button type="button" @click="toggleDetail(item)">
                    {{ expandedSku === item.company_sku ? "收起详情" : "详情" }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedSku === item.company_sku" class="detail-row">
                <td colspan="7">
                  <div class="link-grid">
                    <article v-for="link in item.sales.links" :key="`${link.store_code}:${link.plid}`">
                      <header>
                        <strong>{{ link.store_name }}</strong>
                        <span>{{ link.lifecycle_label }}</span>
                      </header>
                      <code>PLID{{ link.plid }} · {{ link.offer_ids.join(" / ") }}</code>
                      <dl>
                        <div><dt>近30天实单</dt><dd>{{ link.recent_30_units }}件 / {{ link.recent_30_known_days }}天</dd></div>
                        <div><dt>近30天月化</dt><dd>{{ number(link.recent_monthly_velocity, 1) }}件</dd></div>
                        <div><dt>前30天实单</dt><dd>{{ link.previous_30_units }}件 / {{ link.previous_30_known_days }}天</dd></div>
                        <div><dt>前30天月化</dt><dd>{{ number(link.previous_monthly_velocity, 1) }}件</dd></div>
                        <div><dt>近期环比</dt><dd>{{ trend(link.recent_vs_previous_change_percentage) }}</dd></div>
                        <div><dt>可售观察</dt><dd>{{ link.buyable_observed_days }}/{{ link.observed_inventory_days }}天</dd></div>
                      </dl>
                      <small>
                        90天背景 {{ link.ordered_units }}件；完整 {{ link.verified_days }}天 · 部分 {{ link.partial_days }}天 · 缺失 {{ link.missing_days }}天
                      </small>
                      <a
                        class="link-detail-action"
                        :href="ownLinkDetailHref(link)"
                        target="_blank"
                        rel="noopener noreferrer"
                      >打开该链路完整详情</a>
                    </article>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <p v-if="!replenishmentItems.length" class="empty">当前筛选没有商品。</p>
      </section>

      <template v-else>
        <section class="radar-card-list" aria-label="新品监控类目">
          <article
            v-for="category in radarCategories"
            :key="category.category_id"
            class="radar-card category-card"
          >
            <header>
              <div class="radar-category-heading">
                <div class="radar-category-cover">
                  <img
                    v-if="selectionImageUrl(radarCategoryCoverImage(category))"
                    :key="radarCategoryCoverImage(category)"
                    :src="selectionImageUrl(radarCategoryCoverImage(category))"
                    :alt="`${category.category_name} 类目代表商品图`"
                    width="72"
                    height="72"
                    loading="lazy"
                    decoding="async"
                    fetchpriority="low"
                    referrerpolicy="no-referrer"
                    @error="retrySelectionImage($event, radarCategoryCoverImage(category))"
                  />
                  <span v-else>暂无图片</span>
                </div>
                <div>
                  <p>{{ category.market_leaf_name }} · 候选池 {{ category.cohort_basis.sample_size }} 条</p>
                  <h2>{{ category.category_name }}</h2>
                </div>
              </div>
              <span class="status-pill" :class="monitoringTone(category.monitoring.status)">
                {{ category.monitoring.label }}
              </span>
            </header>

            <div class="category-metrics">
              <article>
                <span>代表链接</span>
                <strong>{{ category.monitoring.active_link_count }}/{{ category.monitoring.representative_count }}</strong>
                <small>已在雷达 / 应监控</small>
              </article>
              <article>
                <span>基线达标</span>
                <strong>{{ category.monitoring.baseline_ready_link_count }}</strong>
                <small>至少需 {{ payload.policy.minimum_recent_signal_links }} 条近期信号</small>
              </article>
              <article>
                <span>合格近期链接</span>
                <strong>{{ category.monitoring.qualified_recent_signal_link_count }}</strong>
                <small>信号分 {{ category.monitoring.recent_signal_score }}</small>
              </article>
              <article>
                <span>近30天日期评论</span>
                <strong>{{ category.monitoring.recent_dated_review_count }}</strong>
                <small>{{ category.monitoring.recent_review_link_count }} 条链接有新评论</small>
              </article>
              <article>
                <span>最新评论日期</span>
                <strong>{{ category.monitoring.latest_review_date || "—" }}</strong>
                <small>累计评论不判近期热销</small>
              </article>
              <article>
                <span>近30天库存流出</span>
                <strong>{{ category.monitoring.recent_stock_outflow }}</strong>
                <small>公开信号，不等同订单</small>
              </article>
            </div>

            <section class="economics-panel">
              <div>
                <p class="eyebrow">WORKBOOK ECONOMICS ANCHOR</p>
                <h3>{{ category.economics_anchor.name }}</h3>
                <small>{{ category.economics_anchor.electrical_evidence }}</small>
              </div>
              <dl>
                <div><dt>单件体积</dt><dd>{{ number(category.economics_anchor.unit_cbm, 4) }} m³</dd></div>
                <div><dt>百件体积</dt><dd>{{ number(category.economics_anchor.cbm_per_100_units, 2) }} m³</dd></div>
                <div><dt>缓存海运毛利</dt><dd>{{ money(category.economics_anchor.sea_profit_rmb) }}</dd></div>
                <div><dt>缓存毛利率</dt><dd>{{ percent(category.economics_anchor.sea_margin_percentage) }}</dd></div>
              </dl>
              <p>{{ category.economics_anchor.workbook_observation || "工作簿只提供利润与箱规锚点。" }}</p>
              <small>
                {{ category.economics_anchor.source_sheet }}!第{{ category.economics_anchor.source_row }}行 · 工作簿评论不作为近期需求
              </small>
            </section>

            <button
              type="button"
              class="representative-links-toggle"
              :aria-expanded="isRadarCategoryLinksExpanded(category.category_id)"
              :aria-controls="radarCategoryLinksPanelId(category.category_id)"
              @click="toggleRadarCategoryLinks(category.category_id)"
            >
              <span>
                <strong>代表链接（{{ category.representatives.length }}）</strong>
                <small>点击查看最高价、最低价、评论最多及中位等差异链接</small>
              </span>
              <b>{{ isRadarCategoryLinksExpanded(category.category_id) ? "收起" : "展开" }}</b>
            </button>

            <div
              v-if="isRadarCategoryLinksExpanded(category.category_id)"
              :id="radarCategoryLinksPanelId(category.category_id)"
              class="representative-table-shell"
            >
              <table class="representative-table">
                <thead>
                  <tr>
                    <th>差异角色</th>
                    <th>代表链接</th>
                    <th>当前价格 / 市场结构</th>
                    <th>近30天评论日期</th>
                    <th>库存观察流出（7/15/30/60/90天）</th>
                    <th>监控基线</th>
                    <th>平台</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="item in category.representatives"
                    :key="item.plid"
                    class="clickable-detail-row"
                    role="button"
                    tabindex="0"
                    :aria-label="`打开 ${item.current.title || item.name} 的竞品雷达详情`"
                    title="点击整行查看雷达详情"
                    @pointerenter="scheduleRadarDetailPrefetch(item)"
                    @pointerleave="cancelRadarDetailPrefetchIntent"
                    @focus="scheduleRadarDetailPrefetch(item)"
                    @blur="cancelRadarDetailPrefetchIntent"
                    @click="openRadarDetail(item, $event)"
                    @keydown.enter.self="openRadarDetail(item, $event)"
                    @keydown.space.self.prevent="openRadarDetail(item, $event)"
                  >
                    <td>
                      <div class="role-tags">
                        <span v-for="role in item.role_labels" :key="role">{{ role }}</span>
                      </div>
                    </td>
                    <td class="representative-name">
                      <div class="representative-identity">
                        <div class="selection-product-image compact">
                          <img
                            v-if="selectionImageUrl(item.current.image_url)"
                            :src="selectionImageUrl(item.current.image_url)"
                            :alt="`${item.current.title || item.name} 商品图片`"
                            width="64"
                            height="64"
                            loading="lazy"
                            decoding="async"
                            referrerpolicy="no-referrer"
                            @error="retrySelectionImage($event, item.current.image_url)"
                          />
                          <span v-else>暂无图片</span>
                        </div>
                        <div>
                          <strong>{{ item.current.title || item.name }}</strong>
                          <code>PLID{{ item.plid }}</code>
                        </div>
                      </div>
                    </td>
                    <td>
                      <strong>{{ money(item.current.price_zar, "R") }}</strong>
                      <small>累计评论 {{ number(item.current.review_count_total) }}（仅结构）</small>
                      <small>评分 {{ number(item.current.rating, 1) }}</small>
                    </td>
                    <td>
                      <strong>{{ item.monitoring.recent_dated_review_count }} 条</strong>
                      <small>最新 {{ item.monitoring.latest_review_date || "—" }}</small>
                    </td>
                    <td class="stock-outflow-cell">
                      <div class="stock-outflow-windows" aria-label="近期库存观察流出">
                        <div v-for="days in stockOutflowWindowDays" :key="days">
                          <span>近{{ days }}天</span>
                          <strong :class="{ unavailable: observedStockOutflow(item, days) === null }">
                            {{ observedStockOutflowLabel(item, days) }}
                          </strong>
                        </div>
                      </div>
                      <small>
                        {{ item.monitoring.recent_observed_sales_through
                          ? `截至 ${item.monitoring.recent_observed_sales_through}`
                          : "暂无可用库存日期" }}
                      </small>
                      <small>库存观察，不等同订单</small>
                    </td>
                    <td>
                      <span class="status-pill" :class="monitoringTone(item.monitoring.status)">
                        {{ item.monitoring.label }}
                      </span>
                      <small>{{ item.monitoring.snapshot_count }} 次快照 · {{ item.monitoring.baseline_days }} 天</small>
                      <small>加入 {{ chinaDateTime(item.monitoring.added_at) }}</small>
                    </td>
                    <td class="action-cell">
                      <a
                        :href="item.url"
                        target="_blank"
                        rel="noopener noreferrer"
                        @click.stop
                      >平台</a>
                      <small>整行可查看详情</small>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <footer>
              <div>
                <span v-for="tag in category.economics_anchor.risk_tags" :key="tag">{{ tag }}</span>
              </div>
              <span class="status-pill" :class="categoryOpeningTone(category)">{{ category.decision.label }}</span>
              <small>{{ category.decision.note }}</small>
            </footer>
          </article>

          <p v-if="!radarCategories.length" class="empty">当前筛选没有新品类目。</p>
        </section>

        <details v-if="payload.retained_watchlist.length" class="retained-card">
          <summary>旧批次留观链接（{{ payload.retained_watchlist.length }}）</summary>
          <p>这些链接继续采集以保留基线，但不计入本轮近期多链接新品结论。</p>
          <div class="retained-list">
            <article
              v-for="item in payload.retained_watchlist"
              :key="item.plid"
              class="clickable-detail-card"
              role="button"
              tabindex="0"
              :aria-label="`打开 ${item.current.title || item.name} 的竞品雷达详情`"
              title="点击卡片查看雷达详情"
              @pointerenter="scheduleRadarDetailPrefetch(item)"
              @pointerleave="cancelRadarDetailPrefetchIntent"
              @focus="scheduleRadarDetailPrefetch(item)"
              @blur="cancelRadarDetailPrefetchIntent"
              @click="openRadarDetail(item, $event)"
              @keydown.enter.self="openRadarDetail(item, $event)"
              @keydown.space.self.prevent="openRadarDetail(item, $event)"
            >
              <div class="retained-identity">
                <div class="selection-product-image compact">
                  <img
                    v-if="selectionImageUrl(item.current.image_url)"
                    :src="selectionImageUrl(item.current.image_url)"
                    :alt="`${item.current.title || item.name} 商品图片`"
                    width="64"
                    height="64"
                    loading="lazy"
                    decoding="async"
                    referrerpolicy="no-referrer"
                    @error="retrySelectionImage($event, item.current.image_url)"
                  />
                  <span v-else>暂无图片</span>
                </div>
                <div>
                  <strong>{{ item.current.title || item.name }}</strong>
                  <code>PLID{{ item.plid }}</code>
                  <small>{{ item.retention_reason }}</small>
                </div>
              </div>
              <span class="status-pill" :class="monitoringTone(item.monitoring.status)">{{ item.monitoring.label }}</span>
              <small>近30天日期评论 {{ item.monitoring.recent_dated_review_count }} · 库存流出 {{ item.monitoring.recent_stock_outflow }}</small>
            </article>
          </div>
        </details>
      </template>

      <details class="method-card">
        <summary>口径与边界</summary>
        <ul>
          <li v-for="note in payload.evidence_notes" :key="note">{{ note }}</li>
        </ul>
        <p>{{ payload.policy.formula_note }}</p>
      </details>
    </template>

    <EmbeddedCompetitorDetail
      v-if="radarDetailHostReady"
      embedded-detail-only
      :can-operate="props.canOperate"
      :can-control-collection="props.canControlCollection"
      :is-admin="props.isAdmin"
      :current-username="props.currentUsername"
      :accessible-connected-store-count="props.accessibleConnectedStoreCount"
      :operating-connected-store-count="props.operatingConnectedStoreCount"
      own-store-scope="all"
      :requested-detail-plid="radarDetailPlid"
      :requested-detail-revision="radarDetailRevision"
      :requested-detail-prefetch-plid="radarDetailPrefetchPlid"
      :requested-detail-prefetch-revision="radarDetailPrefetchRevision"
      :on-permission-denied="props.onPermissionDenied"
      @detail-closed="closeRadarDetail"
    />
  </main>
</template>

<style scoped>
.container-selection-page {
  display: grid;
  gap: 18px;
  color: #1f342b;
}

.selection-hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 24px;
  border: 1px solid #d5ddcf;
  border-radius: 20px;
  background:
    radial-gradient(circle at 92% 8%, rgb(220 159 78 / 22%), transparent 34%),
    linear-gradient(135deg, #f9fbf5, #eef4ea);
}

.selection-hero h1,
.selection-hero p,
.radar-card h2,
.radar-card h3,
.radar-card p {
  margin: 0;
}

.selection-hero h1 {
  margin-bottom: 6px;
  font-size: clamp(28px, 4vw, 42px);
  letter-spacing: -.04em;
}

.selection-hero > div > p:last-child {
  max-width: 760px;
  color: #587065;
}

.eyebrow {
  margin-bottom: 8px !important;
  color: #a56521;
  font-size: 11px;
  font-weight: 900;
  letter-spacing: .14em;
}

button,
select,
input {
  font: inherit;
}

button,
.action-cell a,
.link-detail-action,
.radar-card footer a,
.retained-list a,
.retained-list button {
  border: 1px solid #b9cabb;
  border-radius: 10px;
  color: #264b3c;
  background: #fff;
  cursor: pointer;
}

button {
  padding: 9px 14px;
}

button:disabled {
  cursor: wait;
  opacity: .58;
}

.policy-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 18px;
  padding: 12px 16px;
  border-left: 4px solid #c8822d;
  border-radius: 8px 14px 14px 8px;
  background: #fff6e7;
  color: #64491f;
}

.policy-strip small {
  margin-left: auto;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.summary-grid article {
  display: grid;
  gap: 4px;
  min-height: 118px;
  padding: 18px;
  border: 1px solid #dce3d8;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 8px 24px rgb(45 72 58 / 5%);
}

.summary-grid span,
.summary-grid small,
td small,
.radar-card small {
  color: #6d7f75;
}

.summary-grid strong {
  align-self: end;
  font-size: 30px;
}

.state-card {
  padding: 24px;
  border: 1px solid #dce3d8;
  border-radius: 16px;
  background: #fff;
}

.state-card.error {
  color: #8d2d27;
  border-color: #e5bbb7;
  background: #fff7f5;
}

.view-tabs {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid #d6dfd4;
}

.view-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-bottom: -1px;
  padding: 12px 16px;
  border-color: transparent;
  border-bottom-color: #d6dfd4;
  border-radius: 12px 12px 0 0;
  background: transparent;
}

.view-tabs button.active {
  border-color: #d6dfd4 #d6dfd4 #fff;
  background: #fff;
}

.view-tabs b {
  display: grid;
  min-width: 23px;
  height: 23px;
  place-items: center;
  border-radius: 999px;
  background: #e9f0e8;
  font-size: 12px;
}

.toolbar {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar label {
  display: grid;
  gap: 4px;
  min-width: 220px;
  color: #66796f;
  font-size: 12px;
}

.toolbar input,
.toolbar select {
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid #cbd7cb;
  border-radius: 10px;
  color: #243a31;
  background: #fff;
}

.toolbar small {
  margin-left: auto;
  color: #718279;
}

.table-shell {
  overflow: auto;
  border: 1px solid #dce3d8;
  border-radius: 16px;
  background: #fff;
}

table {
  width: 100%;
  min-width: 1180px;
  border-collapse: collapse;
}

th,
td {
  padding: 14px 12px;
  border-bottom: 1px solid #edf0eb;
  text-align: left;
  vertical-align: top;
}

th {
  position: sticky;
  z-index: 1;
  top: 0;
  color: #66786f;
  background: #f7f9f5;
  font-size: 12px;
}

td {
  font-size: 13px;
}

td > strong,
td > small,
.product-cell code {
  display: block;
  margin-bottom: 4px;
}

.product-cell {
  display: grid;
  grid-template-columns: 28px 72px minmax(220px, 1fr);
  gap: 10px;
}

.selection-product-image {
  display: grid;
  width: 72px;
  height: 72px;
  overflow: hidden;
  place-items: center;
  border: 1px solid #dbe3d8;
  border-radius: 10px;
  color: #78877f;
  background: #f4f7f2;
  font-size: 10px;
  text-align: center;
}

.selection-product-image.compact {
  width: 64px;
  height: 64px;
  flex: 0 0 64px;
}

.selection-product-image img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #fff;
}

.rank {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border-radius: 8px;
  color: #80521f;
  background: #f8ead4;
  font-weight: 900;
}

code {
  color: #587066;
  font-family: Consolas, "SFMono-Regular", monospace;
  font-size: 11px;
}

.tag-row,
.radar-card footer > div {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.tag-row span,
.radar-card footer > div span {
  padding: 2px 7px;
  border-radius: 999px;
  color: #73511f;
  background: #f7eddc;
  font-size: 10px;
}

.status-pill {
  display: inline-flex;
  width: fit-content;
  margin-bottom: 6px;
  padding: 4px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 800;
}

.status-pill.positive { color: #1e6348; background: #e0f2e8; }
.status-pill.neutral { color: #3f5f66; background: #e7eff0; }
.status-pill.warning { color: #85571c; background: #faecd5; }
.status-pill.muted { color: #6d746d; background: #ecefeb; }
.status-pill.danger { color: #8e2e27; background: #fae3df; }
.negative { color: #a23f35; }

.action-cell {
  white-space: nowrap;
}

.action-cell button,
.action-cell a,
.radar-card footer a,
.retained-list a,
.retained-list button {
  display: inline-flex;
  margin: 0 0 6px 5px;
  padding: 6px 9px;
  text-decoration: none;
}

.detail-row td {
  padding: 14px 20px 20px;
  background: #f7faf6;
}

.link-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 10px;
}

.link-grid article {
  display: flex;
  flex-direction: column;
  padding: 12px;
  border: 1px solid #d8e1d6;
  border-radius: 12px;
  background: #fff;
}

.link-grid header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.link-grid header span {
  color: #8a5e28;
  font-size: 11px;
}

.link-detail-action {
  align-self: flex-start;
  margin-top: 10px;
  padding: 6px 9px;
  text-decoration: none;
}

.link-grid dl,
.radar-card dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin: 10px 0;
}

.link-grid dl div,
.radar-card dl div {
  padding: 7px;
  border-radius: 8px;
  background: #f4f7f2;
}

dt {
  color: #74837b;
  font-size: 10px;
}

dd {
  margin: 2px 0 0;
  font-weight: 800;
}

.radar-card-list {
  display: grid;
  gap: 14px;
}

.radar-card {
  display: grid;
  gap: 14px;
  padding: 18px;
  border: 1px solid #dce3d8;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 8px 24px rgb(45 72 58 / 5%);
}

.radar-card > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.radar-category-heading {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
}

.radar-category-cover {
  display: grid;
  width: 72px;
  height: 72px;
  flex: 0 0 72px;
  overflow: hidden;
  place-items: center;
  border: 1px solid #dbe3d8;
  border-radius: 11px;
  color: #78877f;
  background: #f4f7f2;
  font-size: 9px;
  text-align: center;
}

.radar-category-cover img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #fff;
}

.radar-category-heading > div:last-child {
  min-width: 0;
}

.radar-card > header p {
  color: #9b6b31;
  font-size: 11px;
  font-weight: 800;
}

.radar-category-selector {
  flex: 1 1 280px;
  max-width: 420px;
}

.radar-category-selector select {
  width: 100%;
  min-width: 0;
}

.radar-card h2 {
  margin-top: 3px;
  font-size: 17px;
}

.category-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}

.category-metrics article {
  display: grid;
  gap: 3px;
  min-width: 0;
  padding: 10px;
  border-radius: 10px;
  background: #f4f7f2;
}

.category-metrics span,
.category-metrics small {
  color: #718179;
  font-size: 10px;
}

.category-metrics strong {
  overflow-wrap: anywhere;
  font-size: 16px;
}

.economics-panel {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(320px, 1fr) minmax(220px, 1fr);
  align-items: center;
  gap: 14px;
  padding: 13px;
  border: 1px solid #eadbc3;
  border-radius: 12px;
  background: #fffaf1;
}

.economics-panel dl {
  margin: 0;
}

.economics-panel > p,
.economics-panel > small {
  color: #6f6250;
  font-size: 11px;
}

.representative-links-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  padding: 12px 14px;
  border-color: #dce3d8;
  border-radius: 12px;
  color: #203b30;
  background: #f6f9f5;
  text-align: left;
}

.representative-links-toggle:hover,
.representative-links-toggle:focus-visible {
  border-color: #8ead99;
  background: #eef5ef;
}

.representative-links-toggle > span {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.representative-links-toggle strong {
  font-size: 13px;
}

.representative-links-toggle small {
  color: #718179;
  font-size: 10px;
}

.representative-links-toggle b {
  flex: 0 0 auto;
  color: #315e4b;
  font-size: 12px;
}

.representative-table-shell {
  overflow-x: auto;
  border: 1px solid #e1e7dd;
  border-radius: 12px;
}

.representative-table {
  min-width: 1040px;
}

.representative-table th {
  position: static;
}

.representative-table td {
  padding: 10px;
}

.stock-outflow-cell {
  min-width: 150px;
}

.stock-outflow-windows {
  display: grid;
  gap: 4px;
  margin-bottom: 6px;
}

.stock-outflow-windows > div {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  align-items: baseline;
  gap: 6px;
}

.stock-outflow-windows span {
  color: #718179;
  font-size: 10px;
}

.stock-outflow-windows strong {
  font-size: 12px;
  white-space: nowrap;
}

.stock-outflow-windows strong.unavailable {
  color: #849088;
  font-weight: 600;
}

.stock-outflow-cell > small {
  display: block;
  margin-top: 3px;
  font-size: 9px;
}

.representative-table tr:last-child td {
  border-bottom: 0;
}

.clickable-detail-row {
  cursor: pointer;
  outline: none;
}

.clickable-detail-row td {
  transition: background-color 120ms ease, box-shadow 120ms ease;
}

.clickable-detail-row:hover td,
.clickable-detail-row:focus-visible td {
  background: #f1f7f1;
}

.clickable-detail-row:focus-visible td:first-child {
  box-shadow: inset 3px 0 #4f8068;
}

.clickable-detail-row .action-cell small {
  display: block;
  color: #718179;
  font-size: 9px;
  white-space: normal;
}

.representative-name {
  min-width: 220px;
  max-width: 330px;
}

.representative-identity,
.retained-identity {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.representative-identity > div:last-child,
.retained-identity > div:last-child {
  min-width: 0;
}

.role-tags {
  display: flex;
  max-width: 180px;
  flex-wrap: wrap;
  gap: 4px;
}

.role-tags span {
  padding: 3px 6px;
  border-radius: 999px;
  color: #78521f;
  background: #f8ecd9;
  font-size: 9px;
  font-weight: 800;
}

.radar-body {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.radar-body section {
  padding: 12px;
  border-radius: 12px;
  background: #f7f9f5;
}

.radar-card h3 {
  font-size: 13px;
}

.radar-card section > p {
  color: #536b60;
  font-size: 12px;
}

.radar-card footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid #edf0eb;
}

.radar-card footer > div {
  flex: 1 1 220px;
}

.radar-card footer > strong {
  color: #315e4b;
  font-size: 12px;
}

.radar-card footer > small {
  flex: 1 1 420px;
}

.retained-card {
  padding: 14px 16px;
  border: 1px solid #dce3d8;
  border-radius: 14px;
  background: #fff;
}

.retained-card summary {
  cursor: pointer;
  font-weight: 800;
}

.retained-card > p {
  color: #65776e;
  font-size: 12px;
}

.retained-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.retained-list article {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 6px 10px;
  padding: 10px;
  border-radius: 10px;
  background: #f7f9f5;
}

.retained-list article.clickable-detail-card {
  cursor: pointer;
  outline: none;
  transition: border-color 120ms ease, background-color 120ms ease, box-shadow 120ms ease;
}

.retained-list article.clickable-detail-card:hover,
.retained-list article.clickable-detail-card:focus-visible {
  background: #edf6ef;
  box-shadow: inset 3px 0 #4f8068;
}

.retained-identity small,
.retained-identity code {
  display: block;
  margin-top: 4px;
}

.retained-list article > small {
  color: #6d7f75;
  font-size: 10px;
}

.retained-list a,
.retained-list button {
  width: fit-content;
  padding: 5px 8px;
  text-decoration: none;
}

.empty {
  grid-column: 1 / -1;
  padding: 28px;
  color: #75847c;
  text-align: center;
}

.method-card {
  padding: 14px 16px;
  border: 1px solid #dce3d8;
  border-radius: 14px;
  background: #fff;
}

.method-card summary {
  cursor: pointer;
  font-weight: 800;
}

.method-card li,
.method-card p {
  margin-top: 8px;
  color: #65776e;
  font-size: 12px;
}

@media (max-width: 1080px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .category-metrics {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .economics-panel {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 680px) {
  .selection-hero {
    display: grid;
    padding: 18px;
  }

  .summary-grid,
  .retained-list,
  .category-metrics {
    grid-template-columns: 1fr;
  }

  .policy-strip small,
  .toolbar small {
    width: 100%;
    margin-left: 0;
  }

  .toolbar label {
    width: 100%;
  }

}
</style>
