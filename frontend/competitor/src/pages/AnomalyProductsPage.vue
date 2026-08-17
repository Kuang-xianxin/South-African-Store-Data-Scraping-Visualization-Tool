<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  ANOMALY_PRODUCT_VIEWS,
  ANOMALY_VIEW_LABELS,
  countForAnomalyView,
  itemsForAnomalyView,
  type AnomalyProductView,
} from "../anomalyProducts";
import { fetchAnomalyProducts } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import type { AnomalyProductItem, AnomalyProductPayload } from "../types";
import CompetitorsPage from "./CompetitorsPage.vue";

const props = defineProps<{
  asOf: string;
  canViewCompetitors?: boolean;
  currentStoreCode?: string;
  currentStoreName?: string;
  onPermissionDenied?: () => void;
}>();

const payload = ref<AnomalyProductPayload | null>(null);
const activeView = ref<AnomalyProductView>("sudden_sales_stop");
const slowDays = ref(7);
const query = ref("");
const loading = ref(true);
const error = ref("");
const failedImages = ref(new Set<string>());
const detailRequest = ref({ plid: "", revision: 0 });

const slowDayOptions = computed(
  () => payload.value?.rules.slow_day_options ?? [4, 7, 10, 15, 20, 30],
);
const viewItems = computed(() =>
  itemsForAnomalyView(payload.value, activeView.value, slowDays.value),
);
const filteredItems = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return viewItems.value;
  return viewItems.value.filter((item) =>
    [item.title, item.sku, item.offer_id, item.plid, item.tsin_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle)),
  );
});

watch(() => props.asOf, loadAnomalies, { immediate: true });

async function loadAnomalies(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchAnomalyProducts(props.asOf);
    if (!slowDayOptions.value.includes(slowDays.value)) {
      slowDays.value = slowDayOptions.value[0] ?? 4;
    }
  } catch (reason) {
    payload.value = null;
    error.value = reason instanceof Error ? reason.message : "异常商品读取失败";
  } finally {
    loading.value = false;
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
  detailRequest.value = {
    plid: item.plid,
    revision: detailRequest.value.revision + 1,
  };
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
  return typeof value === "number"
    ? new Intl.NumberFormat("zh-CN").format(value)
    : "—";
}

function currency(value: number | null): string {
  return typeof value === "number"
    ? new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value)
    : "—";
}

function percent(value: number | null): string {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "—";
}

function noSalesLabel(item: AnomalyProductItem): string {
  const prefix = item.no_sales_days_exact ? "" : "至少 ";
  return `${prefix}${item.no_sales_days} 天`;
}

function statusInventoryLabel(item: AnomalyProductItem): string {
  const parts = [
    item.available_stock > 0 ? `现货 ${number(item.available_stock)}` : "",
    item.receiving_stock > 0 ? `收货中 ${number(item.receiving_stock)}` : "",
  ].filter(Boolean);
  return parts.join(" · ") || "到仓库存明细待补充";
}

function onWayInventoryLabel(item: AnomalyProductItem): string {
  return item.on_way_stock > 0
    ? ` · 在途 ${number(item.on_way_stock)}（不计入）`
    : "";
}

function emptyMessage(): string {
  if (query.value.trim()) return "当前类型下没有匹配搜索条件的商品。";
  if (activeView.value === "slow_moving") {
    return `当前没有有库存且连续 ${slowDays.value} 天未动销的可售商品。`;
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
        <p class="hero-copy">
          各类异常独立展示；零销量只采用已核验完成的南非业务日，不可售库存只统计已经到平台仓的商品。
        </p>
      </div>
      <div class="evidence-card">
        <span>销量证据截至</span>
        <strong>{{ payload?.data_through || "暂无完整业务日" }}</strong>
        <small>请求截止 {{ payload?.requested_as_of || props.asOf }} · 南非日期</small>
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
          <label for="slow-days">滞销选择器</label>
          <select id="slow-days" v-model.number="slowDays">
            <option v-for="days in slowDayOptions" :key="days" :value="days">
              有库存 {{ days }} 天没动销
            </option>
          </select>
          <small>仅统计状态为可购买且当前现货大于 0 的商品</small>
        </template>
        <template v-else>
          <span>当前规则</span>
          <strong>{{ ANOMALY_VIEW_LABELS[activeView] }}</strong>
          <small>只统计现货和平台收货中，在途不计入异常库存</small>
        </template>
      </div>
      <label class="anomaly-search">
        <span class="sr-only">搜索异常商品</span>
        <input
          v-model="query"
          type="search"
          placeholder="搜索商品名、SKU、Offer ID 或 PLID"
        />
        <strong>{{ filteredItems.length }} 个商品</strong>
      </label>
    </section>

    <div v-if="loading" class="anomaly-state">正在核对完整业务日与商品状态……</div>
    <div v-else-if="error" class="anomaly-state error" role="alert">
      <strong>异常商品暂时无法读取</strong>
      <span>{{ error }}</span>
      <button type="button" @click="loadAnomalies">重新读取</button>
    </div>
    <section v-else-if="filteredItems.length" class="anomaly-grid">
      <button
        v-for="item in filteredItems"
        :key="`${activeView}-${item.offer_id}`"
        type="button"
        class="anomaly-card"
        aria-haspopup="dialog"
        :aria-label="`在当前页面查看 ${item.title} 的自有链接详情`"
        @click="openOwnLinkDetail(item)"
      >
        <div class="card-topline">
          <span class="type-badge" :class="`type-${item.anomaly_type}`">
            {{ item.anomaly_label }}
          </span>
          <span class="status-badge">{{ item.offer_status_label }}</span>
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
            <p>{{ item.sku || "无 SKU" }} · Offer {{ item.offer_id }}</p>
            <small>PLID {{ item.plid }}</small>
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
          <small>上次动销 {{ item.last_sale_on || "现有完整历史内未见销量" }}</small>
        </div>
        <div v-else class="primary-signal disabled">
          <span>已到平台仓</span>
          <strong>{{ number(item.inventory_units) }} 件</strong>
          <small>
            {{ statusInventoryLabel(item) }}{{ onWayInventoryLabel(item) }}
          </small>
        </div>

        <dl class="card-metrics">
          <div>
            <dt>售价</dt>
            <dd>{{ currency(item.selling_price) }}</dd>
          </div>
          <div>
            <dt>现货</dt>
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
          <span>数据截至 {{ item.data_through || "暂无" }}</span>
          <strong>查看完整商品详情</strong>
        </div>
      </button>
    </section>
    <div v-else class="anomaly-state empty">
      <strong>{{ emptyMessage() }}</strong>
      <span>切换上方异常类型或调整搜索条件继续查看。</span>
    </div>

    <CompetitorsPage
      v-if="props.canViewCompetitors"
      detail-only
      :can-operate="false"
      :can-control-collection="false"
      :is-admin="false"
      :current-store-code="props.currentStoreCode"
      :current-store-name="props.currentStoreName"
      own-store-scope="current"
      :requested-detail-plid="detailRequest.plid"
      :requested-detail-revision="detailRequest.revision"
      :on-permission-denied="props.onPermissionDenied"
    />
  </div>
</template>

<style scoped>
.anomaly-page {
  display: grid;
  gap: 18px;
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
  grid-template-columns: repeat(5, minmax(0, 1fr));
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
  transition: border-color 180ms ease, box-shadow 180ms ease, transform 180ms ease;
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
}
</style>
