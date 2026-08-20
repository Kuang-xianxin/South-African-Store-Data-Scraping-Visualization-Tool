<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  OWN_STORE_SALES_CHART,
  aggregateOwnStoreSalesPoints,
  buildOwnStoreSalesChart,
  filterOwnStoreSalesPoints,
  getOwnStoreSalesDateBounds,
  getOwnStoreSalesRecentRange,
  nearestOwnStoreSalesPointIndex,
} from "../ownStoreSalesChart";
import type { OwnStoreSalesPoint, OwnStoreSalesSeries } from "../types";

const props = defineProps<{
  series: OwnStoreSalesSeries[];
  preferredStoreCode?: string | null;
}>();

const selectedStoreCode = ref("");
const activeIndex = ref(0);
const rangeStart = ref("");
const rangeEnd = ref("");
const rangeStoreCode = ref("");

const seriesSignature = computed(() =>
  props.series
    .map((item) => `${item.store_code}:${item.points.length}:${item.through_date}`)
    .join("|"),
);

watch(
  [seriesSignature, () => props.preferredStoreCode],
  () => {
    const preferred = props.series.find(
      (item) => item.store_code === props.preferredStoreCode,
    );
    if (preferred) {
      selectedStoreCode.value = preferred.store_code;
      return;
    }
    if (!props.series.some((item) => item.store_code === selectedStoreCode.value)) {
      selectedStoreCode.value = props.series[0]?.store_code ?? "";
    }
  },
  { immediate: true },
);

const selectedSeries = computed(
  () =>
    props.series.find((item) => item.store_code === selectedStoreCode.value) ??
    props.series[0] ??
    null,
);
const availableRange = computed(() =>
  getOwnStoreSalesDateBounds(selectedSeries.value?.points ?? []),
);
const availableStart = computed(() => availableRange.value?.start ?? "");
const availableEnd = computed(() => availableRange.value?.end ?? "");

watch(
  [selectedStoreCode, availableStart, availableEnd],
  ([storeCode, start, end]) => {
    if (!start || !end) {
      rangeStart.value = "";
      rangeEnd.value = "";
      rangeStoreCode.value = storeCode;
      return;
    }
    if (rangeStoreCode.value !== storeCode) {
      rangeStoreCode.value = storeCode;
      rangeStart.value = start;
      rangeEnd.value = end;
      return;
    }
    rangeStart.value = clampDate(rangeStart.value || start, start, end);
    rangeEnd.value = clampDate(rangeEnd.value || end, start, end);
    if (rangeStart.value > rangeEnd.value) {
      rangeStart.value = start;
      rangeEnd.value = end;
    }
  },
  { immediate: true },
);

const filteredPoints = computed(() => {
  const points = selectedSeries.value?.points ?? [];
  if (!rangeStart.value || !rangeEnd.value) return points;
  return filterOwnStoreSalesPoints(
    points,
    rangeStart.value,
    rangeEnd.value,
  );
});
const aggregatedPoints = computed(() =>
  aggregateOwnStoreSalesPoints(filteredPoints.value),
);
const displayBuckets = computed(() => aggregatedPoints.value.buckets);
const granularity = computed(() => aggregatedPoints.value.granularity);
const geometry = computed(() =>
  buildOwnStoreSalesChart(displayBuckets.value),
);
const activePoint = computed(
  () => displayBuckets.value[activeIndex.value] ?? null,
);
const activeChartPoint = computed(
  () => geometry.value.points[activeIndex.value] ?? null,
);
const visibleBars = computed(() =>
  geometry.value.points.filter((point) => point.barHeight !== null),
);
const missingBarMarkers = computed(() =>
  geometry.value.points.filter((point) => point.units === null),
);
const showBarValueLabels = computed(() => displayBuckets.value.length <= 24);
const isFullRange = computed(
  () =>
    rangeStart.value === availableStart.value &&
    rangeEnd.value === availableEnd.value,
);
const rangeSummary = computed(() => {
  const points = filteredPoints.value;
  const knownPoints = points.filter(
    (point): point is OwnStoreSalesPoint & { ordered_units: number } =>
      point.ordered_units !== null,
  );
  const verifiedPoints = points.filter(
    (point): point is OwnStoreSalesPoint & { ordered_units: number } =>
      point.data_status === "verified" && point.ordered_units !== null,
  );
  const peakPoint = verifiedPoints.reduce<(typeof verifiedPoints)[number] | null>(
    (peak, point) =>
      !peak || point.ordered_units > peak.ordered_units ? point : peak,
    null,
  );
  return {
    totalDays: points.length,
    coveredDays: verifiedPoints.length,
    partialDays: points.filter((point) => point.data_status === "partial").length,
    missingDays: points.filter((point) => point.data_status === "missing").length,
    orderedUnits: knownPoints.length
      ? knownPoints.reduce((total, point) => total + point.ordered_units, 0)
      : null,
    peakDate: peakPoint?.date ?? null,
    peakUnits: peakPoint?.ordered_units ?? null,
    salesDays: knownPoints.length
      ? knownPoints.filter((point) => point.ordered_units > 0).length
      : null,
    verifiedAverageUnits: verifiedPoints.length
      ? verifiedPoints.reduce((total, point) => total + point.ordered_units, 0)
        / verifiedPoints.length
      : null,
  };
});
const plotMessage = computed<{
  detail: string;
  title: string;
  tone: "missing" | "zero";
} | null>(() => {
  if (!displayBuckets.value.length) return null;
  const knownBuckets = displayBuckets.value.filter((point) => point.units !== null);
  const evidence = [
    `完整 ${rangeSummary.value.coveredDays} 天`,
    rangeSummary.value.partialDays
      ? `截至采集 ${rangeSummary.value.partialDays} 天`
      : "",
    rangeSummary.value.missingDays
      ? `缺失 ${rangeSummary.value.missingDays} 天`
      : "",
  ].filter(Boolean).join(" · ");
  if (!knownBuckets.length) {
    return {
      detail: `${evidence} · 缺失不会按 0 件补齐`,
      title: "所选区间暂无 Seller Sales 覆盖",
      tone: "missing",
    };
  }
  if (knownBuckets.every((point) => point.units === 0)) {
    return {
      detail: evidence,
      title: "已覆盖日期均为 0 件",
      tone: "zero",
    };
  }
  return null;
});

watch(
  displayBuckets,
  (points) => {
    if (!points.length) {
      activeIndex.value = 0;
      return;
    }
    const lastVerified = points.findLastIndex(
      (point) => point.units !== null,
    );
    activeIndex.value = lastVerified >= 0 ? lastVerified : points.length - 1;
  },
  { immediate: true },
);

function handlePointer(event: PointerEvent) {
  const target = event.currentTarget as SVGSVGElement;
  const bounds = target.getBoundingClientRect();
  activeIndex.value = nearestOwnStoreSalesPointIndex(
    event.clientX - bounds.left,
    bounds.width,
    displayBuckets.value.length,
  );
}

function stepPoint(delta: number) {
  const count = displayBuckets.value.length;
  if (!count) return;
  activeIndex.value = Math.max(0, Math.min(count - 1, activeIndex.value + delta));
}

function updateRangeStart(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  if (!availableRange.value) return;
  rangeStart.value = clampDate(
    value || availableRange.value.start,
    availableRange.value.start,
    availableRange.value.end,
  );
  if (!rangeEnd.value || rangeStart.value > rangeEnd.value) {
    rangeEnd.value = rangeStart.value;
  }
}

function updateRangeEnd(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  if (!availableRange.value) return;
  rangeEnd.value = clampDate(
    value || availableRange.value.end,
    availableRange.value.start,
    availableRange.value.end,
  );
  if (!rangeStart.value || rangeEnd.value < rangeStart.value) {
    rangeStart.value = rangeEnd.value;
  }
}

function resetDateRange() {
  if (!availableRange.value) return;
  rangeStart.value = availableRange.value.start;
  rangeEnd.value = availableRange.value.end;
}

function setRecentRange(dayCount: number) {
  if (!availableRange.value) return;
  const recent = getOwnStoreSalesRecentRange(availableRange.value, dayCount);
  rangeStart.value = recent.start;
  rangeEnd.value = recent.end;
}

function isRecentRange(dayCount: number): boolean {
  if (!availableRange.value) return false;
  const recent = getOwnStoreSalesRecentRange(availableRange.value, dayCount);
  return rangeStart.value === recent.start && rangeEnd.value === recent.end;
}

function clampDate(value: string, minimum: string, maximum: string): string {
  if (value < minimum) return minimum;
  if (value > maximum) return maximum;
  return value;
}

function number(value: number | null): string {
  return value === null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
}

function decimal(value: number | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
}

function granularityLabel(): string {
  if (granularity.value === "week") return "按周汇总";
  if (granularity.value === "month") return "按月汇总";
  return "按日展示";
}

function activePeriodLabel(): string {
  if (!activePoint.value) return "—";
  return activePoint.value.startDate === activePoint.value.endDate
    ? activePoint.value.startDate
    : `${activePoint.value.startDate} 至 ${activePoint.value.endDate}`;
}
</script>

<template>
  <section class="own-sales" aria-label="自有商品上架以来官方销量">
    <header class="own-sales-heading">
      <div>
        <p>OWN STORE OFFICIAL SALES</p>
        <h4>上架以来销量</h4>
        <span>国内自然日（北京时间）</span>
      </div>
      <label v-if="series.length > 1">
        <span>店铺</span>
        <select v-model="selectedStoreCode">
          <option
            v-for="item in series"
            :key="item.store_code"
            :value="item.store_code"
          >
            {{ item.store_name }}
          </option>
        </select>
      </label>
    </header>

    <div v-if="!selectedSeries" class="own-sales-empty">
      当前账号可见店铺没有该 PLID 的自有 Offer，未生成销量条形图。
    </div>
    <template v-else>
      <div class="own-sales-summary">
        <div>
          <small>{{ selectedSeries.listing_date_source === "platform" ? "平台上架日" : "本库最早记录" }}</small>
          <strong>{{ selectedSeries.listing_date }}</strong>
        </div>
        <div>
          <small>当前可见累计销量</small>
          <strong>{{ number(selectedSeries.total_ordered_units) }}<template v-if="selectedSeries.total_ordered_units !== null"> 件</template></strong>
        </div>
        <div>
          <small>Sales 完整覆盖</small>
          <strong>{{ selectedSeries.covered_days }} 天</strong>
        </div>
        <div :class="{ warning: selectedSeries.partial_days > 0 }">
          <small>日内截至采集</small>
          <strong>{{ selectedSeries.partial_days }} 天</strong>
        </div>
        <div :class="{ warning: selectedSeries.missing_days > 0 }">
          <small>未覆盖</small>
          <strong>{{ selectedSeries.missing_days }} 天</strong>
        </div>
      </div>

      <div v-if="availableRange" class="own-sales-range">
        <div class="own-sales-range-controls">
          <strong>图表范围（北京时间）</strong>
          <div class="own-sales-range-presets" role="group" aria-label="销量图快捷日期范围">
            <button
              type="button"
              :class="{ selected: isRecentRange(30) && !isFullRange }"
              @click="setRecentRange(30)"
            >近30天</button>
            <button
              type="button"
              :class="{ selected: isRecentRange(90) && !isFullRange }"
              @click="setRecentRange(90)"
            >近90天</button>
            <button
              type="button"
              :class="{ selected: isFullRange }"
              @click="resetDateRange"
            >全部日期</button>
          </div>
          <label>
            <span>开始日期</span>
            <input
              type="date"
              :value="rangeStart"
              :min="availableStart"
              :max="rangeEnd || availableEnd"
              required
              @input="updateRangeStart"
            />
          </label>
          <span class="own-sales-range-separator" aria-hidden="true">至</span>
          <label>
            <span>结束日期</span>
            <input
              type="date"
              :value="rangeEnd"
              :min="rangeStart || availableStart"
              :max="availableEnd"
              required
              @input="updateRangeEnd"
            />
          </label>
        </div>
        <div class="own-sales-range-insights">
          <div :class="{ warning: rangeSummary.partialDays || rangeSummary.missingDays }">
            <small>{{ rangeSummary.partialDays || rangeSummary.missingDays ? "区间已有销量" : "区间销量" }}</small>
            <strong>{{ number(rangeSummary.orderedUnits) }}<template v-if="rangeSummary.orderedUnits !== null"> 件</template></strong>
          </div>
          <div>
            <small>完整日均</small>
            <strong>{{ decimal(rangeSummary.verifiedAverageUnits) }} 件</strong>
          </div>
          <div>
            <small>有销量记录</small>
            <strong>{{ number(rangeSummary.salesDays) }}<template v-if="rangeSummary.salesDays !== null"> 天</template></strong>
          </div>
          <div>
            <small>最高完整单日</small>
            <strong>{{ number(rangeSummary.peakUnits) }}<template v-if="rangeSummary.peakUnits !== null"> 件</template></strong>
            <span v-if="rangeSummary.peakDate">{{ rangeSummary.peakDate }}</span>
          </div>
        </div>
        <p class="own-sales-range-status" aria-live="polite">
          <span>条形图显示 {{ rangeStart }} 至 {{ rangeEnd }}</span>
          <span>{{ granularityLabel() }} · {{ displayBuckets.length }} 根柱</span>
          <span>{{ rangeSummary.totalDays }} 个自然日</span>
          <span>完整 {{ rangeSummary.coveredDays }} 天</span>
          <span v-if="rangeSummary.partialDays" class="partial">
            截至采集 {{ rangeSummary.partialDays }} 天
          </span>
          <span v-if="rangeSummary.missingDays" class="missing">
            缺失 {{ rangeSummary.missingDays }} 天
          </span>
        </p>
        <p
          v-if="granularity !== 'day' || rangeSummary.partialDays || rangeSummary.missingDays"
          class="own-sales-range-note"
        >
          橙色柱为已有小计；缺失日期不补 0。
        </p>
      </div>

      <div
        v-if="filteredPoints.length"
        class="own-sales-chart"
        tabindex="0"
        :aria-label="`自有商品销量条形图，当前显示 ${rangeStart} 至 ${rangeEnd}，${granularityLabel()}，使用左右方向键切换柱子`"
        @keydown.left.prevent="stepPoint(-1)"
        @keydown.right.prevent="stepPoint(1)"
      >
        <div v-if="activePoint" class="own-sales-readout" aria-live="polite">
          <div>
            <small>{{ granularity === "day" ? "国内日期" : "国内日期范围" }}</small>
            <strong>{{ activePeriodLabel() }}</strong>
            <span>{{ granularityLabel() }}</span>
          </div>
          <div :class="{ missing: activePoint.units === null || activePoint.status === 'partial' }">
            <small>{{ activePoint.status === "verified" ? "完整下单件数" : "已有下单件数" }}</small>
            <strong>
              {{
                activePoint.units === null
                  ? "未覆盖"
                  : activePoint.status === "partial"
                    ? `${number(activePoint.units)} 件（周期不完整）`
                    : `${number(activePoint.units)} 件`
              }}
            </strong>
            <span v-if="activePoint.units !== null">
              {{ activePoint.salesDays }} 个有销量日
            </span>
          </div>
          <div>
            <small>覆盖证据</small>
            <strong>
              完整 {{ activePoint.verifiedDays }} 天
              · 截至采集 {{ activePoint.partialDays }} 天
              · 缺失 {{ activePoint.missingDays }} 天
            </strong>
            <span>Seller Sales /sales</span>
            <span v-if="activePoint.revisionCount">含 {{ activePoint.revisionCount }} 次日终基线后修订</span>
          </div>
        </div>

        <div class="own-sales-chart-meta">
          <div class="own-sales-legend" aria-label="销量条形图图例">
            <strong>图例</strong>
            <span><i class="complete" aria-hidden="true"></i>完整日 / 周期</span>
            <span><i class="partial" aria-hidden="true"></i>截至采集 / 周期不完整</span>
            <span><i class="zero" aria-hidden="true"></i>完整 0 件基线</span>
            <span><i class="missing" aria-hidden="true">×</i>缺失，不补 0</span>
          </div>
          <span class="own-sales-chart-hint">移动鼠标或使用 ← → 逐柱查点</span>
        </div>

        <svg
          :viewBox="`0 0 ${OWN_STORE_SALES_CHART.width} ${OWN_STORE_SALES_CHART.height}`"
          role="img"
          :aria-label="`按北京时间归属并${granularityLabel()}的实际下单件数条形图`"
          @pointermove="handlePointer"
        >
          <rect
            class="own-sales-surface"
            x="4"
            y="8"
            :width="OWN_STORE_SALES_CHART.width - 8"
            height="204"
            rx="10"
          />
          <rect
            v-if="activeChartPoint"
            class="own-sales-active-band"
            :class="{ warning: activePoint?.status !== 'verified' }"
            :x="activeChartPoint.focusX"
            :y="OWN_STORE_SALES_CHART.plotTop - 6"
            :width="activeChartPoint.focusWidth"
            :height="OWN_STORE_SALES_CHART.plotBottom - OWN_STORE_SALES_CHART.plotTop + 12"
            rx="7"
          />
          <line
            v-for="(tick, index) in geometry.xTicks"
            :key="`x-grid:${index}`"
            class="own-sales-grid vertical"
            :x1="tick.x"
            :x2="tick.x"
            :y1="OWN_STORE_SALES_CHART.plotTop"
            :y2="OWN_STORE_SALES_CHART.plotBottom"
            vector-effect="non-scaling-stroke"
          />
          <g v-for="tick in geometry.yTicks" :key="`y:${tick.value}`">
            <line
              class="own-sales-grid"
              :class="{ baseline: tick.value === 0 }"
              :x1="OWN_STORE_SALES_CHART.plotLeft"
              :x2="OWN_STORE_SALES_CHART.plotRight"
              :y1="tick.y"
              :y2="tick.y"
              vector-effect="non-scaling-stroke"
            />
            <text
              class="own-sales-axis"
              :x="OWN_STORE_SALES_CHART.plotLeft - 9"
              :y="(tick.y ?? 0) + 4"
              text-anchor="end"
            >{{ tick.label }}</text>
          </g>
          <text class="own-sales-axis-title" x="10" y="22">下单件数（整数）</text>
          <g
            v-if="plotMessage"
            class="own-sales-plot-message"
            :class="plotMessage.tone"
            aria-hidden="true"
          >
            <rect x="382" y="66" width="480" height="72" rx="12" />
            <text class="title" x="622" y="94" text-anchor="middle">
              {{ plotMessage.title }}
            </text>
            <text class="detail" x="622" y="119" text-anchor="middle">
              {{ plotMessage.detail }}
            </text>
          </g>
          <rect
            v-for="point in visibleBars"
            :key="`bar:${point.index}`"
            class="own-sales-bar"
            :class="{
              active: point.index === activeIndex,
              partial: point.status === 'partial',
              zero: point.units === 0,
            }"
            :x="point.barX"
            :y="point.barY ?? OWN_STORE_SALES_CHART.plotBottom"
            :width="point.barWidth"
            :height="point.barHeight ?? 0"
            rx="1.5"
            vector-effect="non-scaling-stroke"
          />
          <template v-if="showBarValueLabels">
            <text
              v-for="point in visibleBars.filter((item) => (item.units ?? 0) > 0)"
              :key="`bar-label:${point.index}`"
              class="own-sales-bar-label"
              :class="{ partial: point.status === 'partial' }"
              :x="point.x"
              :y="Math.max(OWN_STORE_SALES_CHART.plotTop + 11, (point.barY ?? OWN_STORE_SALES_CHART.plotBottom) - 6)"
              text-anchor="middle"
            >{{ number(point.units) }}</text>
          </template>
          <text
            v-for="point in missingBarMarkers"
            :key="`missing:${point.index}`"
            class="own-sales-missing-marker"
            :x="point.x"
            :y="OWN_STORE_SALES_CHART.plotBottom - 6"
            text-anchor="middle"
          >×</text>
          <line
            v-if="activeChartPoint"
            class="own-sales-cursor"
            :class="{ missing: activePoint?.units === null || activePoint?.status === 'partial' }"
            :x1="activeChartPoint.x"
            :x2="activeChartPoint.x"
            :y1="OWN_STORE_SALES_CHART.plotTop"
            :y2="OWN_STORE_SALES_CHART.plotBottom"
            vector-effect="non-scaling-stroke"
          />
          <text
            v-for="(tick, index) in geometry.xTicks"
            :key="`x:${index}`"
            class="own-sales-time"
            :x="tick.x"
            y="228"
            :text-anchor="tick.anchor"
          >{{ tick.label }}</text>
        </svg>
      </div>
      <div v-else class="own-sales-empty">
        {{
          selectedSeries.points.length
            ? "所选日期区间没有可绘制的销量数据。"
            : "已识别自有 Offer，但当前还没有可绘制的上架日范围。"
        }}
      </div>
    </template>
  </section>
</template>

<style scoped>
.own-sales {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid rgba(28, 96, 74, 0.16);
}

.own-sales-heading,
.own-sales-heading > div,
.own-sales-heading label,
.own-sales-readout > div {
  display: flex;
}

.own-sales-heading {
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.own-sales-heading > div,
.own-sales-heading label,
.own-sales-readout > div {
  flex-direction: column;
  gap: 3px;
}

.own-sales-heading p,
.own-sales-heading h4,
.own-sales-chart > p {
  margin: 0;
}

.own-sales-heading p {
  color: #1c684f;
  font-size: 0.68rem;
  font-weight: 800;
  letter-spacing: 0.14em;
}

.own-sales-heading h4 {
  color: #14261f;
  font-size: 1.05rem;
}

.own-sales-heading span,
.own-sales-heading label span {
  color: #60776e;
  font-size: 0.76rem;
}

.own-sales-heading select {
  min-width: 150px;
  padding: 7px 28px 7px 9px;
  border: 1px solid rgba(27, 96, 74, 0.25);
  border-radius: 8px;
  background: #fff;
  color: #173b30;
}

.own-sales-summary,
.own-sales-readout {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}

.own-sales-summary {
  margin-bottom: 10px;
}

.own-sales-range {
  margin-bottom: 10px;
  padding: 10px 12px;
  border: 1px solid rgba(31, 103, 80, 0.16);
  border-radius: 10px;
  background: rgba(248, 252, 250, 0.92);
}

.own-sales-range-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 8px;
}

.own-sales-range-controls > strong {
  align-self: center;
  margin-right: 4px;
  color: #173b30;
  font-size: 0.78rem;
}

.own-sales-range-controls label {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.own-sales-range-controls label span {
  color: #627a71;
  font-size: 0.68rem;
}

.own-sales-range-presets {
  display: inline-flex;
  gap: 4px;
  padding: 3px;
  border-radius: 9px;
  background: rgba(30, 105, 81, 0.08);
}

.own-sales-range-controls input,
.own-sales-range-controls button {
  min-height: 34px;
  border: 1px solid rgba(27, 96, 74, 0.25);
  border-radius: 8px;
  font: inherit;
}

.own-sales-range-controls input {
  min-width: 142px;
  padding: 6px 8px;
  background: #fff;
  color: #173b30;
}

.own-sales-range-controls button {
  padding: 6px 10px;
  border-color: transparent;
  background: transparent;
  color: #1c684f;
  cursor: pointer;
  font-size: 0.76rem;
  font-weight: 700;
}

.own-sales-range-controls button.selected {
  border-color: rgba(24, 100, 75, 0.28);
  background: #fff;
  box-shadow: 0 1px 3px rgba(20, 72, 56, 0.12);
  color: #124b39;
}

.own-sales-range-separator {
  align-self: center;
  color: #789087;
  font-size: 0.72rem;
}

.own-sales-range-status,
.own-sales-range-note {
  margin: 8px 0 0;
  color: #60736c;
  font-size: 0.7rem;
  line-height: 1.5;
}

.own-sales-range-status {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 12px;
}

.own-sales-range-status span:not(:last-child)::after {
  margin-left: 12px;
  color: rgba(47, 92, 77, 0.35);
  content: "·";
}

.own-sales-range-status .partial,
.own-sales-range-status .missing,
.own-sales-range-note {
  color: #9a582d;
}

.own-sales-range-insights {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-top: 9px;
}

.own-sales-range-insights > div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 5px;
  padding: 7px 9px;
  border: 1px solid rgba(31, 103, 80, 0.12);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.72);
}

.own-sales-range-insights > div.warning {
  border-color: rgba(181, 106, 51, 0.24);
  background: rgba(255, 247, 238, 0.88);
}

.own-sales-range-insights small,
.own-sales-range-insights span {
  color: #687b74;
  font-size: 0.66rem;
  white-space: nowrap;
}

.own-sales-range-insights strong {
  overflow: hidden;
  color: #173b30;
  font-size: 0.82rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.own-sales-summary > div,
.own-sales-readout > div {
  padding: 9px 11px;
  border: 1px solid rgba(31, 103, 80, 0.14);
  border-radius: 9px;
  background: rgba(242, 249, 246, 0.82);
}

.own-sales-summary > div.warning,
.own-sales-readout > div.missing {
  border-color: rgba(181, 106, 51, 0.24);
  background: rgba(255, 247, 238, 0.88);
}

.own-sales-summary small,
.own-sales-readout small {
  display: block;
  margin-bottom: 2px;
  color: #627a71;
  font-size: 0.7rem;
}

.own-sales-summary strong,
.own-sales-readout strong {
  color: #173b30;
  font-size: 0.88rem;
}

.own-sales-readout {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-bottom: 8px;
}

.own-sales-readout span {
  color: #6f7d78;
  font-size: 0.68rem;
}

.own-sales-chart-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  margin: 1px 4px 5px;
  color: #61766e;
  font-size: 0.68rem;
}

.own-sales-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px 13px;
}

.own-sales-legend strong {
  color: #274b3f;
  font-size: 0.69rem;
}

.own-sales-legend > span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.own-sales-legend i {
  display: inline-flex;
  width: 11px;
  height: 8px;
  align-items: center;
  justify-content: center;
  border-radius: 2px;
  font-style: normal;
  line-height: 1;
}

.own-sales-legend i.complete {
  background: #1d7257;
}

.own-sales-legend i.partial {
  background: #c7842d;
}

.own-sales-legend i.zero {
  height: 3px;
  border-radius: 99px;
  background: #3f856e;
}

.own-sales-legend i.missing {
  height: 11px;
  color: #a05f2d;
  font-size: 13px;
  font-weight: 900;
}

.own-sales-chart-hint {
  white-space: nowrap;
}

.own-sales-chart {
  width: 100%;
  outline: none;
}

.own-sales-chart:focus-visible {
  border-radius: 10px;
  box-shadow: 0 0 0 2px rgba(33, 117, 89, 0.24);
}

.own-sales-chart svg {
  display: block;
  width: 100%;
  height: auto;
  touch-action: none;
}

.own-sales-surface {
  fill: rgba(247, 252, 249, 0.96);
  stroke: rgba(31, 103, 80, 0.14);
}

.own-sales-active-band {
  fill: rgba(25, 104, 79, 0.065);
  stroke: rgba(25, 104, 79, 0.13);
  stroke-width: 1;
}

.own-sales-active-band.warning {
  fill: rgba(193, 119, 38, 0.075);
  stroke: rgba(166, 91, 28, 0.16);
}

.own-sales-grid {
  stroke: rgba(37, 86, 70, 0.16);
  stroke-dasharray: 2 3;
}

.own-sales-grid.vertical {
  stroke: rgba(37, 86, 70, 0.08);
  stroke-dasharray: 2 5;
}

.own-sales-grid.baseline {
  stroke: rgba(28, 84, 65, 0.42);
  stroke-dasharray: none;
}

.own-sales-axis,
.own-sales-axis-title,
.own-sales-time {
  fill: #667b73;
  font-size: 11px;
}

.own-sales-axis-title {
  fill: #355e50;
  font-weight: 800;
}

.own-sales-plot-message rect {
  fill: rgba(255, 255, 255, 0.84);
  stroke: rgba(34, 104, 81, 0.18);
}

.own-sales-plot-message .title {
  fill: #1e5f4a;
  font-size: 15px;
  font-weight: 850;
}

.own-sales-plot-message .detail {
  fill: #61766e;
  font-size: 11px;
  font-weight: 650;
}

.own-sales-plot-message.missing rect {
  fill: rgba(255, 248, 239, 0.9);
  stroke: rgba(178, 99, 38, 0.22);
}

.own-sales-plot-message.missing .title,
.own-sales-plot-message.missing .detail {
  fill: #985a2e;
}

.own-sales-bar {
  fill: #1d7257;
  stroke: rgba(16, 77, 58, 0.58);
  stroke-width: 0.8;
}

.own-sales-bar.zero {
  fill: rgba(29, 114, 87, 0.72);
  stroke: none;
}

.own-sales-bar.partial {
  fill: #c7842d;
  stroke: rgba(130, 77, 18, 0.62);
}

.own-sales-bar.active {
  stroke: #173f33;
  stroke-width: 2;
}

.own-sales-bar.partial.active {
  stroke: #744217;
}

.own-sales-bar-label,
.own-sales-missing-marker {
  fill: #1b5f49;
  font-size: 10px;
  font-weight: 800;
  pointer-events: none;
}

.own-sales-bar-label.partial,
.own-sales-missing-marker {
  fill: #a05f2d;
}

.own-sales-missing-marker {
  font-size: 14px;
}

.own-sales-cursor {
  stroke: rgba(23, 65, 51, 0.48);
  stroke-dasharray: 4 4;
}

.own-sales-cursor.missing {
  stroke: rgba(181, 84, 47, 0.78);
}

.own-sales-chart > p,
.own-sales-empty {
  color: #60736c;
  font-size: 0.72rem;
  line-height: 1.55;
}

.own-sales-empty {
  padding: 14px;
  border: 1px dashed rgba(31, 103, 80, 0.22);
  border-radius: 9px;
  background: rgba(248, 252, 250, 0.8);
}

@media (max-width: 760px) {
  .own-sales-heading {
    align-items: stretch;
    flex-direction: column;
  }

  .own-sales-summary,
  .own-sales-readout,
  .own-sales-range-insights {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .own-sales-readout > div:last-child {
    grid-column: 1 / -1;
  }

  .own-sales-range-controls {
    align-items: stretch;
  }

  .own-sales-range-controls > strong {
    flex-basis: 100%;
  }

  .own-sales-range-presets {
    flex-basis: 100%;
  }

  .own-sales-range-presets button {
    flex: 1 1 0;
  }

  .own-sales-range-controls label {
    flex: 1 1 140px;
  }

  .own-sales-range-controls input {
    width: 100%;
  }

  .own-sales-range-separator {
    display: none;
  }

  .own-sales-chart-meta {
    align-items: flex-start;
    flex-direction: column;
  }

  .own-sales-chart-hint {
    white-space: normal;
  }
}
</style>
