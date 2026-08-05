<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { fetchSummary } from "../api";
import { formatChinaDateTime } from "../time";
import type { StoreTrafficPoint, SummaryPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const data = ref<SummaryPayload | null>(null);
const loading = ref(true);
const error = ref("");

const maxUnits = computed(() =>
  Math.max(1, ...(data.value?.sales_series.map((item) => item.ordered_units ?? 0) ?? [1])),
);

const TRAFFIC_WIDTH = 760;
const TRAFFIC_HEIGHT = 250;
const TRAFFIC_LEFT = 64;
const TRAFFIC_RIGHT = 18;
const TRAFFIC_TOP = 20;
const TRAFFIC_BOTTOM = 38;

const trafficChart = computed(() => {
  const source = data.value?.traffic_series ?? [];
  const values = source
    .map((point) => point.page_views_30_days_total)
    .filter((value): value is number => value !== null);
  if (!source.length) {
    return { dots: [], segments: [], ticks: [], labels: [] };
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
  const dots = source.map((point, index) => ({
    point,
    x: x(index),
    y: point.page_views_30_days_total === null
      ? TRAFFIC_TOP + plotHeight
      : y(point.page_views_30_days_total),
  }));
  const segments: string[] = [];
  let current: string[] = [];
  for (const dot of dots) {
    if (dot.point.page_views_30_days_total === null) {
      if (current.length) segments.push(current.join(" "));
      current = [];
    } else {
      current.push(`${dot.x},${dot.y}`);
    }
  }
  if (current.length) segments.push(current.join(" "));
  const ticks = [maximum, (maximum + minimum) / 2, minimum].map((value) => ({
    value,
    y: y(value),
  }));
  const labelEvery = Math.max(1, Math.ceil(source.length / 6));
  const labels = dots.filter((_, index) =>
    index === 0 || index === dots.length - 1 || index % labelEvery === 0,
  );
  return { dots, segments, ticks, labels };
});

const latestTrafficPoint = computed(() => data.value?.traffic_series.at(-1) ?? null);

watch(() => props.asOf, load, { immediate: true });

async function load() {
  loading.value = true;
  error.value = "";
  try {
    data.value = await fetchSummary(props.asOf);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "经营数据读取失败";
  } finally {
    loading.value = false;
  }
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

function trafficPointTitle(point: StoreTrafficPoint) {
  const capture = formatChinaDateTime(point.captured_at);
  if (point.page_views_30_days_total !== null) {
    const returned = point.product_count - point.missing_product_count;
    return `${point.business_date}：已返回商品近30天浏览量合计 ${number(point.page_views_30_days_total)}；覆盖 ${returned}/${point.product_count} 个商品，缺失 ${point.missing_product_count} 个；周期末采集 ${capture}`;
  }
  if (point.status === "failed") {
    return `${point.business_date}：周期末刷新失败，本日未记录合计`;
  }
  return `${point.business_date}：全部商品都缺少近30天浏览量；周期末采集 ${capture}`;
}
</script>

<template>
  <div class="erp-page overview-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">BUSINESS PULSE</p>
        <h2>先看经营结果，再定位商品动作</h2>
      </div>
      <p>
        截止 {{ asOf }} · 最新可用指标日 {{ data?.latest_metric_date || "暂无" }} ·
        下单件数为主销售口径
      </p>
    </div>

    <div v-if="loading" class="state-card">正在读取经营数据……</div>
    <div v-else-if="error" class="state-card error">{{ error }}</div>
    <template v-else-if="data">
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
        <article class="kpi-alert">
          <span>异常商品</span>
          <strong>{{ data.kpis.latest_anomaly_products }}</strong>
          <small>最新指标日去重</small>
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
          缺失商品不补 0，并单独标出覆盖数量；这不是当天浏览量，也不是独立访客数。
        </p>
        <div v-if="!data.traffic_series.length" class="state-card slim">
          暂无周期末流量快照；下次 09:00 周期末刷新成功后开始记录。
        </div>
        <template v-else>
          <div
            class="traffic-latest"
            :class="{ incomplete: (latestTrafficPoint?.missing_product_count ?? 0) > 0 || latestTrafficPoint?.page_views_30_days_total === null }"
          >
            <strong>{{ number(latestTrafficPoint?.page_views_30_days_total) }}</strong>
            <span v-if="latestTrafficPoint?.page_views_30_days_total !== null">
              {{ latestTrafficPoint?.business_date }} · 已返回
              {{ (latestTrafficPoint?.product_count ?? 0) - (latestTrafficPoint?.missing_product_count ?? 0) }}/{{ latestTrafficPoint?.product_count }} 个商品
              <template v-if="latestTrafficPoint?.missing_product_count">
                · 缺失 {{ latestTrafficPoint.missing_product_count }} 个（未补 0）
              </template>
            </span>
            <span v-else-if="latestTrafficPoint?.status === 'failed'">
              {{ latestTrafficPoint?.business_date }} 周期末刷新失败，本日折线保留断点
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
              <desc id="traffic-chart-description">成功日汇总接口已返回浏览量的商品并标明缺失数，整次周期末刷新失败的日期保留断点。</desc>
              <g class="traffic-grid">
                <template v-for="tick in trafficChart.ticks" :key="tick.y">
                  <line :x1="TRAFFIC_LEFT" :x2="TRAFFIC_WIDTH - TRAFFIC_RIGHT" :y1="tick.y" :y2="tick.y" />
                  <text :x="TRAFFIC_LEFT - 10" :y="tick.y + 4">{{ number(Math.round(tick.value)) }}</text>
                </template>
              </g>
              <polyline
                v-for="(segment, index) in trafficChart.segments"
                :key="index"
                class="traffic-line"
                :points="segment"
              />
              <g v-for="dot in trafficChart.dots" :key="dot.point.business_date">
                <circle
                  :class="[
                    'traffic-dot',
                    {
                      missing: dot.point.page_views_30_days_total === null,
                      partial: dot.point.page_views_30_days_total !== null && dot.point.missing_product_count > 0,
                    },
                  ]"
                  :cx="dot.x"
                  :cy="dot.y"
                  :r="dot.point.page_views_30_days_total === null ? 5 : 4"
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

@media (max-width: 760px) {
  .traffic-heading,
  .traffic-latest {
    align-items: flex-start;
    flex-direction: column;
  }

  .traffic-heading > span {
    text-align: left;
  }
}
</style>
