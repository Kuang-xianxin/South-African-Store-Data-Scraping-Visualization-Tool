<script setup lang="ts">
import { computed, ref } from "vue";
import { homeChartPath, homeCoverageLabel, type HomePoint } from "../homeDashboard";
import { cachedNumberFormatter } from "../numberFormatters";
import { useResponsiveChart } from "../useResponsiveChart";

const props = defineProps<{ title: string; points: HomePoint[]; metric: "orders" | "revenue"; bars?: boolean }>();
const active = ref<number | null>(null);
const pointer = ref({ x: 10, y: 30 });
const { chartElement, chartWidth, compact } = useResponsiveChart(640);
const top = 18, bottom = 200, left = 60;
const right = computed(() => chartWidth.value - 16);
const max = computed(() => Math.ceil(Math.max(1, ...props.points.map(p => p[props.metric] ?? 0)) / 3) * 3);
const nodes = computed(() => props.points.map((p, i) => ({
  ...p, x: left + (i + 0.5) / Math.max(props.points.length, 1) * (right.value - left),
  y: p[props.metric] === null ? null : bottom - p[props.metric]! / max.value * (bottom - top),
})));
const path = computed(() => homeChartPath(nodes.value));
const current = computed(() => active.value === null ? null : nodes.value[active.value]);
const stride = computed(() => Math.max(1, Math.ceil(props.points.length / (compact.value ? 4 : 6))));
const format = (v: number | null) => v === null ? "—" : `${props.metric === "revenue" ? "R" : ""}${cachedNumberFormatter("zh-CN", { maximumFractionDigits: 0 }).format(v)}`;
const axis = (v: number) => `${props.metric === "revenue" ? "R" : ""}${v >= 10000 ? `${(v / 10000).toFixed(1)}万` : cachedNumberFormatter("zh-CN", { maximumFractionDigits: 0 }).format(v)}`;
const partial = (p: HomePoint) => p.known_store_days < p.expected_store_days || p.in_progress
  || (props.metric === "orders" ? p.missing_order_ids : p.missing_price_lines);
function move(event: PointerEvent) {
  const element = event.currentTarget as SVGSVGElement;
  const box = element.getBoundingClientRect();
  const x = (event.clientX - box.left) / box.width * chartWidth.value;
  active.value = Math.max(0, Math.min(props.points.length - 1, Math.floor((x - left) / (right.value - left) * props.points.length)));
  pointer.value = { x: Math.max(4, Math.min(event.clientX - box.left + 12, box.width - 208)), y: event.clientY - box.top + 12 };
}
function keyboard(event: KeyboardEvent) {
  if (event.key === "Escape") { active.value = null; return; }
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  active.value = event.key === "Home" ? 0 : event.key === "End" ? props.points.length - 1
    : Math.max(0, Math.min(props.points.length - 1, (active.value ?? 0) + (event.key === "ArrowLeft" ? -1 : 1)));
  pointer.value = { x: 8, y: 8 };
}
</script>

<template>
  <article class="home-chart">
    <header><h3>{{ title }}</h3><span>{{ bars ? '最近 12 个月' : '所选日期 · 已结束业务日' }}</span></header>
    <div v-if="!points.length" class="chart-empty">该区间暂无已结束业务日</div>
    <div v-else ref="chartElement" class="chart-stage">
      <svg :viewBox="`0 0 ${chartWidth} 232`" role="group" :aria-label="`${title}，左右方向键查看数据`" tabindex="0"
        @pointermove="move" @pointerleave="active = null" @blur="active = null" @keydown="keyboard">
        <g v-for="i in [0, 1, 2, 3]" :key="i">
          <line :x1="left" :x2="right" :y1="bottom - i / 3 * (bottom - top)" :y2="bottom - i / 3 * (bottom - top)" stroke="#e3e9e2" stroke-dasharray="3 5" />
          <text :x="left - 8" :y="bottom - i / 3 * (bottom - top) + 4" text-anchor="end">{{ axis(max * i / 3) }}</text>
        </g>
        <path v-if="!bars" :d="path" fill="none" stroke="#297653" stroke-width="2.4" vector-effect="non-scaling-stroke" />
        <g v-for="(node, i) in nodes" :key="node.date">
          <template v-if="node.y !== null">
            <rect v-if="bars" :x="node.x - (right - left) / points.length * 0.3" :y="node.y"
              :width="(right - left) / points.length * 0.6" :height="Math.max(2, bottom - node.y)" rx="4"
              :fill="partial(node) ? '#c49b4b' : '#38805c'" :opacity="active === i ? 1 : 0.85" />
            <circle v-else :cx="node.x" :cy="node.y" :r="active === i ? 5 : 3" :fill="partial(node) ? '#b87926' : '#297653'" stroke="white" stroke-width="1.4" />
          </template>
          <text v-if="i % stride === 0 || i === nodes.length - 1" :x="node.x" y="224" text-anchor="middle">{{ node.date.slice(5) }}</text>
        </g>
      </svg>
      <div v-if="current" class="chart-tip" role="status" :style="{ left: `${pointer.x}px`, top: `${pointer.y}px` }">
        <b>{{ current.date }}{{ current.in_progress ? ' · 进行中' : '' }}</b>
        <strong>{{ format(current[metric]) }}{{ metric === 'orders' ? ' 单' : '' }}</strong>
        <small>{{ homeCoverageLabel(current) }}</small>
      </div>
    </div>
    <footer><span class="legend verified">已核验</span><span class="legend partial">覆盖不足{{ bars ? ' / 进行中' : '' }}</span><span>缺失保留空档</span></footer>
    <details><summary>查看图表数据</summary><div class="chart-table"><table><thead><tr><th>日期</th><th>{{ metric === 'orders' ? '订单数' : '销售额' }}</th><th>覆盖</th></tr></thead><tbody><tr v-for="point in points" :key="point.date"><td>{{ point.date }}</td><td>{{ format(point[metric]) }}</td><td>{{ homeCoverageLabel(point) }}</td></tr></tbody></table></div></details>
  </article>
</template>

<style scoped>
.home-chart{padding:20px;border:1px solid var(--line);border-radius:16px;background:var(--paper);min-width:0}
header{display:flex;align-items:baseline;justify-content:space-between;gap:12px}h3{font-size:15px;margin:0 0 18px}header span,footer,summary{font-size:11px;color:var(--muted)}
.chart-stage{position:relative}svg{display:block;width:100%;overflow:visible;touch-action:pan-y}svg:focus-visible{outline:2px solid var(--green);outline-offset:5px}svg text{fill:#728075;font-size:11px;font-variant-numeric:tabular-nums}
.chart-tip{position:absolute;pointer-events:none;z-index:3;background:#203e2e;color:white;border-radius:10px;padding:10px 12px;width:200px;box-shadow:0 8px 24px #10201820;display:grid;gap:5px;font-size:12px}.chart-tip strong{font-size:20px}.chart-tip small{color:#d0dece;font-size:10px}
footer{display:flex;gap:14px;margin:10px 0;flex-wrap:wrap}.legend:before{content:'';display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;background:#297653}.partial:before{background:#c49b4b}
summary{cursor:pointer;width:fit-content}.chart-table{max-height:230px;overflow:auto}table{font-size:12px;width:100%;border-collapse:collapse}th,td{padding:8px;text-align:left;border-bottom:1px solid var(--line)}.chart-empty{padding:80px 0;text-align:center;color:var(--muted)}
@media(max-width:600px){.home-chart{padding:14px}header{display:block}header span{display:block;margin-top:-10px;margin-bottom:12px}}
</style>
