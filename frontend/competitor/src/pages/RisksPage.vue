<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchRisks } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import type { AnomalyItem, RiskPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const data = ref<RiskPayload | null>(null);
const tab = ref<"anomalies" | "quality">("anomalies");
const anomalyScope = ref<"latest" | "all">("latest");
const anomalyQuery = ref("");
const anomalyLevel = ref<"all" | "priority" | "notice">("all");
const anomalyType = ref("all");
const loading = ref(true);
const selectedAnomaly = ref<AnomalyItem | null>(null);
const failedImageUrls = ref<Set<string>>(new Set());
let returnFocusElement: HTMLElement | null = null;

const scopedAnomalies = computed(() =>
  anomalyScope.value === "latest"
    ? data.value?.latest_anomalies ?? []
    : data.value?.anomalies ?? [],
);
const anomalyTypeOptions = computed(() => {
  const options = new Map<string, string>();
  for (const item of scopedAnomalies.value) {
    options.set(item.anomaly_type, item.anomaly_label);
  }
  return [...options.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((left, right) => left.label.localeCompare(right.label, "zh-CN"));
});
const anomalies = computed(() => {
  const query = anomalyQuery.value.trim().toLocaleLowerCase("zh-CN");
  return scopedAnomalies.value.filter((item) => {
    const priority = isPriorityAnomaly(item);
    const matchesLevel =
      anomalyLevel.value === "all" ||
      (anomalyLevel.value === "priority" && priority) ||
      (anomalyLevel.value === "notice" && !priority);
    const matchesType = anomalyType.value === "all" || item.anomaly_type === anomalyType.value;
    const matchesQuery =
      !query ||
      [item.offer_id, item.anomaly_label, item.explanation, item.event_date].some((value) =>
        value.toLocaleLowerCase("zh-CN").includes(query),
      );
    return matchesLevel && matchesType && matchesQuery;
  });
});
const hasActiveAnomalyFilters = computed(
  () => anomalyQuery.value.trim() !== "" || anomalyLevel.value !== "all" || anomalyType.value !== "all",
);
const selectedImageUrl = computed(() => {
  const url = String(selectedAnomaly.value?.image_url ?? "").trim();
  return url && !failedImageUrls.value.has(url)
    ? productThumbnailUrl(url, PRODUCT_IMAGE_SIZE.detail)
    : "";
});

watch(() => props.asOf, load, { immediate: true });
watch(anomalyTypeOptions, (options) => {
  if (
    anomalyType.value !== "all" &&
    !options.some((option) => option.value === anomalyType.value)
  ) {
    anomalyType.value = "all";
  }
});
watch(selectedAnomaly, (item) => {
  document.body.style.overflow = item ? "hidden" : "";
});

onMounted(() => {
  window.addEventListener("keydown", handleWindowKeydown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleWindowKeydown);
  document.body.style.overflow = "";
});

function isPriorityAnomaly(item: AnomalyItem) {
  return item.anomaly_type !== "non_buyable";
}

function clearAnomalyFilters() {
  anomalyQuery.value = "";
  anomalyLevel.value = "all";
  anomalyType.value = "all";
}

function openAnomalyDetail(item: AnomalyItem, event?: Event) {
  returnFocusElement =
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  selectedAnomaly.value = item;
}

function closeAnomalyDetail() {
  selectedAnomaly.value = null;
  const target = returnFocusElement;
  returnFocusElement = null;
  if (target) void nextTick(() => target.focus());
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && selectedAnomaly.value) closeAnomalyDetail();
}

function formatNumber(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("zh-CN").format(value);
}

function formatPercent(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}%`;
}

function formatCurrency(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value);
}

function firstListingLabel(item: AnomalyItem) {
  return item.first_listed_at || "暂无记录";
}

function restockLabel(item: AnomalyItem) {
  if (!item.latest_restock_date) return "暂无平台库存增加记录";
  const increase =
    item.latest_restock_increase === null || item.latest_restock_increase === undefined
      ? ""
      : ` · 较前次 +${formatNumber(item.latest_restock_increase)}`;
  return `${item.latest_restock_date}${increase}`;
}

function markSelectedImageUnavailable() {
  const url = String(selectedAnomaly.value?.image_url ?? "").trim();
  if (!url) return;
  const failed = new Set(failedImageUrls.value);
  failed.add(url);
  failedImageUrls.value = failed;
}

async function load() {
  loading.value = true;
  try {
    data.value = await fetchRisks(props.asOf);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="erp-page risks-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">CONTROL TOWER</p>
        <h2>把经营异常和数据质量放在同一处处理</h2>
      </div>
      <div class="page-tabs">
        <button :class="{ active: tab === 'anomalies' }" @click="tab = 'anomalies'">经营异常</button>
        <button :class="{ active: tab === 'quality' }" @click="tab = 'quality'">数据质量</button>
      </div>
    </div>
    <div v-if="loading" class="state-card">正在读取风险数据……</div>
    <template v-else-if="data">
      <section class="mini-kpis">
        <article><span>最新异常商品</span><strong>{{ data.summary.latest_anomaly_products }}</strong></article>
        <article><span>最新异常记录</span><strong>{{ data.summary.latest_anomaly_records }}</strong></article>
        <article><span>质量事件</span><strong>{{ data.summary.quality_events }}</strong></article>
        <article><span>未知销售状态</span><strong>{{ data.summary.unknown_sale_status }}</strong></article>
      </section>

      <section v-if="tab === 'anomalies'" class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">ANOMALY EVENTS</p>
            <h3>异常商品</h3>
          </div>
          <div class="segmented">
            <button :class="{ active: anomalyScope === 'latest' }" @click="anomalyScope = 'latest'">
              最新指标日
            </button>
            <button :class="{ active: anomalyScope === 'all' }" @click="anomalyScope = 'all'">
              全部历史
            </button>
          </div>
        </div>
        <p class="method-note">
          同一商品触发多种异常时保留多条记录；最新指标日为 {{ data.latest_metric_date || "暂无" }}。
        </p>
        <div class="risk-filter-bar">
          <label class="risk-filter-field risk-filter-search">
            <span>关键词</span>
            <input v-model="anomalyQuery" type="search" placeholder="商品编号、异常名称或说明" />
          </label>
          <label class="risk-filter-field">
            <span>提示级别</span>
            <select v-model="anomalyLevel">
              <option value="all">全部级别</option>
              <option value="priority">重点提示</option>
              <option value="notice">普通提示</option>
            </select>
          </label>
          <label class="risk-filter-field">
            <span>异常类型</span>
            <select v-model="anomalyType">
              <option value="all">全部异常</option>
              <option v-for="option in anomalyTypeOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </label>
          <div class="risk-filter-summary" aria-live="polite">
            <span>显示 {{ anomalies.length }} / {{ scopedAnomalies.length }} 条</span>
            <button type="button" :disabled="!hasActiveAnomalyFilters" @click="clearAnomalyFilters">
              清除筛选
            </button>
          </div>
        </div>
        <div v-if="!scopedAnomalies.length" class="state-card slim">当前范围没有异常记录。</div>
        <div v-else-if="!anomalies.length" class="state-card slim">
          没有符合当前筛选条件的异常记录。
        </div>
        <div v-else class="risk-list">
          <article
            v-for="item in anomalies"
            :key="`${item.event_date}-${item.offer_id}-${item.anomaly_type}`"
            v-memo="[item]"
            :class="isPriorityAnomaly(item) ? 'priority' : 'notice'"
            tabindex="0"
            role="button"
            aria-haspopup="dialog"
            :aria-label="`查看 ${item.title || item.offer_id} 的${item.anomaly_label}详情`"
            @click="openAnomalyDetail(item, $event)"
            @keydown.enter="openAnomalyDetail(item, $event)"
            @keydown.space.prevent="openAnomalyDetail(item, $event)"
          >
            <span class="risk-signal" :class="isPriorityAnomaly(item) ? 'priority' : 'notice'">
              <span class="risk-signal-dot" aria-hidden="true"></span>
              {{ isPriorityAnomaly(item) ? "重点提示" : "提示" }}
            </span>
            <div class="risk-card-copy">
              <strong>{{ item.anomaly_label }}</strong>
              <p>{{ item.explanation }}</p>
              <b>{{ item.title || item.sku || item.offer_id }}</b>
              <small>{{ item.event_date }} · 商品编号 {{ item.offer_id }} · 点击查看详情</small>
            </div>
          </article>
        </div>
      </section>

      <section v-else class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">DATA QUALITY</p>
            <h3>质量事件</h3>
          </div>
          <span>页面只读展示</span>
        </div>
        <div v-if="!data.quality_events.length" class="state-card slim">当前没有已记录的质量事件。</div>
        <div v-else class="erp-table-wrap">
          <table class="erp-table">
            <thead><tr><th>日期</th><th>事件</th><th>级别</th><th>商品编号</th><th>详情</th></tr></thead>
            <tbody>
              <tr v-for="item in data.quality_events" :key="item.event_id">
                <td>{{ item.event_date }}</td>
                <td><strong>{{ item.event_label }}</strong></td>
                <td>{{ item.severity_label }}</td>
                <td>{{ item.offer_id || "—" }}</td>
                <td>{{ item.details_text }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <Teleport to="body">
      <div
        v-if="selectedAnomaly"
        class="competitor-modal-backdrop risk-modal-backdrop"
        @click.self="closeAnomalyDetail"
      >
        <section
          class="competitor-modal risk-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="risk-detail-title"
        >
          <header class="competitor-modal-header risk-modal-header">
            <div>
              <p class="section-kicker">ANOMALY DETAIL</p>
              <h2 id="risk-detail-title">
                {{ selectedAnomaly.title || selectedAnomaly.sku || selectedAnomaly.offer_id }}
              </h2>
              <span>
                {{ selectedAnomaly.anomaly_label }} · 异常发生日 {{ selectedAnomaly.event_date }}
              </span>
            </div>
            <button
              type="button"
              class="competitor-modal-close"
              aria-label="关闭异常商品详情"
              @click="closeAnomalyDetail"
            >
              ×
            </button>
          </header>

          <div class="risk-modal-content">
            <section class="risk-product-hero">
              <div class="risk-product-image">
                <img
                  v-if="selectedImageUrl"
                  :src="selectedImageUrl"
                  :alt="`${selectedAnomaly.title || selectedAnomaly.sku || selectedAnomaly.offer_id} 商品图片`"
                  width="640"
                  height="640"
                  decoding="async"
                  fetchpriority="high"
                  referrerpolicy="no-referrer"
                  @error="markSelectedImageUnavailable"
                />
                <span v-else>暂无图片</span>
              </div>
              <div class="risk-product-identity">
                <span
                  class="risk-signal"
                  :class="isPriorityAnomaly(selectedAnomaly) ? 'priority' : 'notice'"
                >
                  <span class="risk-signal-dot" aria-hidden="true"></span>
                  {{ isPriorityAnomaly(selectedAnomaly) ? "重点提示" : "提示" }}
                </span>
                <h3>{{ selectedAnomaly.anomaly_label }}</h3>
                <p>{{ selectedAnomaly.explanation }}</p>
                <div class="risk-identity-grid">
                  <span><small>平台 SKU</small><b>{{ selectedAnomaly.sku || "缺失" }}</b></span>
                  <span><small>商品编号</small><b>{{ selectedAnomaly.offer_id }}</b></span>
                  <span><small>平台商品编号</small><b>{{ selectedAnomaly.tsin_id || "—" }}</b></span>
                  <span><small>条码</small><b>{{ selectedAnomaly.barcode || "—" }}</b></span>
                </div>
              </div>
            </section>

            <section class="risk-modal-metrics" aria-label="当前商品经营指标">
              <article>
                <small>近7日下单</small>
                <strong>{{ formatNumber(selectedAnomaly.ordered_units_7_days) }}</strong>
              </article>
              <article>
                <small>平台可售库存</small>
                <strong>{{ formatNumber(selectedAnomaly.total_stock) }}</strong>
              </article>
              <article>
                <small>近30天浏览量</small>
                <strong>{{ formatNumber(selectedAnomaly.page_views_30_days) }}</strong>
              </article>
              <article>
                <small>近30天转化率</small>
                <strong>{{ formatPercent(selectedAnomaly.conversion_percentage_30_days) }}</strong>
              </article>
              <article>
                <small>当前售价</small>
                <strong>{{ formatCurrency(selectedAnomaly.selling_price) }}</strong>
                <span>建议零售价 {{ formatCurrency(selectedAnomaly.rrp) }}</span>
              </article>
              <article>
                <small>最新日下单金额</small>
                <strong>{{ formatCurrency(selectedAnomaly.ordered_revenue) }}</strong>
                <span>有效销售 {{ formatNumber(selectedAnomaly.effective_units) }} 件</span>
              </article>
            </section>

            <div class="risk-modal-sections">
              <article class="risk-detail-panel anomaly">
                <p class="section-kicker">ANOMALY RECORD</p>
                <h3>异常记录</h3>
                <dl>
                  <div><dt>异常类型</dt><dd>{{ selectedAnomaly.anomaly_label }}</dd></div>
                  <div><dt>异常发生日</dt><dd>{{ selectedAnomaly.event_date }}</dd></div>
                  <div>
                    <dt>提示级别</dt>
                    <dd>{{ isPriorityAnomaly(selectedAnomaly) ? "重点提示" : "普通提示" }}</dd>
                  </div>
                  <div><dt>商品状态</dt><dd>{{ selectedAnomaly.status_label || "—" }}</dd></div>
                </dl>
              </article>

              <article class="risk-detail-panel">
                <p class="section-kicker">PRODUCT TIMELINE</p>
                <h3>商品时效与补货</h3>
                <dl>
                  <div>
                    <dt>
                      {{
                        selectedAnomaly.first_listed_source === "platform"
                          ? "首次上架"
                          : "首次上架 · 本库最早记录"
                      }}
                    </dt>
                    <dd>{{ firstListingLabel(selectedAnomaly) }}</dd>
                  </div>
                  <div>
                    <dt>最近补货时间 · 平台库存增加记录</dt>
                    <dd>{{ restockLabel(selectedAnomaly) }}</dd>
                  </div>
                  <div>
                    <dt>当前经营数据截止日</dt>
                    <dd>{{ selectedAnomaly.metric_date || "暂无" }}</dd>
                  </div>
                </dl>
              </article>
            </div>

            <p class="risk-modal-note">
              异常发生日与当前经营数据截止日分别展示；近30天浏览量是平台滚动窗口值，不是当天访客数。
            </p>
          </div>

          <footer class="competitor-modal-actions">
            <button type="button" @click="closeAnomalyDetail">关闭详情</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>
