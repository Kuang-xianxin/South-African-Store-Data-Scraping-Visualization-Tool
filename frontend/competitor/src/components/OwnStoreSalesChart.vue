<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  OWN_STORE_SALES_CHART,
  buildOwnStoreSalesChart,
  nearestOwnStoreSalesPointIndex,
} from "../ownStoreSalesChart";
import type { OwnStoreSalesSeries } from "../types";

const props = defineProps<{
  series: OwnStoreSalesSeries[];
  preferredStoreCode?: string | null;
}>();

const selectedStoreCode = ref("");
const activeIndex = ref(0);

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
const geometry = computed(() =>
  buildOwnStoreSalesChart(selectedSeries.value?.points ?? []),
);
const activePoint = computed(
  () => selectedSeries.value?.points[activeIndex.value] ?? null,
);
const activeChartPoint = computed(
  () => geometry.value.points[activeIndex.value] ?? null,
);
const visiblePoints = computed(() => {
  const points = geometry.value.points.filter((point) => point.y !== null);
  if (points.length <= 180) return points;
  return points.filter(
    (point) =>
      (point.units ?? 0) > 0 ||
      point.index === 0 ||
      point.index === geometry.value.points.length - 1 ||
      point.index === activeIndex.value,
  );
});

watch(
  selectedSeries,
  (series) => {
    if (!series?.points.length) {
      activeIndex.value = 0;
      return;
    }
    const lastVerified = series.points.findLastIndex(
      (point) => point.ordered_units !== null,
    );
    activeIndex.value = lastVerified >= 0 ? lastVerified : series.points.length - 1;
  },
  { immediate: true },
);

function handlePointer(event: PointerEvent) {
  const target = event.currentTarget as SVGSVGElement;
  const bounds = target.getBoundingClientRect();
  activeIndex.value = nearestOwnStoreSalesPointIndex(
    event.clientX - bounds.left,
    bounds.width,
    selectedSeries.value?.points.length ?? 0,
  );
}

function stepPoint(delta: number) {
  const count = selectedSeries.value?.points.length ?? 0;
  if (!count) return;
  activeIndex.value = Math.max(0, Math.min(count - 1, activeIndex.value + delta));
}

function number(value: number | null): string {
  return value === null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
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
      当前账号可见店铺没有该 PLID 的自有 Offer，未生成销量曲线。
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

      <div
        v-if="selectedSeries.points.length"
        class="own-sales-chart"
        tabindex="0"
        aria-label="自有商品日销量折线图，使用左右方向键切换国内日期"
        @keydown.left.prevent="stepPoint(-1)"
        @keydown.right.prevent="stepPoint(1)"
      >
        <div v-if="activePoint" class="own-sales-readout" aria-live="polite">
          <div>
            <small>国内日期</small>
            <strong>{{ activePoint.date }}</strong>
          </div>
          <div :class="{ missing: activePoint.ordered_units === null }">
            <small>实际下单件数</small>
            <strong>
              {{
                activePoint.ordered_units === null
                  ? "未覆盖"
                  : activePoint.data_status === "partial"
                    ? `${number(activePoint.ordered_units)} 件（截至采集）`
                    : `${number(activePoint.ordered_units)} 件`
              }}
            </strong>
          </div>
          <div>
            <small>来源</small>
            <strong>
              {{
                activePoint.ordered_units === null
                  ? "缺少 /sales 覆盖证据"
                  : activePoint.data_status === "partial"
                    ? "Seller Sales /sales · 日内截至最新采集"
                    : "Seller Sales /sales · 完整自然日"
              }}
            </strong>
            <span v-if="activePoint.revision_count">含 {{ activePoint.revision_count }} 次源数据修订</span>
          </div>
        </div>

        <svg
          :viewBox="`0 0 ${OWN_STORE_SALES_CHART.width} ${OWN_STORE_SALES_CHART.height}`"
          role="img"
          aria-label="按北京时间自然日归属的实际下单件数折线图"
          @pointermove="handlePointer"
        >
          <rect
            class="own-sales-surface"
            x="4"
            y="8"
            :width="OWN_STORE_SALES_CHART.width - 8"
            height="226"
            rx="10"
          />
          <g v-for="tick in geometry.yTicks" :key="`y:${tick.value}`">
            <line
              class="own-sales-grid"
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
          <text class="own-sales-axis-title" x="8" y="24">件</text>
          <path
            v-for="(segment, index) in geometry.segments"
            :key="`segment:${index}`"
            class="own-sales-line"
            :d="segment"
            vector-effect="non-scaling-stroke"
          />
          <circle
            v-for="point in visiblePoints"
            :key="`point:${point.index}`"
            class="own-sales-point"
            :class="{ active: point.index === activeIndex, partial: point.status === 'partial' }"
            :cx="point.x"
            :cy="point.y ?? 0"
            :r="point.index === activeIndex ? 5.5 : 3"
            vector-effect="non-scaling-stroke"
          />
          <line
            v-if="activeChartPoint"
            class="own-sales-cursor"
            :class="{ missing: activePoint?.ordered_units === null || activePoint?.data_status === 'partial' }"
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
            y="250"
            :text-anchor="tick.anchor"
          >{{ tick.label }}</text>
        </svg>
        <p>
          订单按北京时间重新归入国内自然日；完整的 0 件只在该国内日结束后，跨到的 Seller Sales 源日期均已成功复核时显示。今天等未结束日期标为“截至采集”；缺失日期会断线，不按 0 补齐，也不使用库存下降反推销量。
        </p>
      </div>
      <div v-else class="own-sales-empty">
        已识别自有 Offer，但当前还没有可绘制的上架日范围。
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

.own-sales-grid {
  stroke: rgba(37, 86, 70, 0.14);
  stroke-dasharray: 2 3;
}

.own-sales-axis,
.own-sales-axis-title,
.own-sales-time {
  fill: #667b73;
  font-size: 11px;
}

.own-sales-axis-title {
  font-weight: 800;
}

.own-sales-line {
  fill: none;
  stroke: #1d7257;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2.6;
}

.own-sales-point {
  fill: #1d7257;
  stroke: #f8fffb;
  stroke-width: 1.6;
}

.own-sales-point.active {
  fill: #b65432;
}

.own-sales-point.partial {
  fill: #c7842d;
}

.own-sales-cursor {
  stroke: rgba(23, 65, 51, 0.55);
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
  .own-sales-readout {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .own-sales-readout > div:last-child {
    grid-column: 1 / -1;
  }
}
</style>
