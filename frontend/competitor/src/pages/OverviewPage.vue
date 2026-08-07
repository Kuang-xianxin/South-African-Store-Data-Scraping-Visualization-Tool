<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { fetchStoreOverview, fetchSummary } from "../api";
import { formatChinaDateTime } from "../time";
import type {
  StoreOperator,
  StoreOverviewPayload,
  StoreTrafficPoint,
  SummaryPayload,
} from "../types";

const props = defineProps<{
  asOf: string;
  currentStoreName: string;
  allStoresSelected: boolean;
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
let loadRequestId = 0;

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
    return { dots: [], officialSegments: [], referenceSegments: [], ticks: [], labels: [] };
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
  const referenceSegments: string[] = [];
  for (let index = 1; index < dots.length; index += 1) {
    const previous = dots[index - 1];
    const current = dots[index];
    if (previous.value === null || current.value === null) continue;
    const segment = `${previous.x},${previous.y} ${current.x},${current.y}`;
    if (previous.isReference || current.isReference) {
      referenceSegments.push(segment);
    } else {
      officialSegments.push(segment);
    }
  }
  const ticks = [maximum, (maximum + minimum) / 2, minimum].map((value) => ({
    value,
    y: y(value),
  }));
  const labelEvery = Math.max(1, Math.ceil(source.length / 6));
  const labels = dots.filter((_, index) =>
    index === 0 || index === dots.length - 1 || index % labelEvery === 0,
  );
  return { dots, officialSegments, referenceSegments, ticks, labels };
});

const latestTrafficPoint = computed(() => data.value?.traffic_series.at(-1) ?? null);
const latestTrafficValue = computed(() => trafficValue(latestTrafficPoint.value));

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

const overallHealthText = computed(() => {
  const summary = storeData.value?.health_summary;
  if (!summary) return "正在核对健康信号";
  if (summary.attention) return `${summary.attention} 家店铺需优先关注`;
  if (summary.data_gap) return `${summary.data_gap} 家店铺存在数据缺口`;
  return "当前口径未发现缺货或数据缺口";
});

watch(
  () => [props.asOf, props.allStoresSelected],
  load,
  { immediate: true },
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
      const nextStoreData = await fetchStoreOverview(props.asOf);
      if (requestId !== loadRequestId) return;
      storeData.value = nextStoreData;
    } catch (reason) {
      if (requestId !== loadRequestId) return;
      storeData.value = null;
      storeError.value = reason instanceof Error
        ? reason.message
        : "六店经营总览读取失败";
    } finally {
      if (requestId === loadRequestId) storeLoading.value = false;
    }
    return;
  }

  storeData.value = null;
  try {
    const nextData = await fetchSummary(props.asOf);
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
    operator: "运营",
    viewer: "查看",
    selection: "选品",
  } as const)[role];
}

function operatorNames(operators: StoreOperator[] | undefined) {
  return operators?.length
    ? operators.map((operator) => operator.display_name).join("、")
    : "暂未分配非管理员运营";
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
        <h2>{{ allStoresSelected ? "六店经营总览" : `${currentStoreName} 经营总览` }}</h2>
      </div>
      <p v-if="allStoresSelected">
        截止 {{ asOf }} · 汇总当前账号可见、已启用且已接入的店铺 ·
        各店按自身最新可用指标日展示
      </p>
      <p v-else>
        截止 {{ asOf }} · 最新可用指标日 {{ data?.latest_metric_date || "暂无" }} ·
        下单件数为主销售口径
      </p>
    </div>

    <section v-if="allStoresSelected" class="erp-panel multi-store-panel">
      <div class="panel-heading multi-store-heading">
        <div>
          <p class="section-kicker">ALL CONNECTED STORES</p>
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
            <small>按缺货和数据完整性直接判断；数据异常暂只在风险与质量模块展示</small>
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

        <section class="logistics-command" aria-labelledby="logistics-command-title">
          <div class="logistics-command-heading">
            <div>
              <p class="section-kicker">INVENTORY &amp; LOGISTICS</p>
              <h4 id="logistics-command-title">库存与物流全盘</h4>
            </div>
            <span>海外仓共享库存只计一次；平台库存按可见店铺汇总</span>
          </div>
          <div class="logistics-total-grid">
            <article class="overseas-card">
              <span>海外仓库存</span>
              <strong>{{ number(storeData.logistics.overseas_warehouse.stock_total) }}</strong>
              <small>
                {{ storeData.logistics.overseas_warehouse.warehouse_name || "W8 共享海外仓" }}
                · 六店共享只计一次
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
              <strong v-else>暂未分配非管理员运营</strong>
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
              <p class="section-kicker">30 DAY ORDERS</p>
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
            <svg
              class="traffic-chart"
              :viewBox="`0 0 ${TRAFFIC_WIDTH} ${TRAFFIC_HEIGHT}`"
              role="img"
              aria-labelledby="traffic-chart-title traffic-chart-description"
            >
              <title id="traffic-chart-title">店铺商品近30天浏览量每日周期末汇总折线图</title>
              <desc id="traffic-chart-description">绿色实线为成功的周期末汇总；周期末失败但同日另有成功采集时，以橙色虚线展示参考并保留正式失败事实；没有同日参考时保留断点。</desc>
              <g class="traffic-grid">
                <template v-for="tick in trafficChart.ticks" :key="tick.y">
                  <line :x1="TRAFFIC_LEFT" :x2="TRAFFIC_WIDTH - TRAFFIC_RIGHT" :y1="tick.y" :y2="tick.y" />
                  <text :x="TRAFFIC_LEFT - 10" :y="tick.y + 4">{{ number(Math.round(tick.value)) }}</text>
                </template>
              </g>
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
              <g v-for="dot in trafficChart.dots" :key="dot.point.business_date">
                <circle
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
                  :r="dot.value === null || dot.isReference ? 5 : 4"
                  tabindex="0"
                >
                  <title>{{ trafficPointTitle(dot.point) }}</title>
                </circle>
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
          </div>
          <div class="traffic-legend">
            <span><i></i>完整商品覆盖</span>
            <span><i class="partial"></i>部分商品缺失（未补 0）</span>
            <span><i class="reference-line"></i>同日最近成功采集参考（虚线）</span>
            <span><i class="missing"></i>整次刷新失败</span>
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
  overflow-x: auto;
}

.traffic-chart {
  display: block;
  width: 100%;
  min-width: 680px;
  height: auto;
}

.traffic-grid line {
  stroke: #dfe7df;
  stroke-width: 1;
}

.traffic-grid text {
  fill: var(--muted);
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 10px;
  text-anchor: end;
}

.traffic-line {
  fill: none;
  stroke: var(--green);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 3;
}

.traffic-line.reference {
  stroke: #c88224;
  stroke-dasharray: 7 6;
  stroke-width: 2.5;
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

.traffic-dot:focus {
  outline: none;
  stroke: #162d24;
  stroke-width: 4;
}

.traffic-axis-labels text {
  fill: var(--muted);
  font-size: 10px;
  text-anchor: middle;
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

@media (max-width: 760px) {
  .multi-total-grid,
  .logistics-total-grid,
  .store-overview-grid {
    grid-template-columns: 1fr;
  }

  .command-health,
  .logistics-command-heading,
  .traffic-heading,
  .traffic-latest,
  .selected-store-heading {
    align-items: flex-start;
    flex-direction: column;
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
