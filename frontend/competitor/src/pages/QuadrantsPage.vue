<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { fetchQuadrants } from "../api";
import type { QuadrantItem, QuadrantKey, QuadrantPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const percentile = ref(50);
const data = ref<QuadrantPayload | null>(null);
const loading = ref(true);
const selectedQuadrant = ref<QuadrantKey | "all">("all");
const copiedOfferId = ref("");
const copyFeedback = ref("");
const copyFeedbackKind = ref<"success" | "error">("success");
const hoveredItem = ref<QuadrantItem | null>(null);
let feedbackTimer: ReturnType<typeof setTimeout> | undefined;

const labels: Record<QuadrantKey, string> = {
  star: "明星商品",
  conversion_issue: "转化问题",
  potential: "潜力商品",
  optimize: "待优化",
  unclassified: "未分类",
};
const filtered = computed(() =>
  selectedQuadrant.value === "all"
    ? data.value?.items ?? []
    : (data.value?.items ?? []).filter(
        (item) => item.quadrant === selectedQuadrant.value,
      ),
);
const tooltipStyle = computed(() => {
  if (!hoveredItem.value) return {};
  const x = rankValue(hoveredItem.value.page_views_rank);
  const y = rankValue(hoveredItem.value.ordered_units_rank);
  return {
    left: `${x}%`,
    top: `${100 - y}%`,
  };
});
const tooltipClasses = computed(() => {
  if (!hoveredItem.value) return {};
  const x = rankValue(hoveredItem.value.page_views_rank);
  const y = rankValue(hoveredItem.value.ordered_units_rank);
  return {
    "align-left": x < 24,
    "align-right": x > 76,
    below: y > 72,
  };
});

watch([() => props.asOf, percentile], load, { immediate: true });

async function load() {
  loading.value = true;
  try {
    data.value = await fetchQuadrants(props.asOf, percentile.value);
  } finally {
    loading.value = false;
  }
}

function position(value: number | null) {
  return `${rankValue(value)}%`;
}

function boundaryPosition(value: number | null) {
  return `${rankValue(value)}%`;
}

function rankValue(value: number | null) {
  return Math.min(98, Math.max(2, value ?? 50));
}

function number(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat("zh-CN").format(value);
}

function firstListingLabel(item: QuadrantItem) {
  return item.first_listed_at || "暂无记录";
}

function restockLabel(item: QuadrantItem) {
  if (!item.latest_restock_date) return "暂未观察到库存上升";
  const increase =
    item.latest_restock_increase === null
      ? ""
      : ` · 较前次 +${number(item.latest_restock_increase)}`;
  return `${item.latest_restock_date}${increase}`;
}

async function copyPlatformSku(item: QuadrantItem) {
  const sku = String(item.sku ?? "").trim();
  if (!sku) {
    showCopyFeedback("该商品没有平台 SKU，未复制。", "error");
    return;
  }
  try {
    await writeClipboard(sku);
    copiedOfferId.value = item.offer_id;
    showCopyFeedback(`已复制平台 SKU：${sku}`, "success");
  } catch {
    copiedOfferId.value = "";
    showCopyFeedback("复制失败，请稍后重试。", "error");
  }
}

async function writeClipboard(value: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall back to the browser's local copy command when clipboard permission is unavailable.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

function showCopyFeedback(
  message: string,
  kind: "success" | "error",
) {
  if (feedbackTimer) clearTimeout(feedbackTimer);
  copyFeedback.value = message;
  copyFeedbackKind.value = kind;
  feedbackTimer = setTimeout(() => {
    copyFeedback.value = "";
    copiedOfferId.value = "";
  }, 3200);
}

onBeforeUnmount(() => {
  if (feedbackTimer) clearTimeout(feedbackTimer);
});
</script>

<template>
  <div class="erp-page quadrant-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">PORTFOLIO MATRIX</p>
        <h2>用近7日销量与30天浏览量管理商品组合</h2>
      </div>
      <label class="compact-field">
        <span>分组严格程度</span>
        <select v-model="percentile">
          <option :value="25">宽松 · 25分位</option>
          <option :value="50">标准 · 50分位</option>
          <option :value="75">严格 · 75分位</option>
        </select>
      </label>
    </div>

    <div v-if="loading" class="state-card">正在计算经营四象限……</div>
    <template v-else-if="data">
      <section class="quadrant-kpis">
        <button
          v-for="key in (Object.keys(labels) as QuadrantKey[])"
          :key="key"
          :class="[key, { active: selectedQuadrant === key }]"
          @click="selectedQuadrant = selectedQuadrant === key ? 'all' : key"
        >
          <span>{{ labels[key] }}</span>
          <strong>{{ data.counts[key] }}</strong>
        </button>
      </section>

      <section class="erp-panel quadrant-visual">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">RELATIVE POSITION</p>
            <h3>商品经营位置</h3>
          </div>
          <span>
            浏览量分界 {{ number(data.boundaries.page_views) }} ·
            近7日下单分界 {{ number(data.boundaries.ordered_units) }}
          </span>
        </div>
        <div class="matrix-shell">
          <span class="matrix-axis-title y">
            纵轴 · 近7日下单件数相对排名（低 → 高）
          </span>
          <div class="matrix">
          <div class="matrix-zone top-left">潜力商品</div>
          <div class="matrix-zone top-right">明星商品</div>
          <div class="matrix-zone bottom-left">待优化</div>
          <div class="matrix-zone bottom-right">转化问题</div>
          <span
            class="axis-y"
            :style="{ left: boundaryPosition(data.boundaries.page_views_rank) }"
          >
            下单分界
          </span>
          <span
            class="axis-x"
            :style="{ bottom: boundaryPosition(data.boundaries.ordered_units_rank) }"
          >
            流量分界
          </span>
          <span
            class="matrix-divider vertical"
            :style="{ left: boundaryPosition(data.boundaries.page_views_rank) }"
          ></span>
          <span
            class="matrix-divider horizontal"
            :style="{ bottom: boundaryPosition(data.boundaries.ordered_units_rank) }"
          ></span>
          <span
            class="matrix-center"
            :style="{
              left: boundaryPosition(data.boundaries.page_views_rank),
              bottom: boundaryPosition(data.boundaries.ordered_units_rank),
            }"
            aria-hidden="true"
          ></span>
          <button
            v-for="item in data.items.filter((row) => row.quadrant !== 'unclassified')"
            :key="item.offer_id"
            class="matrix-dot"
            :class="[
              item.quadrant,
              {
                copied: copiedOfferId === item.offer_id,
                'missing-sku': !item.sku,
              },
            ]"
            :style="{
              left: position(item.page_views_rank),
              bottom: position(item.ordered_units_rank),
            }"
            :aria-label="`复制平台 SKU ${item.sku || '缺失'}：${item.title || item.offer_id}`"
            :aria-describedby="hoveredItem?.offer_id === item.offer_id ? 'quadrant-tooltip' : undefined"
            @mouseenter="hoveredItem = item"
            @mouseleave="hoveredItem = null"
            @focus="hoveredItem = item"
            @blur="hoveredItem = null"
            @click="copyPlatformSku(item)"
          ></button>
          <aside
            v-if="hoveredItem"
            id="quadrant-tooltip"
            class="matrix-tooltip"
            :class="tooltipClasses"
            :style="tooltipStyle"
            role="tooltip"
          >
            <div class="tooltip-heading">
              <span class="quadrant-tag" :class="hoveredItem.quadrant">
                {{ labels[hoveredItem.quadrant] }}
              </span>
              <small>点击小点复制 SKU</small>
            </div>
            <strong>{{ hoveredItem.title || hoveredItem.sku || hoveredItem.offer_id }}</strong>
            <div class="tooltip-sku">
              <span>平台 SKU</span>
              <b>{{ hoveredItem.sku || "缺失" }}</b>
            </div>
            <div class="tooltip-stats">
              <span>
                <small>近7日流量参考 · 估算</small>
                <b>{{ number(hoveredItem.page_views_7_day_estimate) }}</b>
              </span>
              <span>
                <small>近7日下单</small>
                <b>{{ number(hoveredItem.ordered_units) }}</b>
              </span>
              <span>
                <small>平台可售库存</small>
                <b>{{ number(hoveredItem.total_stock) }}</b>
              </span>
              <span>
                <small>近30天浏览量</small>
                <b>{{ number(hoveredItem.page_views_30_days) }}</b>
              </span>
            </div>
            <div class="tooltip-timeline">
              <span>
                <small>
                  {{
                    hoveredItem.first_listed_source === "platform"
                      ? "首次上架"
                      : "首次上架 · 本库最早记录"
                  }}
                </small>
                <b>{{ firstListingLabel(hoveredItem) }}</b>
              </span>
              <span>
                <small>最近补货 · 按库存上升估算</small>
                <b>{{ restockLabel(hoveredItem) }}</b>
              </span>
              <em>
                库存和流量截至 {{ hoveredItem.metric_date }}；7日流量参考按近30日均值 × 7 估算。
              </em>
            </div>
          </aside>
          </div>
          <span class="matrix-axis-title x">
            横轴 · 近30天浏览量相对排名（低 → 高）
          </span>
        </div>
        <p class="method-note">
          图中位置使用店铺内相对排名拉开差异；十字中心跟随分组严格程度移动。
          悬停可查看首次上架、补货估算、库存及流量时效信息，点击小点直接复制平台 SKU。
        </p>
        <p
          v-if="copyFeedback"
          class="copy-feedback"
          :class="copyFeedbackKind"
          role="status"
          aria-live="polite"
        >
          {{ copyFeedback }}
        </p>
      </section>

      <section class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">PRODUCT ACTION LIST</p>
            <h3>{{ selectedQuadrant === "all" ? "全部商品" : labels[selectedQuadrant] }}</h3>
          </div>
          <button
            v-if="selectedQuadrant !== 'all'"
            class="quiet-button"
            @click="selectedQuadrant = 'all'"
          >
            清除筛选
          </button>
        </div>
        <div class="erp-table-wrap">
          <table class="erp-table">
            <thead>
              <tr><th>商品</th><th>分类</th><th>近30天浏览量</th><th>近7日下单</th><th>库存</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in filtered" :key="item.offer_id">
                <td><strong>{{ item.title || item.sku || item.offer_id }}</strong><small>{{ item.sku || "—" }}</small></td>
                <td><span class="quadrant-tag" :class="item.quadrant">{{ labels[item.quadrant] }}</span></td>
                <td>{{ number(item.page_views_30_days) }}</td>
                <td>{{ number(item.ordered_units) }}</td>
                <td>{{ number(item.total_stock) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
