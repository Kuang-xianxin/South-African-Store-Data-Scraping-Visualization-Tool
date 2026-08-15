<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  fetchSalesRevenueRevisions,
  fetchStoreOverview,
  fetchSummaryRange,
} from "../api";
import {
  floatingChartTooltipClasses,
  floatingChartTooltipFromEvent,
  floatingChartTooltipStyle,
  type FloatingChartTooltipPosition,
} from "../floatingChartTooltip";
import { formatChinaDateTime } from "../time";
import type {
  MultiStoreRevenuePoint,
  SalesRevenueRevisionPayload,
  SalesRevenueSource,
  OwnStoreScope,
  StoreOperator,
  StoreOverviewPayload,
  StoreTrafficPoint,
  SummaryPayload,
} from "../types";

const props = defineProps<{
  rangeStart: string;
  rangeEnd: string;
  currentStoreName: string;
  allStoresSelected: boolean;
  storeScope: OwnStoreScope;
  multiStoreLabel: string;
}>();
const emit = defineEmits<{
  selectStore: [storeCode: string];
}>();
const data = ref<SummaryPayload | null>(null);
const storeData = ref<StoreOverviewPayload | null>(null);
const loading = ref(true);
const storeLoading = ref(true);
const error = ref("");
const storeError = ref("");
const activeTrafficIndex = ref<number | null>(null);
const activeRevenueIndex = ref<number | null>(null);
const trafficTooltipPosition = ref<FloatingChartTooltipPosition | null>(null);
const revenueTooltipPosition = ref<FloatingChartTooltipPosition | null>(null);
const salesAuditOpen = ref(false);
const salesAuditLoading = ref(false);
const salesAuditError = ref("");
const salesAuditData = ref<SalesRevenueRevisionPayload | null>(null);
const salesAuditStart = ref(props.rangeStart);
const salesAuditEnd = ref(props.rangeEnd);
let loadRequestId = 0;
let salesAuditRequestId = 0;

const maxUnits = computed(() =>
  Math.max(1, ...(data.value?.sales_series.map((item) => item.ordered_units ?? 0) ?? [1])),
);

const TRAFFIC_WIDTH = 760;
const TRAFFIC_HEIGHT = 250;
const TRAFFIC_LEFT = 64;
const TRAFFIC_RIGHT = 18;
const TRAFFIC_TOP = 20;
const TRAFFIC_BOTTOM = 38;

function trafficValue(point: StoreTrafficPoint | null) {
  if (!point) return null;
  return point.page_views_30_days_total ?? point.reference?.page_views_30_days_total ?? null;
}

function usesTrafficReference(point: StoreTrafficPoint) {
  return point.page_views_30_days_total === null && point.reference !== null;
}

const trafficChart = computed(() => {
  const source = data.value?.traffic_series ?? [];
  const values = source
    .map((point) => trafficValue(point))
    .filter((value): value is number => value !== null);
  if (!source.length) {
    return {
      dots: [],
      officialSegments: [],
      partialSegments: [],
      referenceSegments: [],
      missingBridgeSegments: [],
      ticks: [],
      labels: [],
    };
  }
  const rawMin = values.length ? Math.min(...values) : 0;
  const rawMax = values.length ? Math.max(...values) : 1;
  const padding = rawMin === rawMax ? Math.max(1, rawMax * 0.08) : (rawMax - rawMin) * 0.12;
  const minimum = Math.max(0, rawMin - padding);
  const maximum = Math.max(minimum + 1, rawMax + padding);
  const plotWidth = TRAFFIC_WIDTH - TRAFFIC_LEFT - TRAFFIC_RIGHT;
  const plotHeight = TRAFFIC_HEIGHT - TRAFFIC_TOP - TRAFFIC_BOTTOM;
  const x = (index: number) =>
    TRAFFIC_LEFT + (source.length === 1 ? plotWidth / 2 : (index / (source.length - 1)) * plotWidth);
  const y = (value: number) =>
    TRAFFIC_TOP + ((maximum - value) / (maximum - minimum)) * plotHeight;
  const dots = source.map((point, index) => {
    const value = trafficValue(point);
    const isReference = usesTrafficReference(point);
    return {
      point,
      value,
      isReference,
      missingProductCount: isReference
        ? point.reference?.missing_product_count ?? 0
        : point.missing_product_count,
      x: x(index),
      y: value === null ? TRAFFIC_TOP + plotHeight : y(value),
    };
  });
  const officialSegments: string[] = [];
  const partialSegments: string[] = [];
  const referenceSegments: string[] = [];
  for (let index = 1; index < dots.length; index += 1) {
    const previous = dots[index - 1];
    const current = dots[index];
    if (previous.value === null || current.value === null) continue;
    const segment = `${previous.x},${previous.y} ${current.x},${current.y}`;
    if (previous.isReference || current.isReference) {
      referenceSegments.push(segment);
    } else if (previous.missingProductCount > 0 || current.missingProductCount > 0) {
      partialSegments.push(segment);
    } else {
      officialSegments.push(segment);
    }
  }
  const missingBridgeSegments: string[] = [];
  let previousKnownIndex: number | null = null;
  let crossedMissingPoint = false;
  dots.forEach((dot, index) => {
    if (dot.value === null) {
      if (previousKnownIndex !== null) crossedMissingPoint = true;
      return;
    }
    if (crossedMissingPoint && previousKnownIndex !== null) {
      const previous = dots[previousKnownIndex];
      missingBridgeSegments.push(`${previous.x},${previous.y} ${dot.x},${dot.y}`);
    }
    previousKnownIndex = index;
    crossedMissingPoint = false;
  });
  const ticks = [maximum, (maximum + minimum) / 2, minimum].map((value) => ({
    value,
    y: y(value),
  }));
  const labelEvery = Math.max(1, Math.ceil(source.length / 6));
  const labels = dots.filter((_, index) => {
    if (index === 0 || index === dots.length - 1) return true;
    if (index % labelEvery !== 0) return false;
    return dots.length - 1 - index >= Math.max(2, Math.floor(labelEvery * 0.6));
  });
  return {
    dots,
    officialSegments,
    partialSegments,
    referenceSegments,
    missingBridgeSegments,
    ticks,
    labels,
  };
});

const latestTrafficPoint = computed(() => data.value?.traffic_series.at(-1) ?? null);
const latestTrafficValue = computed(() => trafficValue(latestTrafficPoint.value));
const activeTrafficDot = computed(() => {
  const index = activeTrafficIndex.value;
  return index === null ? null : trafficChart.value.dots[index] ?? null;
});

type SummableKpi =
  | "latest_ordered_units"
  | "latest_ordered_revenue"
  | "seven_day_ordered_units"
  | "stockout_products";

function aggregateKpi(key: SummableKpi, requireMetricDate = false) {
  const stores = storeData.value?.stores ?? [];
  const values = stores
    .filter((store) => !requireMetricDate || store.latest_metric_date !== null)
    .map((store) => store.kpis[key])
    .filter((value): value is number => value !== null);
  return {
    value: values.length ? values.reduce((sum, value) => sum + value, 0) : null,
    coverage: values.length,
    total: stores.length,
  };
}

const storeTotals = computed(() => ({
  latestUnits: aggregateKpi("latest_ordered_units"),
  latestRevenue: aggregateKpi("latest_ordered_revenue"),
  sevenDayUnits: aggregateKpi("seven_day_ordered_units"),
  stockouts: aggregateKpi("stockout_products", true),
}));

const REVENUE_WIDTH = 760;
const REVENUE_HEIGHT = 250;
const REVENUE_LEFT = 76;
const REVENUE_RIGHT = 18;
const REVENUE_TOP = 20;
const REVENUE_BOTTOM = 38;

const revenueChart = computed(() => {
  const source = storeData.value?.sales_revenue_series ?? [];
  const values = source
    .map((point) => point.total_ordered_revenue)
    .filter((value): value is number => value !== null);
  if (!source.length) {
    return {
      dots: [],
      segments: [],
      pendingSegments: [],
      revisedSegments: [],
      missingBridgeSegments: [],
      ticks: [],
      labels: [],
    };
  }
  const maximum = Math.max(1, ...(values.map((value) => value * 1.08)));
  const plotWidth = REVENUE_WIDTH - REVENUE_LEFT - REVENUE_RIGHT;
  const plotHeight = REVENUE_HEIGHT - REVENUE_TOP - REVENUE_BOTTOM;
  const x = (index: number) =>
    REVENUE_LEFT
    + (source.length === 1 ? plotWidth / 2 : (index / (source.length - 1)) * plotWidth);
  const y = (value: number) => REVENUE_TOP + ((maximum - value) / maximum) * plotHeight;
  const dots = source.map((point, index) => ({
    point,
    value: point.total_ordered_revenue,
    x: x(index),
    y: point.total_ordered_revenue === null
      ? REVENUE_TOP + plotHeight
      : y(point.total_ordered_revenue),
  }));
  const segments: string[] = [];
  const pendingSegments: string[] = [];
  const revisedSegments: string[] = [];
  for (let index = 1; index < dots.length; index += 1) {
    const previous = dots[index - 1];
    const current = dots[index];
    if (previous.value === null || current.value === null) continue;
    const segment = `${previous.x},${previous.y} ${current.x},${current.y}`;
    if (previous.point.data_status === "pending" || current.point.data_status === "pending") {
      pendingSegments.push(segment);
    } else if (
      previous.point.data_status === "revised"
      || current.point.data_status === "revised"
    ) {
      revisedSegments.push(segment);
    } else {
      segments.push(segment);
    }
  }
  const missingBridgeSegments: string[] = [];
  let previousKnownIndex: number | null = null;
  let crossedMissingPoint = false;
  dots.forEach((dot, index) => {
    if (dot.value === null) {
      if (previousKnownIndex !== null) crossedMissingPoint = true;
      return;
    }
    if (crossedMissingPoint && previousKnownIndex !== null) {
      const previous = dots[previousKnownIndex];
      missingBridgeSegments.push(`${previous.x},${previous.y} ${dot.x},${dot.y}`);
    }
    previousKnownIndex = index;
    crossedMissingPoint = false;
  });
  const ticks = [maximum, maximum / 2, 0].map((value) => ({
    value,
    y: y(value),
  }));
  const labelEvery = Math.max(1, Math.ceil(source.length / 6));
  const labels = dots.filter((_, index) => {
    if (index === 0 || index === dots.length - 1) return true;
    if (index % labelEvery !== 0) return false;
    return dots.length - 1 - index >= Math.max(2, Math.floor(labelEvery * 0.6));
  });
  return {
    dots,
    segments,
    pendingSegments,
    revisedSegments,
    missingBridgeSegments,
    ticks,
    labels,
  };
});

const latestRevenuePoint = computed(() => storeData.value?.sales_revenue_series.at(-1) ?? null);
const activeRevenueDot = computed(() => {
  const index = activeRevenueIndex.value;
  return index === null ? null : revenueChart.value.dots[index] ?? null;
});
const salesAuditPageCount = computed(() => Math.max(
  1,
  Math.ceil((salesAuditData.value?.total ?? 0) / (salesAuditData.value?.page_size ?? 20)),
));

const overallHealthText = computed(() => {
  const summary = storeData.value?.health_summary;
  if (!summary) return "正在核对健康信号";
  if (summary.attention) return `${summary.attention} 家店铺需优先关注`;
  if (summary.data_gap) return `${summary.data_gap} 家店铺存在数据缺口`;
  return "当前口径未发现缺货或数据缺口";
});

watch(
  () => [
    props.rangeStart,
    props.rangeEnd,
    props.allStoresSelected,
    props.storeScope,
  ],
  load,
  { immediate: true },
);

watch(
  () => [props.rangeStart, props.rangeEnd],
  ([startDate, endDate]) => {
    salesAuditStart.value = startDate;
    salesAuditEnd.value = endDate;
    salesAuditData.value = null;
    if (salesAuditOpen.value) void loadSalesAudit(1);
  },
);

async function load() {
  const requestId = ++loadRequestId;
  loading.value = !props.allStoresSelected;
  storeLoading.value = props.allStoresSelected;
  error.value = "";
  storeError.value = "";
  if (props.allStoresSelected) {
    data.value = null;
    try {
      const nextStoreData = await fetchStoreOverview(
        props.rangeStart,
        props.rangeEnd,
        props.storeScope === "operating" ? "operating" : "all",
      );
      if (requestId !== loadRequestId) return;
      storeData.value = nextStoreData;
    } catch (reason) {
      if (requestId !== loadRequestId) return;
      storeData.value = null;
      storeError.value = reason instanceof Error
        ? reason.message
        : `${props.multiStoreLabel}经营总览读取失败`;
    } finally {
      if (requestId === loadRequestId) storeLoading.value = false;
    }
    return;
  }

  storeData.value = null;
  try {
    const nextData = await fetchSummaryRange(props.rangeStart, props.rangeEnd);
    if (requestId !== loadRequestId) return;
    data.value = nextData;
  } catch (reason) {
    if (requestId !== loadRequestId) return;
    data.value = null;
    error.value = reason instanceof Error
      ? reason.message
      : "当前店铺经营数据读取失败";
  } finally {
    if (requestId === loadRequestId) loading.value = false;
  }
}

function coverageLabel(coverage: number, total: number) {
  return coverage === total ? `${total} 家完整` : `已返回 ${coverage}/${total} 家`;
}

function offerCoverage(coverage: number, total: number) {
  if (!total) return "暂无商品库存快照";
  return coverage === total
    ? `${total} 个商品完整`
    : `已返回 ${coverage}/${total} 个商品`;
}

function operatorRoleLabel(role: StoreOperator["role"]) {
  return ({
    admin: "管理员",
    operator: "运营",
    viewer: "查看",
    selection: "选品",
  } as const)[role];
}

function operatorNames(operators: StoreOperator[] | undefined) {
  return operators?.length
    ? operators.map((operator) => operator.display_name).join("、")
    : "暂未分配运营账号";
}

function trafficCoverage(point: StoreTrafficPoint | null) {
  if (!point || trafficValue(point) === null) return "暂无可用周期末合计或同日参考";
  const source = usesTrafficReference(point) ? point.reference : point;
  if (!source) return "暂无可用周期末合计或同日参考";
  const returned = source.product_count - source.missing_product_count;
  const suffix = usesTrafficReference(point) ? " · 同日参考" : "";
  return `${point.business_date} · 商品覆盖 ${returned}/${source.product_count}${suffix}`;
}

function number(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("zh-CN").format(value);
}

function currency(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 0,
      }).format(value);
}

function percent(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${value.toFixed(2)}%`;
}

function day(value: string) {
  return value.slice(5);
}

function compactCurrency(value: number) {
  return new Intl.NumberFormat("en-ZA", {
    style: "currency",
    currency: "ZAR",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

async function toggleSalesAudit() {
  salesAuditOpen.value = !salesAuditOpen.value;
  if (salesAuditOpen.value && salesAuditData.value === null) {
    await loadSalesAudit(1);
  }
}

async function loadSalesAudit(page = 1) {
  const requestId = ++salesAuditRequestId;
  salesAuditLoading.value = true;
  salesAuditError.value = "";
  try {
    const payload = await fetchSalesRevenueRevisions({
      startDate: salesAuditStart.value,
      endDate: salesAuditEnd.value,
      page,
      pageSize: 20,
      storeScope: props.storeScope === "operating" ? "operating" : "all",
    });
    if (requestId === salesAuditRequestId) salesAuditData.value = payload;
  } catch (reason) {
    if (requestId !== salesAuditRequestId) return;
    salesAuditError.value = reason instanceof Error
      ? reason.message
      : "销售额修订记录读取失败";
  } finally {
    if (requestId === salesAuditRequestId) salesAuditLoading.value = false;
  }
}

function salesSourceLabel(source: SalesRevenueSource) {
  const range = source.requested_start && source.requested_end
    ? `${source.requested_start} 至 ${source.requested_end}`
    : "未记录请求范围";
  const captured = source.collected_at || source.verified_at || source.recorded_at;
  const time = captured ? formatChinaDateTime(captured) : "历史来源时间未记录";
  return `${source.label} · ${range} · ${time}`;
}

function shortRunId(value: string | null | undefined) {
  return value ? value.slice(0, 8) : "无批次编号";
}

function nearestChartPointIndex(
  event: PointerEvent,
  viewBoxWidth: number,
  points: Array<{ x: number }>,
) {
  if (!points.length) return null;
  const svg = event.currentTarget as SVGSVGElement;
  const bounds = svg.getBoundingClientRect();
  if (!bounds.width) return null;
  const viewX = ((event.clientX - bounds.left) / bounds.width) * viewBoxWidth;
  return points.reduce(
    (nearestIndex, point, index) =>
      Math.abs(point.x - viewX) < Math.abs(points[nearestIndex].x - viewX)
        ? index
        : nearestIndex,
    0,
  );
}

function handleRevenuePointer(event: PointerEvent) {
  const index = nearestChartPointIndex(event, REVENUE_WIDTH, revenueChart.value.dots);
  if (index === null) return;
  activeRevenueIndex.value = index;
  revenueTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function clearRevenuePointer() {
  activeRevenueIndex.value = null;
  revenueTooltipPosition.value = null;
}

function setRevenuePoint(index: number, event: Event) {
  activeRevenueIndex.value = index;
  revenueTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function stepRevenuePoint(index: number, direction: -1 | 1, event: KeyboardEvent) {
  const current = activeRevenueIndex.value ?? index;
  activeRevenueIndex.value = Math.min(
    revenueChart.value.dots.length - 1,
    Math.max(0, current + direction),
  );
  revenueTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function handleTrafficPointer(event: PointerEvent) {
  const index = nearestChartPointIndex(event, TRAFFIC_WIDTH, trafficChart.value.dots);
  if (index === null) return;
  activeTrafficIndex.value = index;
  trafficTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function clearTrafficPointer() {
  activeTrafficIndex.value = null;
  trafficTooltipPosition.value = null;
}

function setTrafficPoint(index: number, event: Event) {
  activeTrafficIndex.value = index;
  trafficTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function stepTrafficPoint(index: number, direction: -1 | 1, event: KeyboardEvent) {
  const current = activeTrafficIndex.value ?? index;
  activeTrafficIndex.value = Math.min(
    trafficChart.value.dots.length - 1,
    Math.max(0, current + direction),
  );
  trafficTooltipPosition.value = floatingChartTooltipFromEvent(event);
}

function revenuePointTitle(point: MultiStoreRevenuePoint) {
  if (point.total_ordered_revenue !== null) {
    const status = point.data_status === "pending"
      ? `；${point.pending_reconciliation_store_count} 家待失败后核验，${point.unverified_source_store_count} 家来源未建档`
      : point.data_status === "revised"
        ? `；已记录 ${point.revision_count} 条历史修订`
        : "；来源已核验";
    const coverage = point.missing_store_count
      ? `；已有 ${point.covered_store_count}/${point.store_count} 家合计，缺失 ${point.missing_store_count} 家且未按 0 补齐`
      : `；店铺覆盖 ${point.covered_store_count}/${point.store_count}`;
    return `${point.metric_date}（南非业务日）：下单金额${point.missing_store_count ? "部分合计" : "合计"} ${currency(point.total_ordered_revenue)}${coverage}${status}`;
  }
  return `${point.metric_date}（南非业务日）：没有任何店铺返回销售额，折线保留断点`;
}

function trafficSlotLabel(slot: string) {
  return ({ morning: "早间采集", evening: "晚间采集", manual: "手动刷新" } as Record<string, string>)[slot]
    ?? slot;
}

function trafficPointTitle(point: StoreTrafficPoint) {
  const capture = formatChinaDateTime(point.captured_at);
  if (point.page_views_30_days_total !== null) {
    const returned = point.product_count - point.missing_product_count;
    return `${point.business_date}：已返回商品近30天浏览量合计 ${number(point.page_views_30_days_total)}；覆盖 ${returned}/${point.product_count} 个商品，缺失 ${point.missing_product_count} 个；周期末采集 ${capture}`;
  }
  if (point.status === "failed") {
    if (point.reference) {
      const returned = point.reference.product_count - point.reference.missing_product_count;
      return `${point.business_date}：09:00 周期末刷新失败；橙色虚线参考 ${trafficSlotLabel(point.reference.source_slot)} ${formatChinaDateTime(point.reference.captured_at)}，已返回商品近30天浏览量合计 ${number(point.reference.page_views_30_days_total)}；覆盖 ${returned}/${point.reference.product_count} 个商品，缺失 ${point.reference.missing_product_count} 个（未补 0）`;
    }
    return `${point.business_date}：周期末刷新失败，本日未记录合计`;
  }
  return `${point.business_date}：全部商品都缺少近30天浏览量；周期末采集 ${capture}`;
}
</script>

<template>
  <div class="erp-page overview-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">
          {{ allStoresSelected ? "MULTI-STORE COMMAND" : "BUSINESS PULSE" }}
        </p>
        <h2>{{ allStoresSelected ? `${multiStoreLabel}经营总览` : `${currentStoreName} 经营总览` }}</h2>
      </div>
      <p v-if="allStoresSelected">
        数据范围 {{ rangeStart }} 至 {{ rangeEnd }} · 汇总{{ multiStoreLabel }}中已启用且已接入的店铺 ·
        各店按自身最新可用指标日展示
      </p>
      <p v-else>
        数据范围 {{ rangeStart }} 至 {{ rangeEnd }} · 最新可用指标日 {{ data?.latest_metric_date || "暂无" }} ·
        下单件数为主销售口径
      </p>
    </div>

    <section v-if="allStoresSelected" class="erp-panel multi-store-panel">
      <div class="panel-heading multi-store-heading">
        <div>
          <p class="section-kicker">
            {{ storeScope === "operating" ? "MY OPERATING STORES" : "ALL CONNECTED STORES" }}
          </p>
          <h3>店铺经营对比</h3>
        </div>
        <span v-if="storeData">当前可见 {{ storeData.store_count }} 家</span>
      </div>
      <div v-if="storeLoading" class="state-card slim">正在汇总各店经营数据……</div>
      <div v-else-if="storeError" class="state-card error slim">{{ storeError }}</div>
      <template v-else-if="storeData">
        <div
          class="command-health"
          :class="{
            attention: storeData.health_summary.attention > 0,
            'data-gap': storeData.health_summary.attention === 0
              && storeData.health_summary.data_gap > 0,
          }"
        >
          <div>
            <span>全盘健康定位</span>
            <strong>{{ overallHealthText }}</strong>
            <small>按缺货和数据完整性直接判断；数据异常暂不在 ERP 前端展示</small>
          </div>
          <dl>
            <div>
              <dt>需关注</dt>
              <dd>{{ storeData.health_summary.attention }}</dd>
            </div>
            <div>
              <dt>数据待补</dt>
              <dd>{{ storeData.health_summary.data_gap }}</dd>
            </div>
            <div>
              <dt>当前正常</dt>
              <dd>{{ storeData.health_summary.healthy }}</dd>
            </div>
          </dl>
        </div>

        <div class="multi-total-grid">
          <article class="multi-total-primary">
            <span>最新日下单件数合计</span>
            <strong>{{ number(storeTotals.latestUnits.value) }}</strong>
            <small>{{ coverageLabel(storeTotals.latestUnits.coverage, storeTotals.latestUnits.total) }}</small>
          </article>
          <article>
            <span>最新日下单金额合计</span>
            <strong>{{ currency(storeTotals.latestRevenue.value) }}</strong>
            <small>{{ coverageLabel(storeTotals.latestRevenue.coverage, storeTotals.latestRevenue.total) }}</small>
          </article>
          <article>
            <span>近 7 日下单件数合计</span>
            <strong>{{ number(storeTotals.sevenDayUnits.value) }}</strong>
            <small>{{ coverageLabel(storeTotals.sevenDayUnits.coverage, storeTotals.sevenDayUnits.total) }}</small>
          </article>
          <article class="multi-total-alert">
            <span>缺货商品合计</span>
            <strong>{{ number(storeTotals.stockouts.value) }}</strong>
            <small>{{ coverageLabel(storeTotals.stockouts.coverage, storeTotals.stockouts.total) }}</small>
          </article>
        </div>

        <section class="revenue-command" aria-labelledby="multi-store-revenue-title">
          <div class="logistics-command-heading revenue-heading">
            <div>
              <p class="section-kicker">SELECTED RANGE REVENUE</p>
              <h4 id="multi-store-revenue-title">{{ storeData.store_count }} 店总销售额趋势</h4>
            </div>
            <span>
              {{ storeData.range_start }} 至 {{ storeData.range_end }} · 已结束南非业务日至
              {{ storeData.sales_revenue_completed_through }}
            </span>
          </div>
          <p class="revenue-definition">
            每个点汇总{{ multiStoreLabel }}中已接入店铺的 <code>ordered_revenue</code>。
            只要至少一家店返回该业务日金额，就绘制已有店铺合计并标注覆盖数；缺失店铺不补 0，
            没有任何店铺返回金额时才保留断点。
            当前仍在进行的 SAST 业务日不进入折线；今天的 Sales 拉取会先修订昨天及更早日期，
            等该 SAST 日结束后才作为完整历史日展示。
          </p>
          <div
            v-if="storeData.sales_reconciliation.pending_store_count"
            class="sales-reconciliation-alert pending"
            role="alert"
          >
            <strong>
              周期末失败后仍有 {{ storeData.sales_reconciliation.pending_store_count }} 家店待新 Sales 批次核验
            </strong>
            <span>
              业务日 {{ storeData.sales_reconciliation.period_end_business_date || "未记录" }} 共
              {{ storeData.sales_reconciliation.failed_store_count }} 家周期末失败；
              {{ storeData.sales_reconciliation.recovered_store_count }} 家已由后续成功批次恢复。
              待核验区间以橙色虚线显示，不再伪装成正常绿线。
            </span>
          </div>
          <div
            v-else-if="storeData.sales_reconciliation.failed_store_count"
            class="sales-reconciliation-alert recovered"
          >
            <strong>周期末失败事实已保留，后续 Sales 批次已完成数值核验</strong>
            <span>
              原失败记录没有改写；若金额或件数发生变化，修订前后值与来源批次均保存在下方审计记录中。
            </span>
          </div>
          <div
            v-if="storeData.sales_reconciliation.revision_count"
            class="sales-reconciliation-alert revised"
          >
            <strong>已记录 {{ storeData.sales_reconciliation.revision_count }} 条销售额历史修订</strong>
            <span>
              最近修订：{{ formatChinaDateTime(storeData.sales_reconciliation.latest_revision_at) }}。
              蓝色折线/点表示该业务日曾被后续 Sales 数据纠偏。
            </span>
          </div>
          <div v-if="!storeData.sales_revenue_series.length" class="state-card slim">
            暂无跨店销售额趋势数据。
          </div>
          <template v-else>
            <div
              class="revenue-latest"
              :class="{
                incomplete: (latestRevenuePoint?.missing_store_count ?? 0) > 0 || latestRevenuePoint?.total_ordered_revenue === null,
                pending: latestRevenuePoint?.data_status === 'pending',
                revised: latestRevenuePoint?.data_status === 'revised',
              }"
            >
              <strong>{{ currency(latestRevenuePoint?.total_ordered_revenue) }}</strong>
              <span v-if="latestRevenuePoint?.total_ordered_revenue === null">
                {{ latestRevenuePoint?.metric_date }} · 暂无任何店铺销售额，折线保留断点
              </span>
              <span v-else-if="latestRevenuePoint?.missing_store_count">
                {{ latestRevenuePoint?.metric_date }} · 已有
                {{ latestRevenuePoint?.covered_store_count }}/{{ latestRevenuePoint?.store_count }} 家合计，
                缺失 {{ latestRevenuePoint?.missing_store_count }} 家且未按 0 补齐
              </span>
              <span v-else>
                {{ latestRevenuePoint?.metric_date }} · 店铺覆盖
                {{ latestRevenuePoint?.covered_store_count }}/{{ latestRevenuePoint?.store_count }} 家
              </span>
            </div>
            <div class="traffic-chart-scroll">
              <div class="trend-chart-stage">
                <svg
                  class="traffic-chart"
                  :viewBox="`0 0 ${REVENUE_WIDTH} ${REVENUE_HEIGHT}`"
                  role="img"
                  aria-labelledby="multi-store-revenue-svg-title multi-store-revenue-svg-description"
                  @pointermove="handleRevenuePointer"
                  @pointerleave="clearRevenuePointer"
                >
                <title id="multi-store-revenue-svg-title">合并范围内店铺已结束业务日总销售额折线图</title>
                <desc id="multi-store-revenue-svg-description">当前仍在进行的SAST日不进入折线；绿色折线为来源已核验的完整店铺日；橙色虚线表示周期末失败后尚待新的 Sales 批次核验；蓝色线表示该日已有可审计历史修订；店铺覆盖不完整时绘制已有店铺合计并披露覆盖数，缺失店铺不按0补齐。</desc>
                <g class="traffic-grid">
                  <template v-for="tick in revenueChart.ticks" :key="tick.y">
                    <line :x1="REVENUE_LEFT" :x2="REVENUE_WIDTH - REVENUE_RIGHT" :y1="tick.y" :y2="tick.y" />
                    <text :x="REVENUE_LEFT - 10" :y="tick.y + 4">{{ compactCurrency(tick.value) }}</text>
                  </template>
                </g>
                <line
                  v-if="activeRevenueDot"
                  class="trend-crosshair"
                  :x1="activeRevenueDot.x"
                  :x2="activeRevenueDot.x"
                  :y1="REVENUE_TOP"
                  :y2="REVENUE_HEIGHT - REVENUE_BOTTOM"
                />
                <polyline
                  v-for="(segment, index) in revenueChart.segments"
                  :key="`revenue-${index}`"
                  class="revenue-line"
                  :points="segment"
                />
                <polyline
                  v-for="(segment, index) in revenueChart.pendingSegments"
                  :key="`revenue-pending-${index}`"
                  class="revenue-line reconciliation-pending"
                  :points="segment"
                />
                <polyline
                  v-for="(segment, index) in revenueChart.revisedSegments"
                  :key="`revenue-revised-${index}`"
                  class="revenue-line revised"
                  :points="segment"
                />
                <polyline
                  v-for="(segment, index) in revenueChart.missingBridgeSegments"
                  :key="`revenue-missing-bridge-${index}`"
                  class="revenue-line missing-bridge"
                  :points="segment"
                />
                <g
                  v-for="(dot, index) in revenueChart.dots"
                  :key="dot.point.metric_date"
                  class="trend-data-point"
                  :class="{ active: index === activeRevenueIndex }"
                  tabindex="0"
                  role="button"
                  :aria-label="revenuePointTitle(dot.point)"
                  @pointerenter="setRevenuePoint(index, $event)"
                  @focus="setRevenuePoint(index, $event)"
                  @click="setRevenuePoint(index, $event)"
                  @keydown.left.prevent="stepRevenuePoint(index, -1, $event)"
                  @keydown.right.prevent="stepRevenuePoint(index, 1, $event)"
                >
                  <circle class="trend-point-hit" :cx="dot.x" :cy="dot.y" r="14" />
                  <circle
                    v-if="index === activeRevenueIndex"
                    class="trend-point-halo"
                    :cx="dot.x"
                    :cy="dot.y"
                    r="9"
                  />
                  <circle
                    v-if="dot.value !== null"
                    :class="[
                      'revenue-dot',
                      {
                        pending: dot.point.data_status === 'pending',
                        revised: dot.point.data_status === 'revised',
                      },
                    ]"
                    :cx="dot.x"
                    :cy="dot.y"
                    r="4"
                  />
                  <path
                    v-else
                    class="trend-missing-mark"
                    :d="`M ${dot.x - 5} ${dot.y - 5} L ${dot.x + 5} ${dot.y + 5} M ${dot.x + 5} ${dot.y - 5} L ${dot.x - 5} ${dot.y + 5}`"
                  />
                </g>
                <g class="traffic-axis-labels">
                  <text
                    v-for="label in revenueChart.labels"
                    :key="label.point.metric_date"
                    :x="label.x"
                    :y="REVENUE_HEIGHT - 12"
                  >{{ day(label.point.metric_date) }}</text>
                </g>
                </svg>
                <div
                  v-if="activeRevenueDot && revenueTooltipPosition"
                  class="trend-hover-card"
                  :class="floatingChartTooltipClasses(revenueTooltipPosition)"
                  :style="floatingChartTooltipStyle(revenueTooltipPosition, 310)"
                  role="status"
                  aria-live="polite"
                >
                  <div class="trend-hover-heading">
                    <span>南非业务日</span>
                    <strong>{{ activeRevenueDot.point.metric_date }}</strong>
                  </div>
                  <dl>
                    <div>
                      <dt>{{ activeRevenueDot.point.missing_store_count ? "已有店铺销售额合计" : `${storeData.store_count} 店总销售额` }}</dt>
                      <dd>{{ currency(activeRevenueDot.value) }}</dd>
                    </div>
                    <div>
                      <dt>店铺覆盖</dt>
                      <dd>{{ activeRevenueDot.point.covered_store_count }}/{{ activeRevenueDot.point.store_count }} 家</dd>
                    </div>
                    <div v-if="activeRevenueDot.point.missing_store_count">
                      <dt>数据状态</dt>
                      <dd class="warning">
                        缺失 {{ activeRevenueDot.point.missing_store_count }} 家；当前金额仅合计已有店铺，缺失店铺未按 0 补齐
                      </dd>
                    </div>
                    <div v-else-if="activeRevenueDot.point.data_status === 'pending'">
                      <dt>数据状态</dt>
                      <dd class="warning">
                        {{ activeRevenueDot.point.pending_reconciliation_store_count }} 家待周期末失败后核验；
                        {{ activeRevenueDot.point.unverified_source_store_count }} 家来源未建档
                      </dd>
                    </div>
                    <div v-else-if="activeRevenueDot.point.data_status === 'revised'">
                      <dt>数据状态</dt>
                      <dd class="revised">
                        已纠偏 · {{ activeRevenueDot.point.revision_count }} 条修订，
                        {{ activeRevenueDot.point.revised_store_count }} 家涉及变化
                      </dd>
                    </div>
                    <div v-else>
                      <dt>数据状态</dt>
                      <dd>合并范围内店铺完整且来源已核验</dd>
                    </div>
                    <div v-if="activeRevenueDot.point.latest_sales_verified_at">
                      <dt>最近来源核验</dt>
                      <dd>{{ formatChinaDateTime(activeRevenueDot.point.latest_sales_verified_at) }}</dd>
                    </div>
                  </dl>
                </div>
              </div>
            </div>
            <div class="traffic-legend revenue-legend">
              <span><i></i>合并范围内店铺完整且来源已核验</span>
              <span><i class="reconciliation-pending"></i>周期末失败后待新批次核验</span>
              <span><i class="revised"></i>后续 Sales 数据已纠偏并留审计</span>
              <span><i class="missing"></i>店铺缺失，未展示部分合计</span>
              <span><i class="missing-bridge"></i>缺失区间虚线桥接，仅连接两端真实值</span>
            </div>
          </template>
        </section>

        <section class="sales-audit-panel" aria-labelledby="sales-audit-title">
          <div class="sales-audit-heading">
            <div>
              <p class="section-kicker">REVISION AUDIT</p>
              <h4 id="sales-audit-title">销售额历史修订记录</h4>
              <span>不可变记录保留更新前后金额、件数、数据源批次、请求范围和发现时间。</span>
            </div>
            <button type="button" class="secondary-button" @click="toggleSalesAudit">
              {{ salesAuditOpen ? "收起记录" : "展开记录" }}
            </button>
          </div>
          <template v-if="salesAuditOpen">
            <form class="sales-audit-filter" @submit.prevent="loadSalesAudit(1)">
              <label>
                <span>开始业务日</span>
                <input v-model="salesAuditStart" type="date" :max="salesAuditEnd" required />
              </label>
              <label>
                <span>结束业务日</span>
                <input v-model="salesAuditEnd" type="date" :min="salesAuditStart" required />
              </label>
              <button type="submit" class="primary-button" :disabled="salesAuditLoading">
                {{ salesAuditLoading ? "查询中…" : "查询修订" }}
              </button>
            </form>
            <div v-if="salesAuditError" class="state-card error slim">{{ salesAuditError }}</div>
            <div v-else-if="salesAuditLoading" class="state-card slim">正在读取本地修订审计…</div>
            <div
              v-else-if="salesAuditData && !salesAuditData.items.length"
              class="state-card slim"
            >
              所选日期没有数值变化记录；来源核验时间仍会随成功 Sales 批次更新。
            </div>
            <template v-else-if="salesAuditData">
              <div class="table-scroll sales-audit-table-wrap">
                <table class="sales-audit-table">
                  <thead>
                    <tr>
                      <th>发现时间</th>
                      <th>店铺 / 业务日</th>
                      <th>销售额</th>
                      <th>件数</th>
                      <th>更新前来源</th>
                      <th>更新后来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="revision in salesAuditData.items" :key="`${revision.store_code}-${revision.id}`">
                      <td>{{ formatChinaDateTime(revision.detected_at) }}</td>
                      <td>
                        <strong>{{ revision.store_name }}</strong>
                        <span>{{ revision.metric_date }} · {{ revision.change_type === "backfilled" ? "补录" : "纠偏" }}</span>
                      </td>
                      <td>
                        <strong>{{ currency(revision.before_ordered_revenue) }} → {{ currency(revision.after_ordered_revenue) }}</strong>
                        <span :class="{ negative: (revision.revenue_delta ?? 0) < 0 }">
                          变化 {{ currency(revision.revenue_delta) }}
                        </span>
                      </td>
                      <td>
                        {{ number(revision.before_ordered_units) }} → {{ number(revision.after_ordered_units) }}
                        <span>变化 {{ number(revision.units_delta) }}</span>
                      </td>
                      <td>
                        <span>{{ salesSourceLabel(revision.before_source) }}</span>
                        <code>{{ shortRunId(revision.before_source.run_id) }}</code>
                      </td>
                      <td>
                        <span>{{ salesSourceLabel(revision.after_source) }}</span>
                        <code>{{ shortRunId(revision.source_run_id) }}</code>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="sales-audit-pagination">
                <span>共 {{ salesAuditData.total }} 条 · 第 {{ salesAuditData.page }}/{{ salesAuditPageCount }} 页</span>
                <div>
                  <button
                    type="button"
                    class="secondary-button"
                    :disabled="salesAuditData.page <= 1 || salesAuditLoading"
                    @click="loadSalesAudit(salesAuditData.page - 1)"
                  >上一页</button>
                  <button
                    type="button"
                    class="secondary-button"
                    :disabled="salesAuditData.page >= salesAuditPageCount || salesAuditLoading"
                    @click="loadSalesAudit(salesAuditData.page + 1)"
                  >下一页</button>
                </div>
              </div>
            </template>
          </template>
        </section>

        <section class="logistics-command" aria-labelledby="logistics-command-title">
          <div class="logistics-command-heading">
            <div>
              <p class="section-kicker">INVENTORY &amp; LOGISTICS</p>
              <h4 id="logistics-command-title">库存与物流全盘</h4>
            </div>
            <span>海外仓共享库存只计一次；平台库存按合并范围汇总</span>
          </div>
          <div class="logistics-total-grid">
            <article class="overseas-card">
              <span>海外仓库存</span>
              <strong>{{ number(storeData.logistics.overseas_warehouse.stock_total) }}</strong>
              <small>
                {{ storeData.logistics.overseas_warehouse.warehouse_name || "W8 共享海外仓" }}
                · 多店共享只计一次
              </small>
            </article>
            <article>
              <span>平台仓可售</span>
              <strong>{{ number(storeData.logistics.platform_warehouse.platform_available_stock) }}</strong>
              <small>
                {{ offerCoverage(
                  storeData.logistics.platform_warehouse.platform_available_coverage,
                  storeData.logistics.platform_warehouse.offer_count,
                ) }}
              </small>
            </article>
            <article class="transit-card">
              <span>平台在途</span>
              <strong>{{ number(storeData.logistics.platform_warehouse.platform_stock_on_way) }}</strong>
              <small>
                {{ offerCoverage(
                  storeData.logistics.platform_warehouse.platform_stock_on_way_coverage,
                  storeData.logistics.platform_warehouse.offer_count,
                ) }}
              </small>
            </article>
            <article>
              <span>平台收货中</span>
              <strong>{{ number(storeData.logistics.platform_warehouse.platform_stock_in_receiving) }}</strong>
              <small>
                {{ offerCoverage(
                  storeData.logistics.platform_warehouse.platform_stock_in_receiving_coverage,
                  storeData.logistics.platform_warehouse.offer_count,
                ) }}
              </small>
            </article>
          </div>
          <dl class="overseas-breakdown">
            <div>
              <dt>海外仓可用</dt>
              <dd>{{ number(storeData.logistics.overseas_warehouse.usable_stock) }}</dd>
            </div>
            <div>
              <dt>海外仓锁定</dt>
              <dd>{{ number(storeData.logistics.overseas_warehouse.locked_stock) }}</dd>
            </div>
            <div>
              <dt>海外仓已分配出库</dt>
              <dd>{{ number(storeData.logistics.overseas_warehouse.outbound_allocated) }}</dd>
            </div>
            <div>
              <dt>海外仓在途</dt>
              <dd>{{ number(storeData.logistics.overseas_warehouse.transit_stock) }}</dd>
            </div>
          </dl>
          <p class="logistics-definition">
            海外仓“已分配出库”可能包含在锁定或库存总量内，平台在途与平台收货中也是不同阶段；
            页面分开展示，不将这些阶段相加成可能重复的“总库存”。
          </p>
        </section>

        <div v-if="!storeData.stores.length" class="state-card slim">
          当前账号暂无可见且已接入的店铺。
        </div>
        <div v-else class="store-overview-grid">
          <article
            v-for="store in storeData.stores"
            :key="store.store_code"
            class="store-overview-card"
            :class="[
              store.health.state,
              { empty: !store.latest_metric_date },
            ]"
          >
            <header>
              <div>
                <span class="store-code">{{ store.store_code }}</span>
                <h4>{{ store.store_name }}</h4>
              </div>
              <div class="store-card-status">
                <span class="health-badge">{{ store.health.label }}</span>
                <time>{{ store.latest_metric_date || "暂无指标日" }}</time>
              </div>
            </header>
            <div class="store-operators">
              <span>负责运营</span>
              <div v-if="store.operators.length">
                <span
                  v-for="operator in store.operators"
                  :key="operator.user_id"
                  class="operator-chip"
                >
                  {{ operator.display_name }}
                  <small>{{ operatorRoleLabel(operator.role) }}</small>
                </span>
              </div>
              <strong v-else>暂未分配运营账号</strong>
            </div>
            <div class="health-reasons">
              <span
                v-for="reason in store.health.business_reasons"
                :key="`business-${reason}`"
                class="business-risk"
              >{{ reason }}</span>
              <span
                v-for="reason in store.health.data_reasons"
                :key="`data-${reason}`"
                class="data-risk"
              >{{ reason }}</span>
              <span
                v-if="!store.health.business_reasons.length && !store.health.data_reasons.length"
                class="healthy-signal"
              >当前未发现既定风险项</span>
            </div>
            <div class="store-main-kpi">
              <span>最新日下单件数</span>
              <strong>{{ number(store.kpis.latest_ordered_units) }}</strong>
            </div>
            <dl class="store-metrics">
              <div>
                <dt>最新日金额</dt>
                <dd>{{ currency(store.kpis.latest_ordered_revenue) }}</dd>
              </div>
              <div>
                <dt>近 7 日件数</dt>
                <dd>{{ number(store.kpis.seven_day_ordered_units) }}</dd>
              </div>
              <div>
                <dt>在售商品</dt>
                <dd>{{ number(store.kpis.selling_products) }}</dd>
              </div>
              <div>
                <dt>中位转化率</dt>
                <dd>{{ percent(store.kpis.median_conversion) }}</dd>
              </div>
              <div>
                <dt>缺货商品</dt>
                <dd>{{ store.latest_metric_date ? number(store.kpis.stockout_products) : "—" }}</dd>
              </div>
            </dl>
            <dl class="store-inventory-grid">
              <div>
                <dt>平台可售</dt>
                <dd>{{ number(store.inventory.platform_available_stock) }}</dd>
              </div>
              <div>
                <dt>平台在途</dt>
                <dd>{{ number(store.inventory.platform_stock_on_way) }}</dd>
              </div>
              <div>
                <dt>平台收货中</dt>
                <dd>{{ number(store.inventory.platform_stock_in_receiving) }}</dd>
              </div>
            </dl>
            <div
              class="store-traffic-kpi"
              :class="{
                incomplete: !store.latest_traffic_point
                  || (store.latest_traffic_point?.missing_product_count ?? 0) > 0
                  || store.latest_traffic_point?.page_views_30_days_total === null,
              }"
            >
              <span>周期末商品近30天浏览量合计</span>
              <strong>{{ number(trafficValue(store.latest_traffic_point)) }}</strong>
              <small>{{ trafficCoverage(store.latest_traffic_point) }}</small>
            </div>
            <button
              type="button"
              class="store-drilldown"
              @click="emit('selectStore', store.store_code)"
            >进入该店明细</button>
          </article>
        </div>
        <p class="multi-store-definition">
          店铺按“需关注 → 数据待补 → 当前正常”排序；状态只使用异常、缺货和数据完整性信号，
          不代表平台官方评级。“最新日”按每家店自身的最新可用指标日，不强行对齐日期。
          负责运营来自该店获授权、启用且拥有店铺查看权限的非管理员账号。周期末浏览量仅汇总接口已返回的商品
          <code>page_views_30_days</code>；缺失商品不补 0。周期末失败但同日另有成功采集时仅显示参考值，
          它不是正式周期末数据、当天浏览量或独立访客数。
        </p>
      </template>
    </section>

    <div v-if="!allStoresSelected" class="selected-store-heading">
      <div>
        <p class="section-kicker">SELECTED STORE DETAIL</p>
        <h3>{{ currentStoreName }} · 单店明细</h3>
      </div>
      <div class="selected-store-meta">
        <span>负责运营：{{ operatorNames(data?.operators) }}</span>
        <span>最新可用指标日 {{ data?.latest_metric_date || "暂无" }}</span>
      </div>
    </div>

    <div v-if="!allStoresSelected && loading" class="state-card">正在读取经营数据……</div>
    <div v-else-if="!allStoresSelected && error" class="state-card error">{{ error }}</div>
    <template v-else-if="!allStoresSelected && data">
      <section class="erp-kpi-grid">
        <article class="kpi-primary">
          <span>最新日下单件数</span>
          <strong>{{ number(data.kpis.latest_ordered_units) }}</strong>
          <small>最新可用指标日</small>
        </article>
        <article>
          <span>最新日下单金额</span>
          <strong>{{ currency(data.kpis.latest_ordered_revenue) }}</strong>
          <small>订单行金额口径</small>
        </article>
        <article>
          <span>近 7 日下单件数</span>
          <strong>{{ number(data.kpis.seven_day_ordered_units) }}</strong>
          <small>自然日窗口</small>
        </article>
      </section>

      <section class="overview-grid">
        <article class="erp-panel sales-trend-panel">
          <div class="panel-heading">
            <div>
              <p class="section-kicker">SELECTED RANGE ORDERS</p>
              <h3>店铺下单趋势</h3>
            </div>
            <span>真实整数件数</span>
          </div>
          <div v-if="!data.sales_series.length" class="state-card slim">暂无趋势数据</div>
          <div v-else class="bar-chart">
            <div
              v-for="point in data.sales_series"
              :key="point.metric_date"
              class="bar-column"
              :title="`${point.metric_date}：${point.ordered_units ?? 0} 件`"
            >
              <span>{{ point.ordered_units ?? 0 }}</span>
              <i
                :style="{
                  height: `${Math.max(3, ((point.ordered_units ?? 0) / maxUnits) * 100)}%`,
                }"
              ></i>
              <small>{{ day(point.metric_date) }}</small>
            </div>
          </div>
        </article>

        <article class="erp-panel health-panel">
          <div class="panel-heading">
            <div>
              <p class="section-kicker">STORE HEALTH</p>
              <h3>经营健康度</h3>
            </div>
          </div>
          <div class="health-list">
            <div>
              <span>近30天浏览量合计</span>
              <strong>{{ number(data.kpis.page_views_30_days) }}</strong>
            </div>
            <div>
              <span>近30天转化率中位数</span>
              <strong>{{ percent(data.kpis.median_conversion) }}</strong>
            </div>
            <div>
              <span>今日售出商品</span>
              <strong>{{ data.kpis.selling_products }}</strong>
            </div>
            <div>
              <span>缺货商品</span>
              <strong>{{ data.kpis.stockout_products }}</strong>
            </div>
          </div>
        </article>
      </section>

      <section class="erp-panel traffic-trend-panel">
        <div class="panel-heading traffic-heading">
          <div>
            <p class="section-kicker">PERIOD-END TRAFFIC</p>
            <h3>店铺商品近30天浏览量汇总趋势</h3>
          </div>
          <span>每日次日 09:00 周期末刷新成功后更新</span>
        </div>
        <p class="traffic-definition">
          每个点汇总当日接口已返回的商品 <code>page_views_30_days</code>，表示滚动近30天浏览量。
          缺失商品不补 0，并单独标出覆盖数量；09:00 周期末失败时，以同一北京时间自然日最近一次成功采集画橙色虚线参考，正式周期末数据仍保留失败事实。这不是当天浏览量，也不是独立访客数。
        </p>
        <div v-if="!data.traffic_series.length" class="state-card slim">
          暂无周期末流量快照；下次 09:00 周期末刷新成功后开始记录。
        </div>
        <template v-else>
          <div
            class="traffic-latest"
            :class="{ incomplete: (latestTrafficPoint?.missing_product_count ?? 0) > 0 || latestTrafficPoint?.page_views_30_days_total === null }"
          >
            <strong>{{ number(latestTrafficValue) }}</strong>
            <span v-if="latestTrafficPoint?.page_views_30_days_total !== null">
              {{ latestTrafficPoint?.business_date }} · 已返回
              {{ (latestTrafficPoint?.product_count ?? 0) - (latestTrafficPoint?.missing_product_count ?? 0) }}/{{ latestTrafficPoint?.product_count }} 个商品
              <template v-if="latestTrafficPoint?.missing_product_count">
                · 缺失 {{ latestTrafficPoint.missing_product_count }} 个（未补 0）
              </template>
            </span>
            <span v-else-if="latestTrafficPoint?.reference">
              {{ latestTrafficPoint.business_date }} 09:00 周期末刷新失败 · 橙色虚线参考
              {{ trafficSlotLabel(latestTrafficPoint.reference.source_slot) }}
              {{ formatChinaDateTime(latestTrafficPoint.reference.captured_at) }} · 已返回
              {{ latestTrafficPoint.reference.product_count - latestTrafficPoint.reference.missing_product_count }}/{{ latestTrafficPoint.reference.product_count }} 个商品
              <template v-if="latestTrafficPoint.reference.missing_product_count">
                · 缺失 {{ latestTrafficPoint.reference.missing_product_count }} 个（未补 0）
              </template>
            </span>
            <span v-else-if="latestTrafficPoint?.status === 'failed'">
              {{ latestTrafficPoint?.business_date }} 周期末刷新失败，且没有同日成功采集可供参考
            </span>
            <span v-else>
              {{ latestTrafficPoint?.business_date }} 有 {{ latestTrafficPoint?.missing_product_count }} 个商品缺失，未展示部分合计
            </span>
          </div>
          <div class="traffic-chart-scroll">
            <div class="trend-chart-stage">
              <svg
                class="traffic-chart"
                :viewBox="`0 0 ${TRAFFIC_WIDTH} ${TRAFFIC_HEIGHT}`"
                role="img"
                aria-labelledby="traffic-chart-title traffic-chart-description"
                @pointermove="handleTrafficPointer"
                @pointerleave="clearTrafficPointer"
              >
              <title id="traffic-chart-title">店铺商品近30天浏览量每日周期末汇总折线图</title>
              <desc id="traffic-chart-description">绿色实线为成功的周期末汇总；周期末失败但同日另有成功采集时，以橙色虚线展示参考并保留正式失败事实；没有同日参考时保留断点。</desc>
              <g class="traffic-grid">
                <template v-for="tick in trafficChart.ticks" :key="tick.y">
                  <line :x1="TRAFFIC_LEFT" :x2="TRAFFIC_WIDTH - TRAFFIC_RIGHT" :y1="tick.y" :y2="tick.y" />
                  <text :x="TRAFFIC_LEFT - 10" :y="tick.y + 4">{{ number(Math.round(tick.value)) }}</text>
                </template>
              </g>
              <line
                v-if="activeTrafficDot"
                class="trend-crosshair"
                :x1="activeTrafficDot.x"
                :x2="activeTrafficDot.x"
                :y1="TRAFFIC_TOP"
                :y2="TRAFFIC_HEIGHT - TRAFFIC_BOTTOM"
              />
              <polyline
                v-for="(segment, index) in trafficChart.officialSegments"
                :key="`official-${index}`"
                class="traffic-line"
                :points="segment"
              />
              <polyline
                v-for="(segment, index) in trafficChart.referenceSegments"
                :key="`reference-${index}`"
                class="traffic-line reference"
                :points="segment"
              />
              <polyline
                v-for="(segment, index) in trafficChart.partialSegments"
                :key="`partial-${index}`"
                class="traffic-line partial-coverage"
                :points="segment"
              />
              <polyline
                v-for="(segment, index) in trafficChart.missingBridgeSegments"
                :key="`traffic-missing-bridge-${index}`"
                class="traffic-line missing-bridge"
                :points="segment"
              />
              <g
                v-for="(dot, index) in trafficChart.dots"
                :key="dot.point.business_date"
                class="trend-data-point"
                :class="{ active: index === activeTrafficIndex }"
                tabindex="0"
                role="button"
                :aria-label="trafficPointTitle(dot.point)"
                @pointerenter="setTrafficPoint(index, $event)"
                @focus="setTrafficPoint(index, $event)"
                @click="setTrafficPoint(index, $event)"
                @keydown.left.prevent="stepTrafficPoint(index, -1, $event)"
                @keydown.right.prevent="stepTrafficPoint(index, 1, $event)"
              >
                <circle class="trend-point-hit" :cx="dot.x" :cy="dot.y" r="14" />
                <circle
                  v-if="index === activeTrafficIndex"
                  class="trend-point-halo"
                  :cx="dot.x"
                  :cy="dot.y"
                  r="9"
                />
                <circle
                  v-if="dot.value !== null"
                  :class="[
                    'traffic-dot',
                    {
                      missing: dot.value === null,
                      reference: dot.isReference,
                      partial: dot.value !== null && !dot.isReference && dot.missingProductCount > 0,
                    },
                  ]"
                  :cx="dot.x"
                  :cy="dot.y"
                  :r="dot.isReference ? 5 : 4"
                />
                <path
                  v-else
                  class="trend-missing-mark"
                  :d="`M ${dot.x - 5} ${dot.y - 5} L ${dot.x + 5} ${dot.y + 5} M ${dot.x + 5} ${dot.y - 5} L ${dot.x - 5} ${dot.y + 5}`"
                />
              </g>
              <g class="traffic-axis-labels">
                <text
                  v-for="label in trafficChart.labels"
                  :key="label.point.business_date"
                  :x="label.x"
                  :y="TRAFFIC_HEIGHT - 12"
                >{{ day(label.point.business_date) }}</text>
              </g>
              </svg>
              <div
                v-if="activeTrafficDot && trafficTooltipPosition"
                class="trend-hover-card"
                :class="floatingChartTooltipClasses(trafficTooltipPosition)"
                :style="floatingChartTooltipStyle(trafficTooltipPosition, 310)"
                role="status"
                aria-live="polite"
              >
                <div class="trend-hover-heading">
                  <span>业务日期</span>
                  <strong>{{ activeTrafficDot.point.business_date }}</strong>
                </div>
                <dl>
                  <div>
                    <dt>近30天浏览量合计</dt>
                    <dd>{{ number(activeTrafficDot.value) }}</dd>
                  </div>
                  <div>
                    <dt>商品覆盖</dt>
                    <dd>
                      {{ activeTrafficDot.isReference
                        ? `${(activeTrafficDot.point.reference?.product_count ?? 0) - activeTrafficDot.missingProductCount}/${activeTrafficDot.point.reference?.product_count ?? 0}`
                        : `${activeTrafficDot.point.product_count - activeTrafficDot.missingProductCount}/${activeTrafficDot.point.product_count}` }}
                    </dd>
                  </div>
                  <div>
                    <dt>数据来源</dt>
                    <dd :class="{ warning: activeTrafficDot.isReference || activeTrafficDot.value === null }">
                      <template v-if="activeTrafficDot.isReference">
                        周期末失败 · {{ trafficSlotLabel(activeTrafficDot.point.reference?.source_slot ?? "") }}参考
                      </template>
                      <template v-else-if="activeTrafficDot.value === null">周期末刷新失败，无同日参考</template>
                      <template v-else>09:00 周期末正式采集</template>
                    </dd>
                  </div>
                  <div v-if="activeTrafficDot.isReference && activeTrafficDot.point.reference">
                    <dt>采集时间</dt>
                    <dd>{{ formatChinaDateTime(activeTrafficDot.point.reference.captured_at) }}</dd>
                  </div>
                  <div v-else-if="activeTrafficDot.point.captured_at">
                    <dt>采集时间</dt>
                    <dd>{{ formatChinaDateTime(activeTrafficDot.point.captured_at) }}</dd>
                  </div>
                  <div v-if="activeTrafficDot.missingProductCount">
                    <dt>缺失商品</dt>
                    <dd class="warning">{{ activeTrafficDot.missingProductCount }} 个，未补 0</dd>
                  </div>
                </dl>
              </div>
            </div>
          </div>
          <div class="traffic-legend">
            <span><i></i>完整商品覆盖</span>
            <span><i class="partial"></i>部分商品缺失（未补 0）</span>
            <span><i class="partial-line"></i>部分覆盖区间（虚线）</span>
            <span><i class="reference-line"></i>同日最近成功采集参考（虚线）</span>
            <span><i class="missing"></i>整次刷新失败</span>
            <span><i class="missing-bridge"></i>缺失区间桥接，仅连接两端真实值</span>
          </div>
        </template>
      </section>

      <section class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">TOP PRODUCTS</p>
            <h3>最新日商品表现</h3>
          </div>
          <span>按下单件数排序</span>
        </div>
        <div class="erp-table-wrap">
          <table class="erp-table">
            <thead>
              <tr>
                <th>商品</th>
                <th>下单件数</th>
                <th>下单金额</th>
                <th>近30天浏览量</th>
                <th>转化率</th>
                <th>库存</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in data.top_products" :key="item.offer_id">
                <td>
                  <strong>{{ item.title || item.sku || item.offer_id }}</strong>
                  <small>{{ item.sku || "无库存编码" }}</small>
                </td>
                <td>{{ number(item.ordered_units) }}</td>
                <td>{{ currency(item.ordered_revenue) }}</td>
                <td>{{ number(item.page_views_30_days) }}</td>
                <td>{{ percent(item.conversion_percentage_30_days) }}</td>
                <td>{{ number(item.total_stock) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.multi-store-panel {
  overflow: hidden;
  border-color: rgba(55, 93, 74, 0.2);
  background:
    radial-gradient(circle at 92% -15%, rgba(184, 217, 109, 0.22), transparent 35%),
    rgba(249, 251, 248, 0.96);
}

.multi-store-heading {
  align-items: flex-start;
}

.multi-store-heading > span {
  padding: 6px 10px;
  border: 1px solid rgba(55, 93, 74, 0.18);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.72);
  color: var(--green);
}

.command-health {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 12px;
  padding: 17px 18px;
  border: 1px solid rgba(55, 93, 74, 0.18);
  border-left: 5px solid var(--green);
  border-radius: 13px;
  background: rgba(255, 255, 255, 0.78);
}

.command-health.attention {
  border-left-color: var(--erp-red);
  background: rgba(255, 247, 244, 0.9);
}

.command-health.data-gap {
  border-left-color: #c88224;
  background: rgba(255, 250, 238, 0.9);
}

.command-health > div > span,
.command-health > div > small {
  display: block;
  color: var(--muted);
  font-size: 0.65rem;
}

.command-health > div > strong {
  display: block;
  margin: 5px 0 4px;
  color: var(--green);
  font-size: clamp(1rem, 2.2vw, 1.38rem);
}

.command-health.attention > div > strong {
  color: var(--erp-red);
}

.command-health dl {
  display: grid;
  grid-template-columns: repeat(3, minmax(70px, 1fr));
  gap: 8px;
  margin: 0;
}

.command-health dl div {
  padding: 8px 10px;
  border-radius: 9px;
  background: rgba(238, 243, 238, 0.9);
  text-align: center;
}

.command-health dt {
  color: var(--muted);
  font-size: 0.58rem;
}

.command-health dd {
  margin: 3px 0 0;
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 1rem;
  font-weight: 800;
}

.multi-total-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.multi-total-grid article {
  min-width: 0;
  padding: 16px;
  border: 1px solid rgba(24, 37, 31, 0.08);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.72);
}

.multi-total-grid span,
.multi-total-grid small {
  display: block;
  color: var(--muted);
  font-size: 0.65rem;
}

.multi-total-grid strong {
  display: block;
  margin: 8px 0 5px;
  overflow: hidden;
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: clamp(1.08rem, 2vw, 1.55rem);
  letter-spacing: -0.05em;
  text-overflow: ellipsis;
}

.multi-total-grid .multi-total-primary {
  border-color: var(--green);
  background: var(--green);
}

.multi-total-grid .multi-total-primary span,
.multi-total-grid .multi-total-primary small {
  color: rgba(255, 255, 255, 0.65);
}

.multi-total-grid .multi-total-primary strong {
  color: var(--erp-accent);
}

.multi-total-grid .multi-total-alert {
  border-top: 3px solid var(--erp-red);
}

.revenue-command {
  margin-bottom: 14px;
  padding: 16px;
  border: 1px solid rgba(55, 93, 74, 0.18);
  border-radius: 13px;
  background: rgba(255, 255, 255, 0.78);
}

.revenue-heading {
  margin-bottom: 8px;
}

.revenue-definition {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 0.7rem;
  line-height: 1.7;
}

.sales-reconciliation-alert {
  display: grid;
  gap: 4px;
  margin: 0 0 10px;
  padding: 10px 12px;
  border: 1px solid rgba(54, 93, 74, 0.2);
  border-left-width: 4px;
  border-radius: 9px;
  font-size: 0.68rem;
  line-height: 1.55;
}

.sales-reconciliation-alert strong {
  font-size: 0.74rem;
}

.sales-reconciliation-alert.pending {
  border-color: rgba(184, 111, 23, 0.4);
  border-left-color: #b86f17;
  background: #fff6e7;
  color: #75470f;
}

.sales-reconciliation-alert.recovered {
  border-left-color: var(--green);
  background: #eef7f1;
  color: #28543e;
}

.sales-reconciliation-alert.revised {
  border-left-color: #3a6f9d;
  background: #eef6fc;
  color: #285777;
}

.revenue-latest {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(238, 243, 238, 0.8);
}

.revenue-latest strong {
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: clamp(1.35rem, 3vw, 2rem);
}

.revenue-latest span {
  color: var(--muted);
  font-size: 0.68rem;
  text-align: right;
}

.revenue-latest.incomplete strong,
.revenue-latest.incomplete span {
  color: #9a6420;
}

.revenue-latest.pending {
  background: #fff4df;
}

.revenue-latest.pending strong,
.revenue-latest.pending span {
  color: #8a5517;
}

.revenue-latest.revised {
  background: #edf5fb;
}

.revenue-latest.revised strong,
.revenue-latest.revised span {
  color: #285f88;
}

.revenue-line {
  fill: none;
  stroke: var(--green);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 4;
  filter: drop-shadow(0 2px 3px rgba(31, 86, 62, 0.18));
}

.revenue-line.reconciliation-pending {
  stroke: #b86f17;
  stroke-dasharray: 8 6;
  filter: none;
}

.revenue-line.revised {
  stroke: #3a78a8;
  filter: drop-shadow(0 2px 3px rgba(58, 120, 168, 0.18));
}

.revenue-line.missing-bridge {
  stroke: #9a6a2d;
  stroke-dasharray: 8 7;
  stroke-width: 3;
  filter: none;
}

.revenue-dot {
  fill: var(--erp-accent);
  stroke: var(--green);
  stroke-width: 2;
}

.revenue-dot.pending {
  fill: #fff2d8;
  stroke: #b86f17;
  stroke-width: 3;
}

.revenue-dot.revised {
  fill: #dceef9;
  stroke: #3a78a8;
  stroke-width: 3;
}

.revenue-dot.missing {
  fill: #fff3d8;
  stroke: #c88224;
}

.revenue-dot:focus {
  outline: none;
  stroke: #162d24;
  stroke-width: 4;
}

.sales-audit-panel {
  margin-bottom: 14px;
  padding: 16px;
  border: 1px solid rgba(58, 111, 157, 0.2);
  border-radius: 13px;
  background: rgba(247, 251, 254, 0.86);
}

.sales-audit-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.sales-audit-heading h4 {
  margin: 2px 0 4px;
  font-size: 0.98rem;
}

.sales-audit-heading span {
  color: var(--muted);
  font-size: 0.68rem;
  line-height: 1.5;
}

.sales-audit-filter {
  display: grid;
  grid-template-columns: repeat(2, minmax(160px, 220px)) auto;
  gap: 10px;
  align-items: end;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid rgba(58, 111, 157, 0.14);
}

.sales-audit-filter label {
  display: grid;
  gap: 5px;
  color: var(--muted);
  font-size: 0.64rem;
}

.sales-audit-filter input {
  min-width: 0;
  padding: 8px 9px;
  border: 1px solid rgba(42, 70, 57, 0.22);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
  font: inherit;
}

.sales-audit-table-wrap {
  margin-top: 12px;
}

.sales-audit-table {
  min-width: 1120px;
}

.sales-audit-table td {
  max-width: 260px;
  vertical-align: top;
}

.sales-audit-table td strong,
.sales-audit-table td span,
.sales-audit-table td code {
  display: block;
}

.sales-audit-table td span {
  margin-top: 3px;
  color: var(--muted);
  font-size: 0.62rem;
  line-height: 1.45;
}

.sales-audit-table td span.negative {
  color: var(--erp-red);
}

.sales-audit-table td code {
  margin-top: 5px;
  color: #285f88;
  font-size: 0.6rem;
}

.sales-audit-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  color: var(--muted);
  font-size: 0.65rem;
}

.sales-audit-pagination > div {
  display: flex;
  gap: 8px;
}

.logistics-command {
  margin-bottom: 14px;
  padding: 16px;
  border: 1px solid rgba(24, 37, 31, 0.08);
  border-radius: 13px;
  background: rgba(238, 243, 238, 0.72);
}

.logistics-command-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 12px;
}

.logistics-command-heading h4 {
  margin: 2px 0 0;
  font-size: 0.98rem;
}

.logistics-command-heading > span {
  max-width: 360px;
  color: var(--muted);
  font-size: 0.64rem;
  line-height: 1.55;
  text-align: right;
}

.logistics-total-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 9px;
}

.logistics-total-grid article {
  min-width: 0;
  padding: 13px;
  border: 1px solid rgba(24, 37, 31, 0.08);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.82);
}

.logistics-total-grid article.overseas-card {
  border-color: rgba(55, 93, 74, 0.3);
}

.logistics-total-grid article.transit-card {
  border-color: rgba(200, 130, 36, 0.35);
}

.logistics-total-grid span,
.logistics-total-grid small {
  display: block;
  color: var(--muted);
  font-size: 0.61rem;
}

.logistics-total-grid strong {
  display: block;
  margin: 6px 0 4px;
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: clamp(1.08rem, 2vw, 1.48rem);
}

.overseas-breakdown {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0 0;
}

.overseas-breakdown div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.58);
}

.overseas-breakdown dt {
  color: var(--muted);
  font-size: 0.58rem;
}

.overseas-breakdown dd {
  margin: 0;
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 800;
}

.logistics-definition {
  margin: 9px 0 0;
  color: var(--muted);
  font-size: 0.61rem;
  line-height: 1.55;
}

.store-overview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.store-overview-card {
  min-width: 0;
  padding: 17px;
  border: 1px solid rgba(24, 37, 31, 0.09);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.8);
  box-shadow: 0 8px 22px rgba(32, 54, 43, 0.045);
}

.store-overview-card.attention {
  border-top: 4px solid var(--erp-red);
}

.store-overview-card.data_gap {
  border-top: 4px solid #c88224;
}

.store-overview-card.healthy {
  border-top: 4px solid var(--green);
}

.store-overview-card.empty {
  border-style: dashed;
}

.store-overview-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.store-overview-card h4 {
  margin: 4px 0 0;
  font-size: 0.92rem;
  line-height: 1.3;
}

.store-code,
.store-overview-card time {
  color: var(--muted);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 0.61rem;
}

.store-overview-card time {
  flex: 0 0 auto;
  padding: 4px 6px;
  border-radius: 6px;
  background: #eef3ee;
}

.store-card-status {
  display: flex;
  align-items: flex-end;
  flex-direction: column;
  gap: 5px;
}

.health-badge {
  padding: 4px 7px;
  border-radius: 999px;
  background: rgba(55, 93, 74, 0.1);
  color: var(--green);
  font-size: 0.58rem;
  font-weight: 800;
}

.store-overview-card.attention .health-badge {
  background: rgba(180, 64, 52, 0.1);
  color: var(--erp-red);
}

.store-overview-card.data_gap .health-badge {
  background: rgba(200, 130, 36, 0.12);
  color: #9a6420;
}

.store-operators {
  margin-top: 12px;
  padding: 9px;
  border-radius: 9px;
  background: #f4f7f3;
}

.store-operators > span,
.store-operators > strong {
  display: block;
  color: var(--muted);
  font-size: 0.59rem;
}

.store-operators > div {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
}

.operator-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border: 1px solid rgba(55, 93, 74, 0.16);
  border-radius: 999px;
  background: #fff;
  color: var(--green);
  font-size: 0.62rem;
  font-weight: 750;
}

.operator-chip small {
  color: var(--muted);
  font-size: 0.5rem;
  font-weight: 500;
}

.health-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 9px;
}

.health-reasons span {
  padding: 4px 6px;
  border-radius: 6px;
  font-size: 0.56rem;
}

.health-reasons .business-risk {
  background: rgba(180, 64, 52, 0.09);
  color: var(--erp-red);
}

.health-reasons .data-risk {
  background: rgba(200, 130, 36, 0.1);
  color: #9a6420;
}

.health-reasons .healthy-signal {
  background: rgba(55, 93, 74, 0.09);
  color: var(--green);
}

.store-main-kpi {
  margin: 18px 0 15px;
}

.store-main-kpi span,
.store-traffic-kpi span,
.store-traffic-kpi small {
  display: block;
  color: var(--muted);
  font-size: 0.64rem;
}

.store-main-kpi strong {
  display: block;
  margin-top: 4px;
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 1.75rem;
  letter-spacing: -0.06em;
}

.store-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;
  margin: 0;
}

.store-metrics div {
  min-width: 0;
  padding: 9px;
  border-radius: 9px;
  background: #f4f7f3;
}

.store-metrics dt {
  color: var(--muted);
  font-size: 0.6rem;
}

.store-metrics dd {
  margin: 4px 0 0;
  overflow: hidden;
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 0.75rem;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.store-inventory-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  margin: 10px 0 0;
  padding: 9px;
  border: 1px solid rgba(55, 93, 74, 0.11);
  border-radius: 9px;
  background: rgba(235, 242, 235, 0.68);
}

.store-inventory-grid div {
  min-width: 0;
}

.store-inventory-grid dt {
  color: var(--muted);
  font-size: 0.55rem;
}

.store-inventory-grid dd {
  margin: 4px 0 0;
  overflow: hidden;
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 0.72rem;
  font-weight: 800;
  text-overflow: ellipsis;
}

.store-traffic-kpi {
  margin-top: 10px;
  padding-top: 11px;
  border-top: 1px solid rgba(24, 37, 31, 0.08);
}

.store-traffic-kpi strong {
  display: block;
  margin: 5px 0 3px;
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 1rem;
}

.store-traffic-kpi.incomplete strong,
.store-traffic-kpi.incomplete small {
  color: #9a6420;
}

.store-drilldown {
  width: 100%;
  margin-top: 12px;
  padding: 8px 10px;
  border: 1px solid rgba(55, 93, 74, 0.22);
  border-radius: 8px;
  background: transparent;
  color: var(--green);
  cursor: pointer;
  font-size: 0.64rem;
  font-weight: 800;
}

.store-drilldown:hover,
.store-drilldown:focus-visible {
  border-color: var(--green);
  background: rgba(55, 93, 74, 0.07);
  outline: none;
}

.multi-store-definition {
  margin: 14px 0 0;
  color: var(--muted);
  font-size: 0.66rem;
  line-height: 1.65;
}

.multi-store-definition code {
  color: var(--green);
  font-size: 0.63rem;
}

.selected-store-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-top: 8px;
  padding-top: 19px;
  border-top: 1px solid rgba(24, 37, 31, 0.1);
}

.selected-store-heading h3 {
  margin: 2px 0 0;
  font-size: 1.15rem;
}

.selected-store-meta {
  display: flex;
  align-items: flex-end;
  flex-direction: column;
  gap: 4px;
}

.selected-store-meta span {
  color: var(--muted);
  font-size: 0.68rem;
}

.traffic-trend-panel {
  overflow: hidden;
}

.traffic-heading {
  align-items: flex-start;
  gap: 18px;
}

.traffic-heading > span {
  max-width: 260px;
  text-align: right;
}

.traffic-definition {
  max-width: 900px;
  margin: -4px 0 14px;
  color: var(--muted);
  font-size: 0.72rem;
  line-height: 1.7;
}

.traffic-definition code {
  color: var(--green);
  font-size: 0.68rem;
}

.traffic-latest {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 6px;
}

.traffic-latest strong {
  color: var(--green);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: clamp(1.4rem, 3vw, 2.2rem);
}

.traffic-latest span {
  color: var(--muted);
  font-size: 0.68rem;
}

.traffic-latest.incomplete strong,
.traffic-latest.incomplete span {
  color: #9a6420;
}

.traffic-chart-scroll {
  min-width: 0;
  overflow: hidden;
  border: 1px solid rgba(62, 85, 72, 0.13);
  border-radius: 12px;
  background: linear-gradient(180deg, #fff 0%, #f8fbf8 100%);
}

.trend-chart-stage {
  position: relative;
  width: 100%;
}

.traffic-chart {
  display: block;
  width: 100%;
  height: auto;
}

.traffic-grid line {
  stroke: #d4dfd7;
  stroke-width: 1;
  stroke-dasharray: 4 5;
}

.traffic-grid text {
  fill: #53665c;
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 11px;
  font-weight: 700;
  text-anchor: end;
}

.traffic-line {
  fill: none;
  stroke: var(--green);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 4;
  filter: drop-shadow(0 2px 3px rgba(31, 86, 62, 0.18));
}

.traffic-line.reference {
  stroke: #c88224;
  stroke-dasharray: 7 6;
  stroke-width: 3.5;
}

.traffic-line.partial-coverage {
  stroke: #8b7527;
  stroke-dasharray: 8 6;
  stroke-width: 3.5;
  filter: none;
}

.traffic-line.missing-bridge {
  stroke: #7e6952;
  stroke-dasharray: 5 7;
  stroke-width: 3;
  filter: none;
}

.trend-data-point {
  cursor: crosshair;
  outline: none;
}

.trend-point-hit {
  fill: transparent;
  stroke: transparent;
}

.trend-point-halo {
  fill: rgba(255, 255, 255, 0.92);
  stroke: rgba(22, 45, 36, 0.28);
  stroke-width: 5;
}

.trend-missing-mark {
  fill: none;
  stroke: #b86f17;
  stroke-linecap: round;
  stroke-width: 2.8;
  pointer-events: none;
}

.trend-data-point:focus-visible .trend-point-halo,
.trend-data-point:hover .trend-point-halo {
  stroke: rgba(22, 45, 36, 0.42);
}

.trend-crosshair {
  stroke: #1d3e30;
  stroke-width: 1.5;
  stroke-dasharray: 5 5;
  opacity: 0.72;
  pointer-events: none;
}

.traffic-dot {
  fill: var(--erp-accent);
  stroke: var(--green);
  stroke-width: 2;
}

.traffic-dot.missing {
  fill: #fff3d8;
  stroke: #c88224;
}

.traffic-dot.partial {
  fill: #fff3d8;
  stroke: #c88224;
}

.traffic-dot.reference {
  fill: #fff;
  stroke: #c88224;
  stroke-width: 3;
}

.traffic-axis-labels text {
  fill: #53665c;
  font-size: 11px;
  font-weight: 700;
  text-anchor: middle;
}

.trend-hover-card {
  position: fixed;
  z-index: 1300;
  width: min(310px, calc(100vw - 24px));
  padding: 12px 14px;
  border: 1px solid rgba(24, 45, 36, 0.18);
  border-radius: 12px;
  background: rgba(19, 40, 31, 0.95);
  box-shadow: 0 12px 28px rgba(18, 38, 30, 0.22);
  color: #fff;
  pointer-events: none;
  backdrop-filter: blur(8px);
  transform: translateY(14px);
}

.trend-hover-card.tooltip-align-above {
  transform: translateY(calc(-100% - 14px));
}

.trend-hover-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.16);
}

.trend-hover-heading span,
.trend-hover-card dt {
  color: rgba(255, 255, 255, 0.68);
  font-size: 0.62rem;
}

.trend-hover-heading strong {
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 0.78rem;
}

.trend-hover-card dl {
  display: grid;
  gap: 6px;
  margin: 9px 0 0;
}

.trend-hover-card dl > div {
  display: grid;
  grid-template-columns: minmax(90px, 0.8fr) minmax(0, 1.4fr);
  gap: 10px;
  align-items: baseline;
}

.trend-hover-card dd {
  margin: 0;
  font-size: 0.68rem;
  font-weight: 750;
  line-height: 1.45;
  text-align: right;
}

.trend-hover-card dd.warning {
  color: #ffd28e;
}

.trend-hover-card dd.revised {
  color: #a9ddff;
}

.traffic-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  color: var(--muted);
  font-size: 0.65rem;
}

.traffic-legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.traffic-legend i {
  width: 9px;
  height: 9px;
  border: 2px solid var(--green);
  border-radius: 999px;
  background: var(--erp-accent);
}

.traffic-legend i.missing {
  border-color: #c88224;
  background: #fff3d8;
}

.traffic-legend i.reconciliation-pending {
  width: 22px;
  height: 0;
  border: 0;
  border-top: 2px dashed #b86f17;
  border-radius: 0;
  background: transparent;
}

.traffic-legend i.revised {
  border-color: #3a78a8;
  background: #dceef9;
}

.traffic-legend i.partial {
  border-color: #c88224;
  background: var(--erp-accent);
}

.traffic-legend i.reference-line {
  width: 22px;
  height: 0;
  border: 0;
  border-top: 2px dashed #c88224;
  border-radius: 0;
  background: transparent;
}

.traffic-legend i.partial-line,
.traffic-legend i.missing-bridge {
  width: 22px;
  height: 0;
  border: 0;
  border-top: 2px dashed #8b7527;
  border-radius: 0;
  background: transparent;
}

.traffic-legend i.missing-bridge {
  border-top-color: #7e6952;
}

@media (max-width: 760px) {
  .multi-total-grid,
  .logistics-total-grid,
  .store-overview-grid {
    grid-template-columns: 1fr;
  }

  .command-health,
  .logistics-command-heading,
  .sales-audit-heading,
  .sales-audit-pagination,
  .traffic-heading,
  .traffic-latest,
  .selected-store-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .sales-audit-filter {
    grid-template-columns: 1fr;
  }

  .command-health dl {
    width: 100%;
  }

  .overseas-breakdown {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .logistics-command-heading > span,
  .traffic-heading > span {
    text-align: left;
  }

  .selected-store-meta {
    align-items: flex-start;
  }
}

@media (min-width: 761px) and (max-width: 1100px) {
  .multi-total-grid,
  .logistics-total-grid,
  .store-overview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .overseas-breakdown {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
