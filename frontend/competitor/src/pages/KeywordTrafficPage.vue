<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

import {
  ApiRequestError,
  fetchKeywordTrafficDetail,
  fetchKeywordTrafficProducts,
} from "../api";
import {
  floatingChartTooltipClasses,
  floatingChartTooltipFromEvent,
  floatingChartTooltipStyle,
  type FloatingChartTooltipPosition,
} from "../floatingChartTooltip";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import type {
  KeywordTrafficDetailPayload,
  KeywordTrafficEvent,
  KeywordTrafficHistoryPoint,
  KeywordTrafficListPayload,
  KeywordTrafficProductSummary,
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
const chartTooltipPosition = ref<FloatingChartTooltipPosition | null>(null);

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
const chartBridgeSegments = computed(() => {
  const bridges: string[] = [];
  let previousKnownIndex: number | null = null;
  let crossedMissingPoint = false;
  chartPointPositions.value.forEach((point, index) => {
    if (point.y === null) {
      if (previousKnownIndex !== null) crossedMissingPoint = true;
      return;
    }
    if (crossedMissingPoint && previousKnownIndex !== null) {
      const previous = chartPointPositions.value[previousKnownIndex];
      if (previous?.y !== null && previous?.y !== undefined) {
        bridges.push(
          `M ${previous.x.toFixed(2)} ${previous.y.toFixed(2)} L ${point.x.toFixed(2)} ${point.y.toFixed(2)}`,
        );
      }
    }
    previousKnownIndex = index;
    crossedMissingPoint = false;
  });
  return bridges;
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
  chartTooltipPosition.value = null;
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

function handleChartPointer(event: PointerEvent) {
  if (!chartPointPositions.value.length) return;
  const svg = event.currentTarget as SVGSVGElement;
  const bounds = svg.getBoundingClientRect();
  if (!bounds.width) return;
  const viewX = ((event.clientX - bounds.left) / bounds.width) * chartWidth;
  activePointIndex.value = chartPointPositions.value.reduce(
    (nearestIndex, point, index) =>
      Math.abs(point.x - viewX)
        < Math.abs(chartPointPositions.value[nearestIndex].x - viewX)
        ? index
        : nearestIndex,
    0,
  );
  chartTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function clearChartPointer() {
  activePointIndex.value = null;
  chartTooltipPosition.value = null;
}

function setActivePoint(index: number, event: Event) {
  activePointIndex.value = index;
  chartTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function stepActivePoint(index: number, direction: -1 | 1, event: KeyboardEvent) {
  const current = activePointIndex.value ?? index;
  activePointIndex.value = Math.min(
    chartPointPositions.value.length - 1,
    Math.max(0, current + direction),
  );
  chartTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function setSelectedEvent(event: KeywordTrafficEvent, interactionEvent?: Event) {
  selectedEventId.value = event.id;
  const pointIndex = detail.value?.history.findIndex((point) => point.date === event.effective_date) ?? -1;
  if (pointIndex >= 0) activePointIndex.value = pointIndex;
  chartTooltipPosition.value = interactionEvent
    ? floatingChartTooltipFromEvent(interactionEvent)
    : null;
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

function changeSummary(event: KeywordTrafficEvent) {
  return event.change_label.replace(/^自动(?=基线|变化)/, "");
}

function firstListingTitle(item: KeywordTrafficDetailPayload["product"]) {
  if (!item.first_listed_at) return "首次上架时间";
  return item.first_listed_source === "platform"
    ? "首次上架时间 · 南非时间"
    : "首次上架时间 · 本库最早记录";
}

function firstListingNotice(item: KeywordTrafficDetailPayload["product"]) {
  if (!item.first_listed_at) return "当前没有可用的首次上架或本库历史记录";
  return item.first_listed_source === "platform"
    ? "取自 Takealot Offers 首次上架字段"
    : "旧记录缺少平台时间，仅显示本库最早日期";
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
        <h2>标题关键词档案，流量结果一眼看清</h2>
        <span>
          每次完整 Offer 采集都会归档官方商品标题；发现标题词或词序变化时，系统标记高对比节点，并比较前后的流量水平和趋势速度。
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
        <span>已建档</span>
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
              <em v-else-if="item.keyword_event_count" class="baseline-badge">基线</em>
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

          <section class="product-lifecycle" aria-label="商品上架与补货时间">
            <article>
              <span>{{ firstListingTitle(detail.product) }}</span>
              <strong>{{ detail.product.first_listed_at || "暂无记录" }}</strong>
              <small>{{ firstListingNotice(detail.product) }}</small>
            </article>
            <article>
              <span>最近补货时间 · 北京时间</span>
              <strong>{{ detail.product.latest_restock_date || "暂无记录" }}</strong>
              <small v-if="detail.product.latest_restock_increase !== null">
                平台库存较前一条有效快照 +{{ formatNumber(detail.product.latest_restock_increase) }} 件
              </small>
              <small v-else>尚未观察到平台库存增加</small>
            </article>
          </section>

          <section class="current-keywords">
            <div>
              <p>当前官方标题关键词</p>
              <span v-if="currentKeywords.length">共 {{ currentKeywords.length }} 个</span>
              <span v-else>等待每日 Offer 快照</span>
            </div>
            <div class="keyword-chips">
              <span v-for="keyword in currentKeywords" :key="keyword">{{ keyword }}</span>
              <em v-if="!currentKeywords.length">无需人工操作；下次完整采集会建立首份标题关键词档案。</em>
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
                <span><i class="marker"></i>标题变化</span>
                <span><i class="missing-bridge"></i>缺失区间桥接（非补值）</span>
              </div>
            </header>
            <div class="chart-wrap">
              <svg
                :viewBox="`0 0 ${chartWidth} ${chartHeight}`"
                role="img"
                aria-label="近30天浏览量与标题关键词变化节点趋势图"
                @pointermove="handleChartPointer"
                @pointerleave="clearChartPointer"
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
                <path
                  v-for="(segment, index) in chartBridgeSegments"
                  :key="`missing-bridge-${index}`"
                  class="traffic-line missing-bridge"
                  :d="segment"
                />
                <line
                  v-if="activePoint"
                  class="point-cursor"
                  :x1="activePoint.x"
                  :x2="activePoint.x"
                  :y1="chartTop"
                  :y2="chartTop + chartInnerHeight"
                />
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
                  @click="setSelectedEvent(marker.event, $event)"
                  @keydown.enter.prevent="setSelectedEvent(marker.event, $event)"
                  @keydown.space.prevent="setSelectedEvent(marker.event, $event)"
                >
                  <line :x1="marker.x" :x2="marker.x" :y1="chartTop - 8" :y2="chartTop + chartInnerHeight" />
                  <rect :x="marker.x - 34" :y="14" width="68" height="26" rx="13" />
                  <text :x="marker.x" y="32">
                    {{ marker.event.event_kind === "change" ? `变化 ${marker.number}` : "基线" }}
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
                  @pointerenter="setActivePoint(point.index, $event)"
                  @focus="setActivePoint(point.index, $event)"
                  @click="setActivePoint(point.index, $event)"
                  @keydown.left.prevent="stepActivePoint(point.index, -1, $event)"
                  @keydown.right.prevent="stepActivePoint(point.index, 1, $event)"
                >
                  <circle class="point-hit" :cx="point.x" :cy="point.y ?? chartTop + chartInnerHeight" r="15" />
                  <circle
                    v-if="point.y !== null && point.index === activePointIndex"
                    class="point-halo"
                    :cx="point.x"
                    :cy="point.y"
                    r="10"
                  />
                  <circle v-if="point.y !== null" class="point-dot" :cx="point.x" :cy="point.y" r="3.2" />
                  <path
                    v-else
                    class="point-missing"
                    :d="`M ${point.x - 5} ${chartTop + chartInnerHeight - 5} L ${point.x + 5} ${chartTop + chartInnerHeight + 5} M ${point.x + 5} ${chartTop + chartInnerHeight - 5} L ${point.x - 5} ${chartTop + chartInnerHeight + 5}`"
                  />
                </g>
              </svg>
              <div
                v-if="activePoint && chartTooltipPosition"
                class="point-readout"
                :class="floatingChartTooltipClasses(chartTooltipPosition)"
                :style="floatingChartTooltipStyle(chartTooltipPosition, 330)"
                role="status"
                aria-live="polite"
              >
                <div>
                  <span>数据日期</span>
                  <strong>{{ activePoint.date }}</strong>
                </div>
                <div>
                  <span>滚动指标</span>
                  <strong>近30天浏览量 {{ formatNumber(activePoint.page_views_30_days) }}</strong>
                </div>
                <small v-if="activePoint.page_views_30_days === null">平台该日流量字段缺失，折线保留断点且未补零。</small>
                <small v-else>这是该日看到的滚动30天值，不是单日浏览量。</small>
              </div>
            </div>
            <p class="metric-notice">{{ detail.metric_notice }}</p>
          </section>

          <section class="event-timeline">
            <header>
              <div>
                <p>标题关键词变化时间线</p>
                <h3>点击任一节点切换前后对比</h3>
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
                  <small>{{ event.effective_date }} · 系统识别</small>
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
              <span>无需人工建档；采集成功后系统提取标题词建立基线，以后发现变化就标记节点。</span>
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
.focus-copy p, .current-keywords p, .traffic-chart-card header p, .event-timeline header p { color: var(--muted); font-size: 0.7rem; letter-spacing: 0.08em; margin: 0 0 4px; text-transform: uppercase; }
.focus-copy h3 { font-size: 1.2rem; line-height: 1.35; margin: 0 0 6px; }
.focus-copy span { color: var(--muted); font-size: 0.75rem; overflow-wrap: anywhere; }
.focus-actions { align-items: end; display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.focus-actions label { display: grid; gap: 4px; }
.focus-actions label span { color: var(--muted); font-size: 0.65rem; }
.focus-actions select { font-size: 0.75rem; padding: 8px 9px; }

.product-lifecycle { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 16px; }
.product-lifecycle article { background: #f7f9fb; border: 1px solid #dbe2eb; border-left: 4px solid #315e95; border-radius: 13px; display: grid; gap: 4px; min-width: 0; padding: 13px 15px; }
.product-lifecycle article:last-child { border-left-color: var(--green); }
.product-lifecycle span { color: var(--muted); font-size: 0.68rem; letter-spacing: 0.04em; }
.product-lifecycle strong { font-size: 0.96rem; overflow-wrap: anywhere; }
.product-lifecycle small { color: var(--muted); font-size: 0.68rem; line-height: 1.5; }

.current-keywords { align-items: start; background: #f7f9fb; border: 1px solid #e1e5eb; border-radius: 14px; display: grid; gap: 16px; grid-template-columns: 145px minmax(0, 1fr); margin-top: 18px; padding: 14px 16px; }
.current-keywords > div:first-child span { color: var(--muted); font-size: 0.72rem; }
.keyword-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.keyword-chips span { background: #203652; border-radius: 999px; color: #fff; font-size: 0.72rem; padding: 6px 10px; }
.keyword-chips em { color: var(--muted); font-size: 0.78rem; font-style: normal; line-height: 1.6; }

.traffic-chart-card, .event-timeline { border: 1px solid #dbe1e9; border-radius: 18px; margin-top: 18px; overflow: hidden; }
.traffic-chart-card > header, .event-timeline > header { align-items: center; display: flex; gap: 16px; justify-content: space-between; padding: 16px 18px 8px; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }
.chart-legend span { align-items: center; color: var(--muted); display: flex; font-size: 0.65rem; gap: 5px; }
.chart-legend i { display: inline-block; height: 8px; width: 18px; }
.chart-legend .line { border-top: 3px solid #e05245; height: 0; }
.chart-legend .before { background: rgba(61, 105, 165, 0.14); }
.chart-legend .after { background: rgba(31, 139, 99, 0.14); }
.chart-legend .marker { border-left: 3px solid #c8443b; width: 2px; }
.chart-legend .missing-bridge { border-top: 3px dashed #8a6a48; height: 0; }
.chart-wrap { background: linear-gradient(180deg, #fff 0%, #f8fafc 100%); overflow: hidden; padding: 4px 10px 0; position: relative; }
.chart-wrap svg { display: block; width: 100%; }
.chart-background { fill: #fbfcfe; stroke: #cfd7e2; stroke-width: 1.5; }
.window-band.before { fill: rgba(58, 101, 161, 0.1); }
.window-band.after { fill: rgba(28, 132, 94, 0.1); }
.grid-line { stroke: #d8dfe8; stroke-dasharray: 4 5; }
.axis-label { fill: #536176; font-size: 12px; font-weight: 700; }
.axis-label.y { text-anchor: end; }
.axis-label.x { text-anchor: middle; }
.axis-title { fill: #66738a; font-size: 12px; font-weight: 700; text-anchor: middle; }
.traffic-line { fill: none; stroke: #d43f36; stroke-linecap: round; stroke-linejoin: round; stroke-width: 4.5; }
.traffic-line.halo { stroke: rgba(212, 63, 54, 0.14); stroke-width: 13; }
.traffic-line.missing-bridge { filter: none; stroke: #8a6a48; stroke-dasharray: 7 7; stroke-width: 3; }
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
.point-hit { fill: transparent; stroke: transparent; }
.point-dot { fill: #fff; pointer-events: none; stroke: #d43f36; stroke-width: 2.5; }
.point-halo { fill: rgba(255, 255, 255, 0.94); pointer-events: none; stroke: rgba(212, 63, 54, 0.28); stroke-width: 6; }
.point-cursor { pointer-events: none; stroke: #253a56; stroke-dasharray: 5 5; stroke-width: 1.5; }
.point-missing { fill: none; pointer-events: none; stroke: #9b6521; stroke-linecap: round; stroke-width: 2.5; }
.data-point { cursor: crosshair; outline: none; }
.data-point.active .point-dot, .data-point:focus .point-dot { fill: #d84b41; r: 5; }
.point-readout { background: rgba(24, 38, 58, 0.96); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; box-shadow: 0 14px 30px rgba(24, 38, 58, 0.24); color: #fff; display: grid; gap: 8px; left: 0; max-width: min(330px, calc(100vw - 24px)); padding: 12px 14px; pointer-events: none; position: fixed; top: 0; transform: translateY(14px); width: max-content; z-index: 1300; }
.point-readout.tooltip-align-above { transform: translateY(calc(-100% - 14px)); }
.point-readout > div { align-items: baseline; display: flex; gap: 12px; justify-content: space-between; }
.point-readout span, .point-readout small { color: rgba(255, 255, 255, 0.7); font-size: 0.68rem; line-height: 1.5; }
.point-readout strong { font-size: 0.76rem; text-align: right; }
.point-readout small { border-top: 1px solid rgba(255, 255, 255, 0.13); padding-top: 7px; }
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
  .product-lifecycle { grid-template-columns: 1fr; }
  .current-keywords { grid-template-columns: 1fr; }
  .traffic-chart-card > header, .event-timeline > header { align-items: flex-start; flex-direction: column; }
}

@media (max-width: 560px) {
  .keyword-hero { border-radius: 16px; padding: 22px 18px; }
  .overview-metrics { grid-template-columns: 1fr 1fr; }
  .monitor-workspace { padding: 13px; }
  .product-focus { grid-template-columns: 58px minmax(0, 1fr); }
  .focus-thumb { height: 58px; width: 58px; }
  .focus-actions { align-items: stretch; display: grid; grid-template-columns: 1fr 1fr; }
  .event-card { align-items: start; grid-template-columns: 30px minmax(0, 1fr); }
  .event-outcome { grid-column: 2; justify-items: start; }
  .point-readout { left: 30px; max-width: none; right: 30px; top: 12px; }
}
</style>
