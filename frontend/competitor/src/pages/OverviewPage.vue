<script setup lang="ts">
import HomeDashboard from "../components/HomeDashboard.vue";
import { useResponsiveChart } from "../useResponsiveChart";
import { useStablePageHeight } from "../useStablePageHeight";
import { cachedNumberFormatter } from "../numberFormatters";
import { useLiveUpdates } from "../liveUpdates";
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
import {
  projectRevenueMonthTotal,
  revenuePeriodLabels,
  summarizeRevenuePeriod,
  type RevenueMonthProjection,
  type RevenuePeriodSummary,
} from "../overviewRevenue";
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
useLiveUpdates("overview", async () => {
  await load(true);
  if (salesAuditOpen.value) await loadSalesAudit(salesAuditData.value?.page ?? 1, true);
  return !error.value && !storeError.value && (!salesAuditOpen.value || !salesAuditError.value);
}, {
  busy: () => loading.value || storeLoading.value || salesAuditLoading.value,
  editing: () => false,
});

const data = ref<SummaryPayload | null>(null);
const storeData = ref<StoreOverviewPayload | null>(null);
const loading = ref(true);
const storeLoading = ref(true);
const homeLoading = ref(false);
const { pageElement, pageStyle } = useStablePageHeight(
  () => [props.rangeStart, props.rangeEnd],
  () => loading.value || storeLoading.value || homeLoading.value,
);
const error = ref("");
const storeError = ref("");
const activeTrafficIndex = ref<number | null>(null);
const trafficTooltipPosition = ref<FloatingChartTooltipPosition | null>(null);
const salesAuditOpen = ref(false);
const salesAuditLoading = ref(false);
const salesAuditError = ref("");
const salesAuditData = ref<SalesRevenueRevisionPayload | null>(null);
const salesAuditStart = ref(props.rangeStart);
const salesAuditEnd = ref(props.rangeEnd);
let loadRequestId = 0;
let salesAuditRequestId = 0;

const { chartElement: trafficChartElement, chartWidth: TRAFFIC_WIDTH } = useResponsiveChart(760);
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
  const plotWidth = TRAFFIC_WIDTH.value - TRAFFIC_LEFT - TRAFFIC_RIGHT;
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
  const labelEvery = Math.max(1, Math.ceil(source.length / (TRAFFIC_WIDTH.value < 500 ? 3 : 6)));
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
  sevenDayUnits: aggregateKpi("seven_day_ordered_units"),
  stockouts: aggregateKpi("stockout_products", true),
}));

const periodRevenueLabels = computed(() =>
  revenuePeriodLabels(props.rangeStart, props.rangeEnd),
);
const multiStorePeriodRevenue = computed(() =>
  summarizeRevenuePeriod(
    (storeData.value?.sales_revenue_series ?? []).map((point) => ({
      amount: point.total_ordered_revenue,
      partial: point.missing_store_count > 0,
      pending: point.data_status === "pending",
    })),
  ),
);
const singleStorePeriodRevenue = computed(() =>
  summarizeRevenuePeriod(
    (data.value?.sales_series ?? []).map((point) => ({
      amount: point.ordered_revenue,
    })),
  ),
);
const multiStoreRevenueProjection = computed(() =>
  projectRevenueMonthTotal(
    multiStorePeriodRevenue.value.dailyAverage,
    props.rangeStart,
    props.rangeEnd,
  ),
);
const singleStoreRevenueProjection = computed(() =>
  projectRevenueMonthTotal(
    singleStorePeriodRevenue.value.dailyAverage,
    props.rangeStart,
    props.rangeEnd,
  ),
);

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

async function load(background: unknown = false) {
  const preserve = background === true;
  const requestId = ++loadRequestId;
  loading.value = !props.allStoresSelected && !preserve;
  storeLoading.value = props.allStoresSelected && !preserve;
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
      if (!preserve) storeData.value = null;
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
    if (!preserve) data.value = null;
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

function periodRevenueCoverage(summary: RevenuePeriodSummary) {
  if (!summary.knownDayCount) return "暂无已返回销售额业务日";
  const details = [`${summary.knownDayCount} 个已有销售额业务日`];
  if (summary.partialDayCount) details.push(`${summary.partialDayCount} 日为部分店铺合计`);
  if (summary.pendingDayCount) details.push(`${summary.pendingDayCount} 日待核验`);
  if (summary.missingDayCount) details.push(`${summary.missingDayCount} 日无金额`);
  return details.join(" · ");
}

function periodRevenueAverageBasis(summary: RevenuePeriodSummary) {
  return summary.knownDayCount
    ? `总额 ÷ ${summary.knownDayCount} 个已有销售额业务日；缺失不补 0`
    : "暂无可计算日均的销售额数据";
}

function periodRevenueProjectionBasis(
  projection: RevenueMonthProjection,
  summary: RevenuePeriodSummary,
) {
  if (projection.monthDayCount === null) return "仅在单个自然月视口计算";
  if (projection.projectedTotal === null) return "暂无可计算预测的销售额数据";
  const details = [`月内日均 × ${projection.monthDayCount} 天`];
  if (summary.partialDayCount) details.push("含部分店铺合计");
  if (summary.pendingDayCount) details.push("含待核验日");
  if (summary.missingDayCount) details.push("缺失不补 0");
  return details.join(" · ");
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
    : cachedNumberFormatter("zh-CN").format(value);
}

function currency(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : cachedNumberFormatter("en-ZA", {
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
  return cachedNumberFormatter("en-ZA", {
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

async function loadSalesAudit(page = 1, preserve = false) {
  const requestId = ++salesAuditRequestId;
  salesAuditLoading.value = !preserve;
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

function handleTrafficPointer(event: PointerEvent) {
  const index = nearestChartPointIndex(event, TRAFFIC_WIDTH.value, trafficChart.value.dots);
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

function revenuePendingStatus(point: MultiStoreRevenuePoint) {
  const details: string[] = [];
  if (point.pending_reconciliation_store_count) {
    details.push(`${point.pending_reconciliation_store_count} 家对应周期末业务日待失败后核验`);
  }
  if (point.unverified_source_store_count) {
    details.push(`${point.unverified_source_store_count} 家来源未建档`);
  }
  return details.join("；") || "该业务日销售额来源待核验";
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
  <div ref="pageElement" class="erp-page overview-page" :style="pageStyle">
    <div class="page-intro">
      <div>
        <p class="section-kicker">
          {{ allStoresSelected ? "MULTI-STORE COMMAND" : "BUSINESS PULSE" }}
        </p>
        <h2>{{ allStoresSelected ? `${multiStoreLabel}经营总览` : `${currentStoreName} 经营总览` }}</h2>
      </div>
    </div>

    <HomeDashboard
      :scope="allStoresSelected ? storeScope : 'current'" :start="rangeStart" :end="rangeEnd"
      :store-name="allStoresSelected ? multiStoreLabel : currentStoreName"
      :store-inventory="allStoresSelected && !storeLoading ? storeData?.stores : undefined"
      @loading-change="homeLoading = $event"
    >
      <template #forecast>
        <div v-if="allStoresSelected && storeData && !storeLoading" class="forecast-strip">
          <div><span>{{ periodRevenueLabels.projectedTotal }} <small>估算</small></span><strong>{{ currency(multiStoreRevenueProjection.projectedTotal) }}</strong><small>{{ periodRevenueProjectionBasis(multiStoreRevenueProjection, multiStorePeriodRevenue) }}</small></div>
          <div><span>{{ periodRevenueLabels.dailyAverage }} · 已结束日</span><strong>{{ currency(multiStorePeriodRevenue.dailyAverage) }}</strong><small>{{ periodRevenueAverageBasis(multiStorePeriodRevenue) }}</small></div>
          <details class="forecast-basis"><summary>查看预测依据与覆盖</summary><p>{{ rangeStart }} 至 {{ storeData.sales_revenue_completed_through }} · {{ periodRevenueLabels.total }} {{ currency(multiStorePeriodRevenue.total) }}</p><p>{{ periodRevenueCoverage(multiStorePeriodRevenue) }}；当日进行中金额不参与此预测。</p></details>
        </div>
        <div v-else-if="!allStoresSelected && data && !loading" class="forecast-strip">
          <div><span>{{ periodRevenueLabels.projectedTotal }} <small>估算</small></span><strong>{{ currency(singleStoreRevenueProjection.projectedTotal) }}</strong><small>{{ periodRevenueProjectionBasis(singleStoreRevenueProjection, singleStorePeriodRevenue) }}</small></div>
          <div><span>{{ periodRevenueLabels.dailyAverage }}</span><strong>{{ currency(singleStorePeriodRevenue.dailyAverage) }}</strong><small>{{ periodRevenueAverageBasis(singleStorePeriodRevenue) }}</small></div>
          <details class="forecast-basis"><summary>查看预测依据与覆盖</summary><p>{{ rangeStart }} 至 {{ rangeEnd }} · {{ periodRevenueLabels.total }} {{ currency(singleStorePeriodRevenue.total) }}</p><p>{{ periodRevenueCoverage(singleStorePeriodRevenue) }}；采用单店所选区间已有指标日。</p></details>
        </div>
      </template>
      <template #warehouse-logistics>
        <div v-if="storeData && !storeLoading" class="warehouse-logistics">
          <div class="platform-stages">
            <span>待入官方仓</span>
            <span :title="offerCoverage(storeData.logistics.platform_warehouse.platform_stock_on_way_coverage, storeData.logistics.platform_warehouse.offer_count)">在途 <b>{{ number(storeData.logistics.platform_warehouse.platform_stock_on_way) }}</b> 件 <small v-if="storeData.logistics.platform_warehouse.platform_stock_on_way_coverage < storeData.logistics.platform_warehouse.offer_count">部分覆盖</small></span>
            <span :title="offerCoverage(storeData.logistics.platform_warehouse.platform_stock_in_receiving_coverage, storeData.logistics.platform_warehouse.offer_count)">收货中 <b>{{ number(storeData.logistics.platform_warehouse.platform_stock_in_receiving) }}</b> 件 <small v-if="storeData.logistics.platform_warehouse.platform_stock_in_receiving_coverage < storeData.logistics.platform_warehouse.offer_count">部分覆盖</small></span>
          </div>
          <div class="shared-warehouse">
            <div><h4>共享海外仓</h4><span>{{ storeData.logistics.overseas_warehouse.warehouse_name || '南非仓' }} · 多店只计一次</span><small>{{ formatChinaDateTime(storeData.logistics.overseas_warehouse.snapshot_at) }}</small></div>
            <dl>
              <div><dt>库存总数</dt><dd>{{ number(storeData.logistics.overseas_warehouse.stock_total) }}</dd></div>
              <div><dt>可用</dt><dd>{{ number(storeData.logistics.overseas_warehouse.usable_stock) }}</dd></div>
              <div><dt>锁定</dt><dd>{{ number(storeData.logistics.overseas_warehouse.locked_stock) }}</dd></div>
              <div><dt>已分配出库</dt><dd>{{ number(storeData.logistics.overseas_warehouse.outbound_allocated) }}</dd></div>
              <div><dt>海外仓在途</dt><dd>{{ number(storeData.logistics.overseas_warehouse.transit_stock) }}</dd></div>
            </dl>
          </div>
          <small class="stage-note">各库存阶段可能重叠，请勿相加。</small>
        </div>
      </template>
      <template #store-comparison>
        <section v-if="allStoresSelected" class="operations-panel">
          <header class="operations-heading"><div><h3>店铺经营对比</h3><span>{{ rangeEnd }} · {{ storeData?.store_count ?? '—' }} 家店铺</span></div><p v-if="storeData && !storeLoading" class="health-summary"><b>{{ overallHealthText }}</b><span>待补数据 {{ storeData.health_summary.data_gap }} · 正常 {{ storeData.health_summary.healthy }}</span></p></header>
          <div v-if="storeLoading" class="state-card slim">正在汇总各店经营数据……</div>
          <div v-else-if="storeError" class="state-card error slim">{{ storeError }}{{ storeData ? '，保留最近成功结果。' : '' }}</div>
          <template v-if="storeData && !storeLoading">
            <div v-if="!storeData.stores.length" class="state-card slim">当前账号暂无可见且已接入的店铺。</div>
            <div v-else class="operations-table-wrap">
              <table class="operations-table">
                <thead><tr><th>店铺 / 负责运营</th><th>经营状态</th><th>近7日件数</th><th>缺货商品</th><th>近30天转化率中位数</th><th>周期末近30天浏览量</th><th></th></tr></thead>
                <tbody><tr v-for="store in storeData.stores" :key="store.store_code">
                  <td><b>{{ store.store_name }}</b><small>{{ operatorNames(store.operators) }}</small></td>
                  <td><details class="store-health-detail"><summary :class="store.health.state">{{ store.health.label }} <span>查看原因</span></summary><div><p v-for="reason in [...store.health.business_reasons, ...store.health.data_reasons]" :key="reason">{{ reason }}</p><p v-if="!store.health.business_reasons.length && !store.health.data_reasons.length">当前未发现既定风险项</p><small>最新指标日 {{ store.latest_metric_date || '暂无' }}</small><small v-for="operator in store.operators" :key="operator.user_id">{{ operator.display_name }} · {{ operatorRoleLabel(operator.role) }}</small></div></details></td>
                  <td>{{ number(store.kpis.seven_day_ordered_units) }}</td>
                  <td :class="{ 'stockout-cell': (store.kpis.stockout_products ?? 0) > 0 }">{{ store.latest_metric_date ? number(store.kpis.stockout_products) : '—' }}</td>
                  <td>{{ percent(store.kpis.median_conversion) }}</td>
                  <td>{{ number(trafficValue(store.latest_traffic_point)) }}<small>{{ trafficCoverage(store.latest_traffic_point) }}</small></td>
                  <td><button type="button" class="store-link" :aria-label="`查看 ${store.store_name} 明细`" @click="emit('selectStore', store.store_code)">明细 ↗</button></td>
                </tr></tbody>
                <tfoot><tr><th colspan="2">合计 <small>缺失不补 0</small></th><td>{{ number(storeTotals.sevenDayUnits.value) }}<small>{{ coverageLabel(storeTotals.sevenDayUnits.coverage, storeTotals.sevenDayUnits.total) }}</small></td><td>{{ number(storeTotals.stockouts.value) }}<small>{{ coverageLabel(storeTotals.stockouts.coverage, storeTotals.stockouts.total) }}</small></td><td colspan="3">浏览量为滚动30天商品合计，非当天流量或独立访客。</td></tr></tfoot>
              </table>
            </div>
          </template>
        </section>
      </template>
      <template #revenue-evidence>
        <details v-if="allStoresSelected && storeData && !storeLoading" class="revenue-evidence">
          <summary><b>销售数据与历史修订</b><span>历史共 {{ storeData.sales_reconciliation.revision_count }} 条修订<span v-if="storeData.sales_reconciliation.pending_store_count"> · {{ storeData.sales_reconciliation.pending_store_count }} 家待核验</span> · 查看来源与记录</span></summary>
          <p class="evidence-range">{{ rangeStart }} 至 {{ storeData.sales_revenue_completed_through }} · 已结束南非业务日；店铺缺失不补 0。</p>
          <div class="operations-table-wrap"><table class="operations-table"><thead><tr><th>业务日</th><th>已结束日销售额</th><th>店铺覆盖</th><th>来源核验</th><th>修订记录</th></tr></thead><tbody><tr v-for="point in storeData.sales_revenue_series" :key="point.metric_date"><td>{{ point.metric_date }}</td><td>{{ currency(point.total_ordered_revenue) }}<small v-if="point.missing_store_count">部分合计 · 缺 {{ point.missing_store_count }} 家</small></td><td>{{ point.covered_store_count }}/{{ point.store_count }}</td><td>{{ point.data_status === 'pending' ? revenuePendingStatus(point) : '已核验' }}<small>{{ formatChinaDateTime(point.latest_sales_verified_at) }}</small></td><td>{{ point.revision_count ? `${point.revision_count} 条` : '—' }}<small v-if="point.latest_revision_at">{{ formatChinaDateTime(point.latest_revision_at) }}</small></td></tr></tbody></table></div>
        <section class="sales-audit-panel" aria-labelledby="sales-audit-title">
          <div class="sales-audit-heading">
            <div>
              <p class="section-kicker">REVISION AUDIT</p>
              <h4 id="sales-audit-title">销售额日终后历史修订记录</h4>
              <span>仅记录日终基线后的变化</span>
            </div>
            <button type="button" class="secondary-button" :aria-expanded="salesAuditOpen" @click="toggleSalesAudit">
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
                {{ salesAuditLoading ? "查询中…" : "查询日终后修订" }}
              </button>
            </form>
            <div v-if="salesAuditError" class="state-card error slim">{{ salesAuditError }}</div>
            <div v-else-if="salesAuditLoading" class="state-card slim">正在读取本地修订审计…</div>
            <div
              v-else-if="salesAuditData && !salesAuditData.items.length"
              class="state-card slim"
            >
              所选日期暂无日终后修订。
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
        </details>
      </template>
    </HomeDashboard>

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
    <div v-else-if="!allStoresSelected && error && !data" class="state-card error">{{ error }}</div>
    <template v-else-if="!allStoresSelected && data">
      <div class="single-health-strip">
        <span>近7日下单 <b>{{ number(data.kpis.seven_day_ordered_units) }}</b> 件</span>
        <span>缺货商品 <b>{{ number(data.kpis.stockout_products) }}</b></span>
        <span>近30天转化率中位数 <b>{{ percent(data.kpis.median_conversion) }}</b></span>
        <details><summary>查看所选区间每日件数</summary><div class="operations-table-wrap"><table class="operations-table"><thead><tr><th>业务日</th><th>下单件数</th></tr></thead><tbody><tr v-for="point in data.sales_series" :key="point.metric_date"><td>{{ point.metric_date }}</td><td>{{ number(point.ordered_units) }}</td></tr></tbody></table></div></details>
      </div>

      <section class="erp-panel traffic-trend-panel">
        <div class="panel-heading traffic-heading">
          <div>
            <p class="section-kicker">PERIOD-END TRAFFIC</p>
            <h3>店铺商品近30天浏览量汇总趋势</h3>
          </div>
          <span>每日 09:00 更新</span>
        </div>
        <p class="traffic-definition">
          橙色虚线为参考值；缺失商品不补 0。
        </p>
        <div v-if="!data.traffic_series.length" class="state-card slim">
          暂无流量快照，等待下次采集。
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
          <div ref="trafficChartElement" class="traffic-chart-scroll">
            <div class="trend-chart-stage">
              <svg
                class="traffic-chart"
                :viewBox="`0 0 ${TRAFFIC_WIDTH} ${TRAFFIC_HEIGHT}`"
                role="img"
                aria-labelledby="traffic-chart-title traffic-chart-description"
                @pointermove="handleTrafficPointer"
                  @pointerdown="handleTrafficPointer"
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
          <table class="erp-table mobile-record-table">
            <thead>
              <tr>
                <th scope="col">商品</th>
                <th scope="col">下单件数</th>
                <th scope="col">下单金额</th>
                <th scope="col">近30天浏览量</th>
                <th scope="col">转化率</th>
                <th scope="col">库存</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in data.top_products" :key="item.offer_id">
                <td data-label="商品" data-mobile-wide>
                  <strong>{{ item.title || item.sku || item.offer_id }}</strong>
                  <small>平台 {{ item.sku || "无库存编码" }} · 公司 {{ item.company_sku || "未关联" }}</small>
                </td>
                <td data-label="下单件数">{{ number(item.ordered_units) }}</td>
                <td data-label="下单金额">{{ currency(item.ordered_revenue) }}</td>
                <td data-label="近30天浏览量">{{ number(item.page_views_30_days) }}</td>
                <td data-label="转化率">{{ percent(item.conversion_percentage_30_days) }}</td>
                <td data-label="库存">{{ number(item.total_stock) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.forecast-strip { display:flex; align-items:center; flex-wrap:wrap; gap:18px 30px; padding:16px 20px; background:#eff4e7; border:1px solid var(--line); border-radius:12px; }
.forecast-strip>div { display:grid; gap:6px; }
.forecast-strip span { font-size:12px; color:#61735a; }
.forecast-strip strong { font-size:23px; color:#305338; font-variant-numeric:tabular-nums; }
.forecast-strip small { font-size:10px; color:#798672; }
.forecast-basis { margin-left:auto; font-size:11px; color:var(--muted); max-width:390px; }
.forecast-basis summary,.store-health-detail summary,.revenue-evidence>summary,.single-health-strip summary { cursor:pointer; }
.forecast-basis p { line-height:1.6; margin-bottom:0; }
.warehouse-logistics { margin-top:16px; border-top:1px solid var(--line); padding-top:14px; }
.platform-stages { display:flex; align-items:center; flex-wrap:wrap; gap:12px 24px; font-size:12px; color:#6c7865; }
.platform-stages b { margin:0 4px; font-size:19px; color:#35563b; font-variant-numeric:tabular-nums; }
.platform-stages small { color:#9e6e27; }
.shared-warehouse { display:flex; align-items:center; flex-wrap:wrap; gap:18px; background:#f4f6ee; padding:16px; border-radius:10px; margin:14px 0 8px; }
.shared-warehouse h4 { margin:0 0 6px; font-size:14px; }
.shared-warehouse span,.shared-warehouse small { display:block; font-size:10px; color:#7b8473; line-height:1.7; }
.shared-warehouse dl { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); flex:1; gap:18px; margin:0; }
.shared-warehouse dt { font-size:11px; color:#7b8473; }
.shared-warehouse dd { margin:7px 0 0; font-size:20px; color:#45603d; font-variant-numeric:tabular-nums; }
.stage-note { font-size:10px; color:var(--muted); }
.operations-panel { min-width:0; padding:20px; border:1px solid var(--line); border-radius:16px; background:var(--paper); }
.operations-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
.operations-heading h3 { margin:0 0 6px; font-size:17px; }
.operations-heading span { font-size:11px; color:var(--muted); }
.health-summary { display:flex; align-items:center; flex-wrap:wrap; gap:12px; margin:0; font-size:12px; }
.health-summary b { font-weight:500; color:#8e6b31; }
.operations-table-wrap { max-width:100%; overflow-x:auto; }
.operations-table { width:100%; border-collapse:collapse; font-size:12px; font-variant-numeric:tabular-nums; }
.operations-table th,.operations-table td { padding:12px 10px; text-align:right; border-bottom:1px solid #e4e9df; white-space:nowrap; }
.operations-table th { font-size:11px; font-weight:500; background:#f0f4eb; color:#718069; }
.operations-table td:first-child,.operations-table th:first-child { text-align:left; }
.operations-table tbody td:first-child b { font-weight:500; }
.operations-table small { display:block; font-size:10px; color:#88907f; line-height:1.7; }
.operations-table tbody tr:hover { background:#f8faf3; }
.operations-table tfoot td { font-size:11px; background:#f7f9f2; }
.operations-table tfoot td:last-child { white-space:normal; color:var(--muted); }
.operations-table .stockout-cell { color:#a17a34; }
.store-health-detail { text-align:left; min-width:116px; }
.store-health-detail summary { font-size:11px; }
.store-health-detail summary span { font-size:10px; color:#879180; margin-left:4px; }
.store-health-detail .attention { color:#a17a34; }.store-health-detail .data_gap { color:#ac7f31; }.store-health-detail .healthy { color:#3a7250; }
.store-health-detail>div { max-width:220px; white-space:normal; font-size:11px; line-height:1.5; color:var(--muted); }
.store-health-detail p { margin:8px 0; }
.store-link { border:0; background:transparent; color:#4a7957; padding:8px 4px; font-size:11px; cursor:pointer; white-space:nowrap; }
.store-link:hover { text-decoration:underline; }
.revenue-evidence { min-width:0; border:1px solid var(--line); border-radius:12px; padding:16px 20px; background:#f6f8f1; }
.revenue-evidence>summary { font-size:13px; color:#536a4d; }
.revenue-evidence>summary>b { font-weight:500; }
.revenue-evidence>summary>span { margin-left:14px; font-size:11px; color:#7d8877; }
.evidence-range { font-size:11px; color:var(--muted); margin:18px 0 12px; }
.single-health-strip { display:flex; flex-wrap:wrap; gap:16px 25px; padding:15px 20px; border:1px solid var(--line); border-radius:12px; background:#f5f7ef; color:#75846e; font-size:12px; }
.single-health-strip b { color:#3f6043; margin-left:6px; }
.single-health-strip details { margin-left:auto; }.single-health-strip details[open] { width:100%; }
@media(max-width:760px) {
  .forecast-strip { padding:14px 12px; gap:18px; }.forecast-strip>div { flex:1; min-width:125px; }.forecast-strip strong { font-size:21px; }.forecast-basis { margin-left:0; }
  .shared-warehouse { padding:14px 12px; }.shared-warehouse>div { width:100%; }.shared-warehouse dl { grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }.shared-warehouse dd { font-size:18px; }
  .operations-panel { padding:16px 12px; }.operations-heading { align-items:flex-start; flex-direction:column; gap:10px; }.operations-table th,.operations-table td { padding:12px 8px; }
  .revenue-evidence { padding:14px 12px; }.revenue-evidence>summary>span { display:block; margin:8px 0 0; }.single-health-strip details { margin-left:0; }
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

  .traffic-heading > span {
    text-align: left;
  }

  .selected-store-meta {
    align-items: flex-start;
  }
}

/* Mobile layout: retain every field and existing action. */

@media (max-width: 760px) {
  .sales-audit-filter input { width: 100%; }
  .traffic-latest { gap: 6px; }
}
</style>
