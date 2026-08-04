<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

import {
  ApiRequestError,
  fetchKeywordTrafficDetail,
  fetchKeywordTrafficProducts,
} from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import type {
  KeywordTrafficDetailPayload,
  KeywordTrafficEvent,
  KeywordTrafficHistoryPoint,
  KeywordTrafficListPayload,
  KeywordTrafficProductSummary,
  KeywordTrafficWindow,
} from "../types";

const props = defineProps<{
  asOf: string;
}>();

const listPayload = ref<KeywordTrafficListPayload | null>(null);
const detail = ref<KeywordTrafficDetailPayload | null>(null);
const selectedOfferId = ref("");
const selectedEventId = ref<number | null>(null);
const search = ref("");
const productFilter = ref<"all" | "changed" | "untracked">("all");
const historyDays = ref(90);
const comparisonDays = ref(7);
const loadingProducts = ref(false);
const loadingDetail = ref(false);
const loadError = ref("");
const failedImageUrls = ref(new Set<string>());
const activePointIndex = ref<number | null>(null);

const products = computed(() => listPayload.value?.items ?? []);
const filteredProducts = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase();
  return products.value.filter((item) => {
    if (productFilter.value === "changed" && item.keyword_change_count === 0) return false;
    if (productFilter.value === "untracked" && item.keyword_event_count > 0) return false;
    if (!needle) return true;
    return [item.title, item.sku, item.offer_id, ...item.current_keywords]
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(needle));
  });
});
const selectedSummary = computed(() =>
  products.value.find((item) => item.offer_id === selectedOfferId.value) ?? null,
);
const selectedEvent = computed(() => {
  const events = detail.value?.events ?? [];
  return events.find((event) => event.id === selectedEventId.value) ?? events.at(-1) ?? null;
});
const currentKeywords = computed(() => detail.value?.product.current_keywords ?? []);

const chartWidth = 1040;
const chartHeight = 390;
const chartLeft = 72;
const chartRight = 28;
const chartTop = 54;
const chartBottom = 62;
const chartInnerWidth = chartWidth - chartLeft - chartRight;
const chartInnerHeight = chartHeight - chartTop - chartBottom;

const chartValues = computed(() =>
  (detail.value?.history ?? [])
    .map((point) => point.page_views_30_days)
    .filter((value): value is number => value !== null),
);
const chartExtent = computed(() => {
  if (!chartValues.value.length) return { min: 0, max: 1 };
  const rawMin = Math.min(...chartValues.value);
  const rawMax = Math.max(...chartValues.value);
  const span = Math.max(1, rawMax - rawMin);
  const padding = Math.max(2, span * 0.12);
  return { min: Math.max(0, rawMin - padding), max: rawMax + padding };
});
const chartPointPositions = computed(() =>
  (detail.value?.history ?? []).map((point, index, history) => ({
    ...point,
    index,
    x: chartX(index, history.length),
    y: point.page_views_30_days === null ? null : chartY(point.page_views_30_days),
  })),
);
const chartSegments = computed(() => {
  const segments: string[] = [];
  let current: string[] = [];
  for (const point of chartPointPositions.value) {
    if (point.y === null) {
      if (current.length) segments.push(current.join(" "));
      current = [];
      continue;
    }
    current.push(`${current.length ? "L" : "M"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`);
  }
  if (current.length) segments.push(current.join(" "));
  return segments;
});
const chartGrid = computed(() =>
  Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    const value = chartExtent.value.max - ratio * (chartExtent.value.max - chartExtent.value.min);
    return {
      value: Math.round(value),
      y: chartTop + ratio * chartInnerHeight,
    };
  }),
);
const chartDateTicks = computed(() => {
  const history = detail.value?.history ?? [];
  if (!history.length) return [];
  const indices = [...new Set([0, Math.floor((history.length - 1) / 2), history.length - 1])];
  return indices.map((index) => ({
    label: formatShortDate(history[index].date),
    x: chartX(index, history.length),
  }));
});
const chartEventMarkers = computed(() => {
  const history = detail.value?.history ?? [];
  const dateIndex = new Map(history.map((point, index) => [point.date, index]));
  return (detail.value?.events ?? [])
    .map((event, index) => {
      const pointIndex = dateIndex.get(event.effective_date);
      if (pointIndex === undefined) return null;
      const point = chartPointPositions.value[pointIndex];
      return {
        event,
        number: index + 1,
        x: point.x,
        y: point.y,
      };
    })
    .filter((value): value is NonNullable<typeof value> => value !== null);
});
const selectedWindowBands = computed(() => {
  const event = selectedEvent.value;
  const history = detail.value?.history ?? [];
  if (!event || !history.length) return [];
  const ranges = [
    { kind: "before", start: event.comparison.before.start_date, end: event.comparison.before.end_date },
    { kind: "after", start: event.comparison.after.start_date, end: event.comparison.after.end_date },
  ] as const;
  return ranges.map((range) => {
    const startIndex = Math.max(0, history.findIndex((point) => point.date >= range.start));
    let endIndex = history.findLastIndex((point) => point.date <= range.end);
    if (endIndex < 0) endIndex = history.length - 1;
    const startX = chartX(startIndex, history.length);
    const endX = chartX(Math.max(startIndex, endIndex), history.length);
    return {
      kind: range.kind,
      x: Math.max(chartLeft, startX - 4),
      width: Math.max(8, Math.min(chartLeft + chartInnerWidth, endX + 4) - Math.max(chartLeft, startX - 4)),
    };
  });
});
const activePoint = computed(() => {
  if (activePointIndex.value === null) return null;
  return chartPointPositions.value[activePointIndex.value] ?? null;
});

onMounted(() => void loadProducts());
watch(() => props.asOf, () => void loadProducts(selectedOfferId.value));
watch([historyDays, comparisonDays], () => {
  if (selectedOfferId.value) void loadDetail(selectedOfferId.value);
});

async function loadProducts(preferredOfferId = "") {
  loadingProducts.value = true;
  loadError.value = "";
  try {
    const payload = await fetchKeywordTrafficProducts(props.asOf);
    listPayload.value = payload;
    const preferred = payload.items.find((item) => item.offer_id === preferredOfferId);
    const next = preferred
      ?? payload.items.find((item) => item.keyword_change_count > 0)
      ?? payload.items.find((item) => item.keyword_event_count > 0)
      ?? payload.items[0];
    if (next) await selectProduct(next.offer_id);
    else {
      selectedOfferId.value = "";
      detail.value = null;
    }
  } catch (error) {
    loadError.value = errorMessage(error, "关键词流量商品列表加载失败");
  } finally {
    loadingProducts.value = false;
  }
}

async function selectProduct(offerId: string) {
  selectedOfferId.value = offerId;
  activePointIndex.value = null;
  await loadDetail(offerId);
}

async function loadDetail(offerId: string) {
  loadingDetail.value = true;
  loadError.value = "";
  try {
    const payload = await fetchKeywordTrafficDetail(
      offerId,
      props.asOf,
      historyDays.value,
      comparisonDays.value,
    );
    if (selectedOfferId.value !== offerId) return;
    detail.value = payload;
    const existing = payload.events.find((event) => event.id === selectedEventId.value);
    selectedEventId.value = (
      existing
      ?? [...payload.events].reverse().find((event) => event.event_kind === "change")
      ?? payload.events.at(-1)
    )?.id ?? null;
  } catch (error) {
    loadError.value = errorMessage(error, "商品关键词流量详情加载失败");
  } finally {
    loadingDetail.value = false;
  }
}

function chartX(index: number, count: number) {
  if (count <= 1) return chartLeft + chartInnerWidth / 2;
  return chartLeft + (index / (count - 1)) * chartInnerWidth;
}

function chartY(value: number) {
  const extent = chartExtent.value;
  const ratio = (value - extent.min) / Math.max(1, extent.max - extent.min);
  return chartTop + (1 - ratio) * chartInnerHeight;
}

function setActivePoint(index: number) {
  activePointIndex.value = index;
}

function setSelectedEvent(event: KeywordTrafficEvent) {
  selectedEventId.value = event.id;
  const pointIndex = detail.value?.history.findIndex((point) => point.date === event.effective_date) ?? -1;
  if (pointIndex >= 0) activePointIndex.value = pointIndex;
}

function productImageUrl(item: KeywordTrafficProductSummary | KeywordTrafficDetailPayload["product"] | null) {
  const source = item?.image_url?.trim() ?? "";
  if (!source || failedImageUrls.value.has(source)) return "";
  return productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list);
}

function markImageUnavailable(source: string | null | undefined) {
  const normalized = source?.trim() ?? "";
  if (!normalized) return;
  const next = new Set(failedImageUrls.value);
  next.add(normalized);
  failedImageUrls.value = next;
}

function formatNumber(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
}

function formatSigned(value: number | null | undefined, suffix = "") {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatNumber(value)}${suffix}`;
}

function formatShortDate(value: string) {
  const [, month = "", day = ""] = value.split("-");
  return `${month}/${day}`;
}

function directionLabel(direction: KeywordTrafficEvent["comparison"]["traffic_direction"]) {
  return {
    up: "上升",
    down: "下降",
    flat: "基本持平",
    unavailable: "数据不足",
  }[direction];
}

function trendDirectionLabel(direction: KeywordTrafficWindow["trend_direction"]) {
  return {
    up: "上升趋势",
    down: "下降趋势",
    flat: "平稳趋势",
    unavailable: "趋势不足",
  }[direction];
}

function trendChangeLabel(event: KeywordTrafficEvent | null) {
  if (!event) return "尚未选择变更节点";
  return {
    reversal_up: "趋势由弱转强",
    reversal_down: "趋势由强转弱",
    improving: "上升速度增强",
    weakening: "上升速度减弱",
    stable: "趋势变化不明显",
    insufficient: "趋势数据不足",
  }[event.comparison.trend_change];
}

function comparisonStatusLabel(event: KeywordTrafficEvent | null) {
  if (!event) return "等待每日完整 Offer 快照自动建立标题关键词档案。";
  const comparison = event.comparison;
  if (comparison.status === "waiting") return "变更后尚无可观察日期。";
  if (comparison.status === "collecting") {
    return `变更后已观察 ${comparison.observed_after_days}/${comparison.comparison_days} 天，结论仍在积累。`;
  }
  if (comparison.status === "data_missing") return "对比窗口已结束，但存在缺失流量，结论保持缺失。";
  return `变更前后各 ${comparison.comparison_days} 天的观察窗口已完整结束。`;
}

function changeSummary(event: KeywordTrafficEvent) {
  return event.change_label;
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof ApiRequestError ? error.message : fallback;
}

</script>

<template>
  <section class="keyword-traffic-page">
    <header class="keyword-hero">
      <div>
        <p class="eyebrow">KEYWORD × TRAFFIC MONITOR</p>
        <h2>标题关键词自动建档，流量结果一眼看清</h2>
        <span>
          每次完整 Offer 采集都会自动归档官方商品标题；发现标题词或词序变化时，系统自动打标签、固定高对比节点，并比较前后的流量水平和趋势速度。
        </span>
      </div>
      <div class="metric-boundary">
        <strong>口径边界</strong>
        <p>这是近30天浏览量滚动窗口，不是精确当天流量，也不是独立访客数。</p>
      </div>
    </header>

    <p v-if="loadError" class="page-message error" role="alert">{{ loadError }}</p>
    <div v-if="listPayload" class="overview-metrics">
      <article>
        <span>店铺商品</span>
        <strong>{{ formatNumber(listPayload.summary.product_count) }}</strong>
        <small>全部现有 Offer</small>
      </article>
      <article>
        <span>今日有流量值</span>
        <strong>{{ formatNumber(listPayload.summary.with_traffic_count) }}</strong>
        <small>缺失不补零</small>
      </article>
      <article>
        <span>已自动建档</span>
        <strong>{{ formatNumber(listPayload.summary.archived_product_count) }}</strong>
        <small>来自每日标题快照</small>
      </article>
      <article class="accent">
        <span>关键词变更节点</span>
        <strong>{{ formatNumber(listPayload.summary.keyword_change_count) }}</strong>
        <small>不含首次基线</small>
      </article>
    </div>

    <div class="monitor-layout">
      <aside class="product-browser">
        <div class="browser-heading">
          <div>
            <p>商品监测清单</p>
            <strong>{{ filteredProducts.length }} / {{ products.length }}</strong>
          </div>
          <span v-if="loadingProducts" class="loading-dot">读取中</span>
        </div>
        <label class="product-search">
          <span>搜索商品 / SKU / 关键词</span>
          <input v-model="search" type="search" placeholder="输入部分内容即可" />
        </label>
        <div class="filter-tabs" aria-label="商品筛选">
          <button :class="{ active: productFilter === 'all' }" @click="productFilter = 'all'">全部</button>
          <button :class="{ active: productFilter === 'changed' }" @click="productFilter = 'changed'">有变更</button>
          <button :class="{ active: productFilter === 'untracked' }" @click="productFilter = 'untracked'">待首份快照</button>
        </div>
        <div class="product-list">
          <button
            v-for="item in filteredProducts"
            :key="item.offer_id"
            type="button"
            class="product-row"
            :class="{ active: selectedOfferId === item.offer_id }"
            @click="selectProduct(item.offer_id)"
          >
            <span class="product-thumb">
              <img
                v-if="productImageUrl(item)"
                :src="productImageUrl(item)"
                width="56"
                height="56"
                loading="lazy"
                decoding="async"
                alt=""
                @error="markImageUnavailable(item.image_url)"
              />
              <i v-else>暂无图片</i>
            </span>
            <span class="product-copy">
              <strong>{{ item.title || "未命名商品" }}</strong>
              <small>{{ item.sku || item.offer_id }}</small>
              <em v-if="item.keyword_change_count" class="changed-badge">
                {{ item.keyword_change_count }} 次变更
              </em>
              <em v-else-if="item.keyword_event_count" class="baseline-badge">自动基线</em>
              <em v-else class="untracked-badge">待首份快照</em>
            </span>
            <span class="product-traffic">
              <small>近30天浏览量</small>
              <strong>{{ formatNumber(item.latest_page_views_30_days) }}</strong>
            </span>
          </button>
          <div v-if="!filteredProducts.length && !loadingProducts" class="empty-list">
            没有符合当前条件的商品。
          </div>
        </div>
      </aside>

      <main class="monitor-workspace">
        <div v-if="loadingDetail" class="workspace-loading">正在整理关键词节点与流量历史…</div>
        <div v-else-if="!detail" class="workspace-empty">
          <strong>选择一个商品开始查看</strong>
          <span>这里会显示每日近30天浏览量、关键词节点和前后变化结论。</span>
        </div>
        <template v-else>
          <header class="product-focus">
            <span class="focus-thumb">
              <img
                v-if="productImageUrl(detail.product)"
                :src="productImageUrl(detail.product)"
                width="80"
                height="80"
                decoding="async"
                alt=""
                @error="markImageUnavailable(detail.product.image_url)"
              />
              <i v-else>暂无图片</i>
            </span>
            <div class="focus-copy">
              <p>当前监测商品</p>
              <h3>{{ detail.product.title || "未命名商品" }}</h3>
              <span>SKU {{ detail.product.sku || "缺失" }} · Offer {{ detail.product.offer_id }}</span>
            </div>
            <div class="focus-actions">
              <label>
                <span>趋势范围</span>
                <select v-model.number="historyDays">
                  <option :value="60">最近60天</option>
                  <option :value="90">最近90天</option>
                  <option :value="180">最近180天</option>
                  <option :value="365">最近365天</option>
                </select>
              </label>
              <label>
                <span>前后窗口</span>
                <select v-model.number="comparisonDays">
                  <option :value="3">前后3天</option>
                  <option :value="7">前后7天</option>
                  <option :value="14">前后14天</option>
                  <option :value="30">前后30天</option>
                </select>
              </label>
            </div>
          </header>

          <section class="current-keywords">
            <div>
              <p>当前官方标题关键词</p>
              <span v-if="currentKeywords.length">共 {{ currentKeywords.length }} 个</span>
              <span v-else>等待每日 Offer 快照</span>
            </div>
            <div class="keyword-chips">
              <span v-for="keyword in currentKeywords" :key="keyword">{{ keyword }}</span>
              <em v-if="!currentKeywords.length">无需人工操作；下次完整采集会自动建立首份标题关键词档案。</em>
            </div>
          </section>

          <section v-if="selectedEvent" class="impact-section">
            <header>
              <div>
                <p>{{ selectedEvent.event_kind === "change" ? "已选自动变化节点" : "已选自动基线节点" }}</p>
                <h3>{{ selectedEvent.effective_date }} · {{ changeSummary(selectedEvent) }}</h3>
              </div>
              <span class="observation-status" :class="selectedEvent.comparison.status">
                {{ comparisonStatusLabel(selectedEvent) }}
              </span>
            </header>
            <div class="impact-grid">
              <article class="impact-card traffic" :class="selectedEvent.comparison.traffic_direction">
                <div class="impact-label">
                  <span>01</span>
                  <p>30天浏览量上升 / 下降</p>
                </div>
                <strong>{{ directionLabel(selectedEvent.comparison.traffic_direction) }}</strong>
                <div class="value-flow">
                  <span>
                    <small>变更前最后有效值</small>
                    <b>{{ formatNumber(selectedEvent.comparison.before.last_value) }}</b>
                  </span>
                  <i>→</i>
                  <span>
                    <small>变更后最新有效值</small>
                    <b>{{ formatNumber(selectedEvent.comparison.after.last_value) }}</b>
                  </span>
                </div>
                <p class="impact-result">
                  净变化 {{ formatSigned(selectedEvent.comparison.traffic_delta) }}
                  <em v-if="selectedEvent.comparison.traffic_delta_percent !== null">
                    （{{ formatSigned(selectedEvent.comparison.traffic_delta_percent, '%') }}）
                  </em>
                </p>
              </article>

              <article class="impact-card trend" :class="selectedEvent.comparison.trend_change">
                <div class="impact-label">
                  <span>02</span>
                  <p>上升 / 下降趋势变化</p>
                </div>
                <strong>{{ trendChangeLabel(selectedEvent) }}</strong>
                <div class="value-flow">
                  <span>
                    <small>变更前趋势</small>
                    <b>{{ trendDirectionLabel(selectedEvent.comparison.before.trend_direction) }}</b>
                    <em>{{ formatSigned(selectedEvent.comparison.before.slope_per_day, "/天") }}</em>
                  </span>
                  <i>→</i>
                  <span>
                    <small>变更后趋势</small>
                    <b>{{ trendDirectionLabel(selectedEvent.comparison.after.trend_direction) }}</b>
                    <em>{{ formatSigned(selectedEvent.comparison.after.slope_per_day, "/天") }}</em>
                  </span>
                </div>
                <p class="impact-result">
                  趋势速度变化 {{ formatSigned(selectedEvent.comparison.slope_change, "/天") }}
                </p>
              </article>
            </div>
          </section>

          <section class="traffic-chart-card">
            <header>
              <div>
                <p>每日近30天浏览量</p>
                <h3>关键词节点与滚动流量同轴观察</h3>
              </div>
              <div class="chart-legend">
                <span><i class="line"></i>近30天浏览量</span>
                <span><i class="before"></i>变更前窗口</span>
                <span><i class="after"></i>变更后窗口</span>
                <span><i class="marker"></i>自动检测变化</span>
              </div>
            </header>
            <div class="chart-wrap">
              <svg
                :viewBox="`0 0 ${chartWidth} ${chartHeight}`"
                role="img"
                aria-label="近30天浏览量与标题关键词自动变化节点趋势图"
              >
                <rect class="chart-background" :x="chartLeft" :y="chartTop" :width="chartInnerWidth" :height="chartInnerHeight" rx="14" />
                <rect
                  v-for="band in selectedWindowBands"
                  :key="band.kind"
                  class="window-band"
                  :class="band.kind"
                  :x="band.x"
                  :y="chartTop"
                  :width="band.width"
                  :height="chartInnerHeight"
                />
                <g v-for="grid in chartGrid" :key="grid.y">
                  <line class="grid-line" :x1="chartLeft" :x2="chartLeft + chartInnerWidth" :y1="grid.y" :y2="grid.y" />
                  <text class="axis-label y" :x="chartLeft - 12" :y="grid.y + 5">{{ formatNumber(grid.value) }}</text>
                </g>
                <text class="axis-title" :x="18" :y="chartTop + chartInnerHeight / 2" transform="rotate(-90 18 190)">近30天浏览量</text>
                <g v-for="tick in chartDateTicks" :key="tick.label">
                  <text class="axis-label x" :x="tick.x" :y="chartTop + chartInnerHeight + 34">{{ tick.label }}</text>
                </g>
                <path v-for="(segment, index) in chartSegments" :key="index" class="traffic-line halo" :d="segment" />
                <path v-for="(segment, index) in chartSegments" :key="`line-${index}`" class="traffic-line" :d="segment" />
                <g
                  v-for="marker in chartEventMarkers"
                  :key="marker.event.id"
                  class="event-marker"
                  :class="[
                    marker.event.event_kind,
                    { selected: marker.event.id === selectedEventId },
                  ]"
                  tabindex="0"
                  role="button"
                  :aria-label="`${marker.event.effective_date} ${changeSummary(marker.event)}`"
                  @click="setSelectedEvent(marker.event)"
                  @keydown.enter.prevent="setSelectedEvent(marker.event)"
                  @keydown.space.prevent="setSelectedEvent(marker.event)"
                >
                  <line :x1="marker.x" :x2="marker.x" :y1="chartTop - 8" :y2="chartTop + chartInnerHeight" />
                  <rect :x="marker.x - 34" :y="14" width="68" height="26" rx="13" />
                  <text :x="marker.x" y="32">
                    {{ marker.event.event_kind === "change" ? `自动 ${marker.number}` : "自动基线" }}
                  </text>
                  <circle v-if="marker.y !== null" :cx="marker.x" :cy="marker.y" r="8" />
                </g>
                <g
                  v-for="point in chartPointPositions"
                  :key="point.date"
                  class="data-point"
                  :class="{ active: point.index === activePointIndex }"
                  tabindex="0"
                  role="button"
                  :aria-label="`${point.date}，近30天浏览量 ${formatNumber(point.page_views_30_days)}`"
                  @mouseenter="setActivePoint(point.index)"
                  @focus="setActivePoint(point.index)"
                  @click="setActivePoint(point.index)"
                >
                  <circle v-if="point.y !== null" class="point-hit" :cx="point.x" :cy="point.y" r="10" />
                  <circle v-if="point.y !== null" class="point-dot" :cx="point.x" :cy="point.y" r="3.2" />
                </g>
              </svg>
              <div v-if="activePoint" class="point-readout">
                <span>{{ activePoint.date }}</span>
                <strong>近30天浏览量 {{ formatNumber(activePoint.page_views_30_days) }}</strong>
                <small v-if="activePoint.page_views_30_days === null">平台该日流量字段缺失，未补零。</small>
              </div>
            </div>
            <p class="metric-notice">{{ detail.metric_notice }}</p>
          </section>

          <section class="event-timeline">
            <header>
              <div>
                <p>标题关键词自动变化时间线</p>
                <h3>系统自动打标签；点击任一节点切换前后对比</h3>
              </div>
              <span>{{ detail.events.length }} 个记录 · {{ Math.max(0, detail.events.length - 1) }} 次变更</span>
            </header>
            <div v-if="detail.events.length" class="event-list">
              <button
                v-for="(event, index) in [...detail.events].reverse()"
                :key="event.id"
                type="button"
                class="event-card"
                :class="[
                  event.event_kind,
                  { active: event.id === selectedEventId },
                ]"
                @click="setSelectedEvent(event)"
              >
                <span class="event-index">{{ detail.events.length - index }}</span>
                <span class="event-body">
                  <small>{{ event.effective_date }} · 系统自动检测</small>
                  <strong>{{ changeSummary(event) }}</strong>
                  <span class="event-diffs">
                    <em v-for="keyword in event.added_keywords" :key="`add-${keyword}`" class="added">+ {{ keyword }}</em>
                    <em v-for="keyword in event.removed_keywords" :key="`remove-${keyword}`" class="removed">− {{ keyword }}</em>
                    <em v-if="event.event_kind === 'baseline'" class="baseline">{{ event.keywords.join(" · ") }}</em>
                  </span>
                  <p class="source-title">标题：{{ event.source_title }}</p>
                </span>
                <span class="event-outcome" :class="event.comparison.traffic_direction">
                  <small>流量结果</small>
                  <strong>{{ directionLabel(event.comparison.traffic_direction) }}</strong>
                  <em>{{ formatSigned(event.comparison.traffic_delta) }}</em>
                </span>
              </button>
            </div>
            <div v-else class="timeline-empty">
              <strong>等待首份完整 Offer 快照</strong>
              <span>无需人工建档；采集成功后系统会自动提取标题词建立基线，以后发现变化自动打标签。</span>
            </div>
          </section>
        </template>
      </main>
    </div>

  </section>
</template>

<style scoped>
.keyword-traffic-page {
  --ink: #162138;
  --muted: #6c7890;
  --line: #dce2eb;
  --paper: #ffffff;
  --canvas: #f2f5f8;
  --red: #d1493f;
  --red-soft: #fff0ed;
  --green: #177d5d;
  --green-soft: #e7f6ef;
  --amber: #b76a16;
  --blue: #3166b4;
  color: var(--ink);
  display: grid;
  gap: 18px;
}

.keyword-hero {
  align-items: stretch;
  background: linear-gradient(120deg, #172238 0%, #243a5d 62%, #2c4e75 100%);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 22px;
  box-shadow: 0 16px 40px rgba(25, 40, 66, 0.16);
  color: #fff;
  display: grid;
  gap: 24px;
  grid-template-columns: minmax(0, 1fr) minmax(260px, 360px);
  overflow: hidden;
  padding: 28px 30px;
  position: relative;
}

.keyword-hero::after {
  background: radial-gradient(circle, rgba(255, 153, 117, 0.42), transparent 65%);
  content: "";
  height: 240px;
  position: absolute;
  right: -70px;
  top: -100px;
  width: 240px;
}

.keyword-hero > * { position: relative; z-index: 1; }
.eyebrow { color: #ffb092; font-size: 0.72rem; font-weight: 800; letter-spacing: 0.16em; margin: 0 0 8px; }
.keyword-hero h2 { font-size: clamp(1.55rem, 2.4vw, 2.35rem); letter-spacing: -0.04em; margin: 0 0 10px; }
.keyword-hero > div > span { color: rgba(255, 255, 255, 0.76); display: block; line-height: 1.75; max-width: 760px; }
.metric-boundary { align-self: center; background: rgba(255, 255, 255, 0.09); border: 1px solid rgba(255, 255, 255, 0.14); border-radius: 16px; padding: 17px 18px; }
.metric-boundary strong { color: #ffd5c5; display: block; font-size: 0.78rem; letter-spacing: 0.08em; margin-bottom: 6px; }
.metric-boundary p { color: rgba(255, 255, 255, 0.82); line-height: 1.65; margin: 0; }

.page-message { background: #edf7f2; border: 1px solid #b9e2cf; border-radius: 12px; color: #176c51; margin: 0; padding: 11px 15px; }
.page-message.error { background: #fff0ee; border-color: #f0b9b3; color: #a43730; }

.overview-metrics { display: grid; gap: 12px; grid-template-columns: repeat(4, minmax(0, 1fr)); }
.overview-metrics article { background: var(--paper); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 8px 24px rgba(28, 43, 68, 0.06); display: grid; gap: 2px; padding: 17px 18px; }
.overview-metrics article.accent { background: linear-gradient(145deg, #fff5f0, #fff); border-color: #f0b39b; }
.overview-metrics span { color: var(--muted); font-size: 0.78rem; }
.overview-metrics strong { font-size: 1.7rem; letter-spacing: -0.04em; }
.overview-metrics small { color: #929bad; font-size: 0.7rem; }

.monitor-layout { align-items: start; display: grid; gap: 16px; grid-template-columns: 330px minmax(0, 1fr); }
.product-browser, .monitor-workspace { background: var(--paper); border: 1px solid var(--line); border-radius: 20px; box-shadow: 0 12px 32px rgba(28, 43, 68, 0.07); }
.product-browser { max-height: calc(100vh - 128px); overflow: hidden; position: sticky; top: 18px; }
.browser-heading { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 18px; }
.browser-heading p { color: var(--muted); font-size: 0.75rem; margin: 0 0 3px; }
.browser-heading strong { font-size: 1.05rem; }
.loading-dot { color: var(--blue); font-size: 0.75rem; }
.product-search { display: grid; gap: 6px; padding: 15px 16px 10px; }
.product-search span { color: var(--muted); font-size: 0.72rem; }
.product-search input, .focus-actions select { background: #f8fafc; border: 1px solid #cfd7e3; border-radius: 10px; color: var(--ink); font: inherit; outline: none; padding: 10px 11px; }
.product-search input:focus, .focus-actions select:focus { border-color: #557db9; box-shadow: 0 0 0 3px rgba(75, 112, 169, 0.13); }
.filter-tabs { display: grid; gap: 6px; grid-template-columns: repeat(3, 1fr); padding: 0 16px 12px; }
.filter-tabs button { background: #f2f5f8; border: 0; border-radius: 9px; color: var(--muted); cursor: pointer; font: inherit; font-size: 0.74rem; padding: 8px 4px; }
.filter-tabs button.active { background: #203652; color: #fff; font-weight: 700; }
.product-list { max-height: calc(100vh - 330px); overflow-y: auto; padding: 0 9px 12px; }
.product-row { align-items: center; background: transparent; border: 1px solid transparent; border-radius: 13px; color: inherit; cursor: pointer; display: grid; gap: 9px; grid-template-columns: 56px minmax(0, 1fr) auto; margin-bottom: 5px; padding: 9px; text-align: left; width: 100%; }
.product-row:hover { background: #f5f7fa; }
.product-row.active { background: #eef3f9; border-color: #9fb4cf; box-shadow: inset 3px 0 #315e95; }
.product-thumb, .focus-thumb { align-items: center; background: #f0f2f5; border: 1px solid #e0e4ea; border-radius: 11px; display: flex; justify-content: center; overflow: hidden; }
.product-thumb { height: 56px; width: 56px; }
.product-thumb img, .focus-thumb img { height: 100%; object-fit: contain; width: 100%; }
.product-thumb i, .focus-thumb i { color: #98a1b0; font-size: 0.62rem; font-style: normal; text-align: center; }
.product-copy { display: grid; gap: 3px; min-width: 0; }
.product-copy strong { display: -webkit-box; font-size: 0.78rem; line-height: 1.35; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.product-copy small { color: var(--muted); font-size: 0.67rem; overflow-wrap: anywhere; }
.product-copy em { border-radius: 999px; font-size: 0.62rem; font-style: normal; justify-self: start; padding: 3px 7px; }
.changed-badge { background: var(--red-soft); color: #b03c34; }
.baseline-badge { background: #edf3fb; color: #386298; }
.untracked-badge { background: #f0f1f3; color: #7c8490; }
.product-traffic { text-align: right; }
.product-traffic small { color: var(--muted); display: block; font-size: 0.58rem; white-space: nowrap; }
.product-traffic strong { font-size: 0.9rem; }
.empty-list, .workspace-empty, .workspace-loading { color: var(--muted); padding: 34px 20px; text-align: center; }

.monitor-workspace { min-width: 0; padding: 20px; }
.product-focus { align-items: center; display: grid; gap: 15px; grid-template-columns: 80px minmax(0, 1fr) auto; }
.focus-thumb { height: 80px; width: 80px; }
.focus-copy p, .current-keywords p, .impact-section header p, .traffic-chart-card header p, .event-timeline header p { color: var(--muted); font-size: 0.7rem; letter-spacing: 0.08em; margin: 0 0 4px; text-transform: uppercase; }
.focus-copy h3 { font-size: 1.2rem; line-height: 1.35; margin: 0 0 6px; }
.focus-copy span { color: var(--muted); font-size: 0.75rem; overflow-wrap: anywhere; }
.focus-actions { align-items: end; display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.focus-actions label { display: grid; gap: 4px; }
.focus-actions label span { color: var(--muted); font-size: 0.65rem; }
.focus-actions select { font-size: 0.75rem; padding: 8px 9px; }

.current-keywords { align-items: start; background: #f7f9fb; border: 1px solid #e1e5eb; border-radius: 14px; display: grid; gap: 16px; grid-template-columns: 145px minmax(0, 1fr); margin-top: 18px; padding: 14px 16px; }
.current-keywords > div:first-child span { color: var(--muted); font-size: 0.72rem; }
.keyword-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.keyword-chips span { background: #203652; border-radius: 999px; color: #fff; font-size: 0.72rem; padding: 6px 10px; }
.keyword-chips em { color: var(--muted); font-size: 0.78rem; font-style: normal; line-height: 1.6; }

.impact-section { background: #f7f9fb; border: 1px solid #dbe2eb; border-radius: 18px; margin-top: 18px; padding: 17px; }
.impact-section > header { align-items: center; display: flex; gap: 18px; justify-content: space-between; }
.impact-section h3, .traffic-chart-card h3, .event-timeline h3 { font-size: 1rem; margin: 0; }
.observation-status { background: #e9f3ee; border-radius: 999px; color: #247257; font-size: 0.7rem; max-width: 420px; padding: 7px 11px; text-align: right; }
.observation-status.collecting, .observation-status.waiting { background: #fff2dc; color: #99601d; }
.observation-status.data_missing { background: #f0f1f3; color: #6f7681; }
.impact-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 14px; }
.impact-card { background: #fff; border: 1px solid #dce2ea; border-radius: 15px; min-width: 0; padding: 16px; position: relative; }
.impact-card::before { border-radius: 15px 0 0 15px; bottom: 0; content: ""; left: 0; position: absolute; top: 0; width: 5px; }
.impact-card.up::before, .impact-card.reversal_up::before, .impact-card.improving::before { background: var(--green); }
.impact-card.down::before, .impact-card.reversal_down::before, .impact-card.weakening::before { background: var(--red); }
.impact-card.flat::before, .impact-card.stable::before { background: var(--blue); }
.impact-card.unavailable::before, .impact-card.insufficient::before { background: #9aa2ae; }
.impact-label { align-items: center; display: flex; gap: 8px; }
.impact-label span { align-items: center; background: #243852; border-radius: 7px; color: #fff; display: flex; font-size: 0.62rem; height: 24px; justify-content: center; width: 28px; }
.impact-label p { color: var(--muted); font-size: 0.73rem; margin: 0; }
.impact-card > strong { display: block; font-size: clamp(1.25rem, 2vw, 1.8rem); letter-spacing: -0.04em; margin: 12px 0; }
.impact-card.up > strong, .impact-card.reversal_up > strong, .impact-card.improving > strong { color: var(--green); }
.impact-card.down > strong, .impact-card.reversal_down > strong, .impact-card.weakening > strong { color: var(--red); }
.value-flow { align-items: stretch; display: grid; gap: 8px; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); }
.value-flow > span { background: #f5f7fa; border-radius: 10px; display: grid; gap: 3px; padding: 10px; }
.value-flow small { color: var(--muted); font-size: 0.65rem; }
.value-flow b { font-size: 1rem; }
.value-flow em { color: var(--muted); font-size: 0.66rem; font-style: normal; }
.value-flow > i { align-self: center; color: #8994a4; font-style: normal; }
.impact-result { border-top: 1px dashed #d9dfe7; font-size: 0.75rem; margin: 12px 0 0; padding-top: 10px; }
.impact-result em { font-style: normal; }

.traffic-chart-card, .event-timeline { border: 1px solid #dbe1e9; border-radius: 18px; margin-top: 18px; overflow: hidden; }
.traffic-chart-card > header, .event-timeline > header { align-items: center; display: flex; gap: 16px; justify-content: space-between; padding: 16px 18px 8px; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }
.chart-legend span { align-items: center; color: var(--muted); display: flex; font-size: 0.65rem; gap: 5px; }
.chart-legend i { display: inline-block; height: 8px; width: 18px; }
.chart-legend .line { border-top: 3px solid #e05245; height: 0; }
.chart-legend .before { background: rgba(61, 105, 165, 0.14); }
.chart-legend .after { background: rgba(31, 139, 99, 0.14); }
.chart-legend .marker { border-left: 3px solid #c8443b; width: 2px; }
.chart-wrap { overflow-x: auto; padding: 4px 10px 0; position: relative; }
.chart-wrap svg { display: block; min-width: 760px; width: 100%; }
.chart-background { fill: #fbfcfd; stroke: #e4e8ee; }
.window-band.before { fill: rgba(58, 101, 161, 0.1); }
.window-band.after { fill: rgba(28, 132, 94, 0.1); }
.grid-line { stroke: #e4e8ee; stroke-dasharray: 3 6; }
.axis-label { fill: #748096; font-size: 12px; }
.axis-label.y { text-anchor: end; }
.axis-label.x { text-anchor: middle; }
.axis-title { fill: #66738a; font-size: 12px; font-weight: 700; text-anchor: middle; }
.traffic-line { fill: none; stroke: #db4c42; stroke-linecap: round; stroke-linejoin: round; stroke-width: 3.5; }
.traffic-line.halo { stroke: rgba(219, 76, 66, 0.12); stroke-width: 11; }
.event-marker { cursor: pointer; outline: none; }
.event-marker line { stroke: #c8443b; stroke-dasharray: 5 4; stroke-width: 2; }
.event-marker rect { fill: #c8443b; }
.event-marker text { fill: #fff; font-size: 11px; font-weight: 800; text-anchor: middle; }
.event-marker circle { fill: #fff; stroke: #c8443b; stroke-width: 4; }
.event-marker.baseline line { stroke: #496a97; }
.event-marker.baseline rect { fill: #496a97; }
.event-marker.baseline circle { stroke: #496a97; }
.event-marker.selected line { stroke-width: 3.5; }
.event-marker.selected rect { filter: drop-shadow(0 4px 7px rgba(93, 36, 32, 0.28)); }
.event-marker:focus rect { stroke: #18243a; stroke-width: 2; }
.point-hit { fill: transparent; }
.point-dot { fill: #fff; pointer-events: none; stroke: #d84b41; stroke-width: 2; }
.data-point { cursor: crosshair; outline: none; }
.data-point.active .point-dot, .data-point:focus .point-dot { fill: #d84b41; r: 5; }
.point-readout { align-items: center; background: #1c2a40; border-radius: 10px; bottom: 18px; color: #fff; display: flex; flex-wrap: wrap; gap: 10px; left: 92px; padding: 8px 12px; pointer-events: none; position: absolute; }
.point-readout span, .point-readout small { color: rgba(255, 255, 255, 0.7); font-size: 0.68rem; }
.point-readout strong { font-size: 0.75rem; }
.metric-notice { background: #f5f7fa; border-top: 1px solid #e2e6ec; color: var(--muted); font-size: 0.7rem; line-height: 1.65; margin: 0; padding: 10px 16px; }

.event-timeline > header > span { color: var(--muted); font-size: 0.72rem; }
.event-list { display: grid; gap: 8px; padding: 10px 12px 14px; }
.event-card { align-items: center; background: #fafbfc; border: 1px solid #e1e5eb; border-radius: 13px; color: inherit; cursor: pointer; display: grid; gap: 11px; grid-template-columns: 34px minmax(0, 1fr) 105px; padding: 12px; text-align: left; width: 100%; }
.event-card:hover, .event-card.active { background: #fff5f2; border-color: #e6a79e; }
.event-card.baseline:hover, .event-card.baseline.active { background: #f2f6fb; border-color: #aebfd4; }
.event-index { align-items: center; background: #cf4d43; border-radius: 50%; color: #fff; display: flex; font-size: 0.7rem; font-weight: 800; height: 30px; justify-content: center; width: 30px; }
.event-card.baseline .event-index { background: #506f99; }
.event-body { display: grid; gap: 4px; min-width: 0; }
.event-body > small { color: var(--muted); font-size: 0.65rem; }
.event-body > strong { font-size: 0.82rem; }
.event-body > p { color: var(--muted); font-size: 0.7rem; margin: 2px 0 0; }
.event-diffs { display: flex; flex-wrap: wrap; gap: 5px; }
.event-diffs em { border-radius: 999px; font-size: 0.64rem; font-style: normal; padding: 4px 7px; }
.event-diffs .added { background: var(--green-soft); color: #176c51; }
.event-diffs .removed { background: var(--red-soft); color: #a53c34; text-decoration: line-through; }
.event-diffs .baseline { background: #edf2f8; color: #476584; }
.event-outcome { display: grid; gap: 2px; justify-items: end; }
.event-outcome small { color: var(--muted); font-size: 0.62rem; }
.event-outcome strong { font-size: 0.82rem; }
.event-outcome em { font-size: 0.7rem; font-style: normal; }
.event-outcome.up strong, .event-outcome.up em { color: var(--green); }
.event-outcome.down strong, .event-outcome.down em { color: var(--red); }
.timeline-empty { align-items: center; color: var(--muted); display: flex; flex-direction: column; gap: 8px; padding: 34px 18px; text-align: center; }
.timeline-empty strong { color: var(--ink); }
.timeline-empty span { font-size: 0.78rem; line-height: 1.6; max-width: 560px; }


@media (max-width: 1180px) {
  .monitor-layout { grid-template-columns: 280px minmax(0, 1fr); }
  .product-row { grid-template-columns: 48px minmax(0, 1fr); }
  .product-thumb { height: 48px; width: 48px; }
  .product-traffic { grid-column: 2; text-align: left; }
  .product-traffic small, .product-traffic strong { display: inline; }
  .impact-grid { grid-template-columns: 1fr; }
  .product-focus { grid-template-columns: 72px minmax(0, 1fr); }
  .focus-thumb { height: 72px; width: 72px; }
  .focus-actions { grid-column: 1 / -1; justify-content: flex-start; }
}

@media (max-width: 860px) {
  .keyword-hero { grid-template-columns: 1fr; }
  .overview-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .monitor-layout { grid-template-columns: 1fr; }
  .product-browser { max-height: none; position: static; }
  .product-list { max-height: 430px; }
  .current-keywords { grid-template-columns: 1fr; }
  .traffic-chart-card > header, .event-timeline > header, .impact-section > header { align-items: flex-start; flex-direction: column; }
  .observation-status { max-width: none; text-align: left; }
}

@media (max-width: 560px) {
  .keyword-hero { border-radius: 16px; padding: 22px 18px; }
  .overview-metrics { grid-template-columns: 1fr 1fr; }
  .monitor-workspace { padding: 13px; }
  .product-focus { grid-template-columns: 58px minmax(0, 1fr); }
  .focus-thumb { height: 58px; width: 58px; }
  .focus-actions { align-items: stretch; display: grid; grid-template-columns: 1fr 1fr; }
  .value-flow { grid-template-columns: 1fr; }
  .value-flow > i { display: none; }
  .event-card { align-items: start; grid-template-columns: 30px minmax(0, 1fr); }
  .event-outcome { grid-column: 2; justify-items: start; }
  .point-readout { bottom: 12px; left: 30px; right: 30px; }
}
</style>
