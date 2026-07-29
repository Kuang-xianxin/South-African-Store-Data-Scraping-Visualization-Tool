<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchRisks } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { formatChinaDateTime } from "../time";
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
const selectedEvidence = computed(() => buildAnomalyEvidence(selectedAnomaly.value));
const selectedSalesChartMaximum = computed(() => {
  const values = selectedEvidence.value?.salesSeries
    ?.map((point) => point.ordered_units)
    .filter((value): value is number => value !== null);
  return Math.max(1, ...(values ?? [0]));
});

interface EvidenceMetric {
  label: string;
  value: string;
  hint: string;
  tone?: "trigger" | "threshold" | "context";
}

interface AnomalyEvidence {
  title: string;
  conclusion: string;
  metrics: EvidenceMetric[];
  salesSeries?: Array<{
    date: string;
    ordered_units: number | null;
  }>;
  salesSeriesCoveredDays?: number;
}

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

function formatDecimal(value: number | null | undefined, suffix = "") {
  return value === null || value === undefined
    ? "—"
    : `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}${suffix}`;
}

function signedDifference(value: number | null) {
  if (value === null) return "—";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${formatDecimal(value, " 件/日")}`;
}

function numericDifference(left: number | undefined, right: number | undefined) {
  return left === undefined || right === undefined ? null : left - right;
}

function shortDate(value: string) {
  return value.length >= 10 ? value.slice(5) : value;
}

function salesBarHeight(value: number | null) {
  if (value === null) return "0%";
  return `${(Math.max(0, value) / selectedSalesChartMaximum.value) * 100}%`;
}

function trafficSalesMetric(details: NonNullable<AnomalyItem["details"]>): EvidenceMetric {
  const days = details.sales_window_days ?? 0;
  const complete = details.sales_window_complete === true && days === 30;
  const dateRange =
    details.sales_window_start && details.sales_window_end
      ? `${details.sales_window_start} 至 ${details.sales_window_end}`
      : "暂无可汇总的逐日销量";
  return {
    label: complete ? "近30天销量" : `当前记录${days}天销量`,
    value:
      details.sales_window_total_units === null ||
      details.sales_window_total_units === undefined
        ? "—"
        : formatDecimal(details.sales_window_total_units, " 件"),
    hint: complete ? "截至异常发生日的30个自然日" : `暂未覆盖30天；${dateRange}`,
    tone: "context",
  };
}

function buildAnomalyEvidence(item: AnomalyItem | null): AnomalyEvidence | null {
  if (!item || item.anomaly_type === "non_buyable") return null;
  const details = item.details ?? {};

  if (item.anomaly_type === "sales_drop" || item.anomaly_type === "sales_spike") {
    const shortDays = details.short_window_days ?? 3;
    const longDays = details.long_window_days ?? 15;
    const shortAverage = details.short_window_average_units;
    const longAverage = details.long_window_average_units;
    const comparison = item.anomaly_type === "sales_drop" ? "低于" : "高于";
    return {
      title: "销量趋势触发证据",
      conclusion: `近${shortDays}日平均${comparison}近${longDays}日平均，因此触发“${item.anomaly_label}”。`,
      salesSeries: details.sales_daily_series ?? [],
      salesSeriesCoveredDays: details.sales_series_covered_days ?? 0,
      metrics: [
        {
          label: `近${shortDays}日平均`,
          value: formatDecimal(shortAverage, " 件/日"),
          hint: "本次判定值",
          tone: "trigger",
        },
        {
          label: `近${longDays}日平均`,
          value: formatDecimal(longAverage, " 件/日"),
          hint: "长期对比基准",
          tone: "threshold",
        },
        {
          label: "两组均值差",
          value: signedDifference(numericDifference(shortAverage, longAverage)),
          hint: `近${shortDays}日减近${longDays}日`,
          tone: "context",
        },
      ],
    };
  }

  if (item.anomaly_type === "high_views_low_conversion") {
    return {
      title: "高浏览低转化触发证据",
      conclusion: "浏览量达到高浏览边界，同时转化率低于低转化边界。",
      metrics: [
        {
          label: "实际近30天浏览量",
          value: formatNumber(details.page_views_30_days),
          hint: "需大于或等于高浏览边界",
          tone: "trigger",
        },
        trafficSalesMetric(details),
        {
          label: "实际近30天转化率",
          value: formatPercent(details.conversion_percentage_30_days),
          hint: "需低于低转化边界",
          tone: "trigger",
        },
        {
          label: "高浏览边界",
          value: formatDecimal(details.high_views_threshold),
          hint: "当日商品分布阈值",
          tone: "threshold",
        },
        {
          label: "低转化边界",
          value: formatPercent(details.low_conversion_threshold),
          hint: "当日商品分布阈值",
          tone: "threshold",
        },
      ],
    };
  }

  if (item.anomaly_type === "low_views_high_conversion") {
    return {
      title: "低浏览高转化触发证据",
      conclusion: "集中展示该商品的近30天浏览量、当前已记录销量和近30天转化率。",
      metrics: [
        {
          label: "实际近30天浏览量",
          value: formatNumber(details.page_views_30_days),
          hint: "平台近30天浏览量",
          tone: "trigger",
        },
        trafficSalesMetric(details),
        {
          label: "实际近30天转化率",
          value: formatPercent(details.conversion_percentage_30_days),
          hint: "平台近30天转化率",
          tone: "trigger",
        },
      ],
    };
  }

  if (item.anomaly_type === "suspected_stockout") {
    const statusLabels: Record<string, string> = {
      buyable: "可购买",
      not_buyable: "不可购买",
      disabled_by_seller: "卖家已停用",
      disabled_by_takealot: "平台已停用",
    };
    const rawStatus = details.offer_status ?? "";
    const status = (statusLabels[rawStatus] ?? rawStatus) || "未知";
    const statusHint =
      rawStatus === "buyable"
        ? "异常时仍处于可购买状态"
        : (details.recent_7_day_units ?? 0) > 0
          ? "本条由异常日前 7 日有下单触发"
          : "异常发生时的平台状态";
    return {
      title: "疑似断货触发证据",
      conclusion: "异常发生时平台可售库存为 0，且商品仍可购买或异常日前 7 日有下单。",
      metrics: [
        {
          label: "异常时平台可售库存",
          value: formatNumber(details.total_stock),
          hint: "断货触发值为 0 件",
          tone: "trigger",
        },
        {
          label: "异常日前 7 日下单",
          value: formatDecimal(details.recent_7_day_units, " 件"),
          hint: "不包含异常发生日",
          tone: "context",
        },
        {
          label: "异常时商品状态",
          value: status,
          hint: statusHint,
          tone: "context",
        },
      ],
    };
  }

  if (item.anomaly_type === "stale_offer_snapshot") {
    return {
      title: "数据停止更新触发证据",
      conclusion: "最近 Offer 快照距异常计算时间已超过允许的小时数。",
      metrics: [
        {
          label: "最近 Offer 采集时间",
          value: formatChinaDateTime(details.captured_at ?? null),
          hint: "北京时间",
          tone: "context",
        },
        {
          label: "采集距今",
          value: formatDecimal(details.stale_age_hours, " 小时"),
          hint: "异常计算时的实际时长",
          tone: "trigger",
        },
        {
          label: "停止更新阈值",
          value: formatDecimal(details.stale_hours_threshold, " 小时"),
          hint: "超过此值即触发",
          tone: "threshold",
        },
      ],
    };
  }

  if (item.anomaly_type === "unknown_sale_status") {
    const statuses = details.sale_statuses?.filter(Boolean) ?? [];
    return {
      title: "未知销售状态触发证据",
      conclusion: "以下平台销售状态尚未配置计入或排除规则，需要先确认业务口径。",
      metrics: [
        {
          label: "未配置的销售状态",
          value: statuses.length ? statuses.join("、") : "—",
          hint: `${statuses.length} 种状态`,
          tone: "trigger",
        },
      ],
    };
  }

  return null;
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

            <section
              v-if="selectedEvidence"
              class="risk-evidence-panel"
              aria-labelledby="risk-evidence-title"
            >
              <div class="risk-evidence-heading">
                <div>
                  <p class="section-kicker">TRIGGER EVIDENCE</p>
                  <h3 id="risk-evidence-title">{{ selectedEvidence.title }}</h3>
                </div>
                <span>异常发生时的判定数据</span>
              </div>
              <p class="risk-evidence-conclusion">{{ selectedEvidence.conclusion }}</p>
              <div
                v-if="selectedEvidence.salesSeries?.length"
                class="risk-sales-chart"
                role="img"
                :aria-label="`异常发生日前15天每日销量柱状图，已记录 ${selectedEvidence.salesSeriesCoveredDays ?? 0} 天`"
              >
                <div class="risk-sales-chart-summary">
                  <strong>15天每日销量</strong>
                  <span>
                    已记录 {{ selectedEvidence.salesSeriesCoveredDays ?? 0 }} / 15 天；
                    柱顶为当天销量件数
                  </span>
                </div>
                <div class="risk-sales-bars">
                  <div
                    v-for="point in selectedEvidence.salesSeries"
                    :key="point.date"
                    class="risk-sales-bar-column"
                    :title="`${point.date}：${point.ordered_units == null ? '缺少记录' : `${point.ordered_units} 件`}`"
                  >
                    <span>{{ point.ordered_units ?? "缺" }}</span>
                    <div class="risk-sales-bar-track">
                      <i
                        :class="{
                          zero: point.ordered_units === 0,
                          missing: point.ordered_units === null,
                        }"
                        :style="{ height: salesBarHeight(point.ordered_units) }"
                      ></i>
                    </div>
                    <small>{{ shortDate(point.date) }}</small>
                  </div>
                </div>
              </div>
              <div class="risk-evidence-metrics">
                <article
                  v-for="metric in selectedEvidence.metrics"
                  :key="metric.label"
                  :class="metric.tone || 'context'"
                >
                  <small>{{ metric.label }}</small>
                  <strong>{{ metric.value }}</strong>
                  <span>{{ metric.hint }}</span>
                </article>
              </div>
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
