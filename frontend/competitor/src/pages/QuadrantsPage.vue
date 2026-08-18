<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { fetchQuadrants } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { matchesProductSearch } from "../productSearch";
import type { OwnStoreScope, QuadrantItem, QuadrantPayload } from "../types";

const props = defineProps<{
  asOf: string;
  storeScope?: OwnStoreScope;
  multiStoreLabel?: string;
}>();
const data = ref<QuadrantPayload | null>(null);
const loading = ref(true);
const copiedOfferId = ref("");
const copyFeedback = ref("");
const copyFeedbackKind = ref<"success" | "error">("success");
const hoveredItem = ref<QuadrantItem | null>(null);
const failedImageUrls = ref<Set<string>>(new Set());
const skuQuery = ref("");
const productSort = ref<"views_desc" | "orders_desc" | "stock_desc" | "name_asc">(
  "views_desc",
);
let feedbackTimer: ReturnType<typeof setTimeout> | undefined;
let loadRequestRevision = 0;

const rankTicks = [0, 25, 50, 75, 100];
const gridTicks = rankTicks.filter((tick) => tick > 0 && tick < 100);
const allItems = computed(() => data.value?.items ?? []);
const filteredItems = computed(() => {
  if (!skuQuery.value.trim()) return allItems.value;
  return allItems.value.filter((item) => matchesProductSearch(
    {
      productNames: [item.title, item.company_product_name],
      otherValues: [item.sku, item.company_sku, item.store_name, item.store_code],
    },
    skuQuery.value,
  ));
});
const sortedItems = computed(() => {
  const items = [...filteredItems.value];
  return items.sort((left, right) => {
    if (productSort.value === "name_asc") {
      return productName(left).localeCompare(productName(right), "zh-CN", {
        numeric: true,
      });
    }

    const key =
      productSort.value === "orders_desc"
        ? "ordered_units"
        : productSort.value === "stock_desc"
          ? "total_stock"
          : "page_views_30_days";
    return (
      compareNullableDesc(left[key], right[key]) ||
      productName(left).localeCompare(productName(right), "zh-CN", {
        numeric: true,
      })
    );
  });
});
const plottableItems = computed(() =>
  allItems.value.filter(
    (item) =>
      item.page_views_rank !== null &&
      item.ordered_units_rank !== null,
  ),
);
const missingCoordinateCount = computed(
  () => allItems.value.length - plottableItems.value.length,
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
    below: y >= 50,
  };
});
const hoveredImageUrl = computed(() => {
  return hoveredItem.value ? imageUrl(hoveredItem.value) : "";
});

watch([() => props.asOf, () => props.storeScope], load, { immediate: true });

async function load() {
  const requestRevision = ++loadRequestRevision;
  const requestedAsOf = props.asOf;
  const requestedStoreScope = props.storeScope ?? "current";
  loading.value = true;
  try {
    const nextData = await fetchQuadrants(
      requestedAsOf,
      50,
      requestedStoreScope,
    );
    if (
      requestRevision !== loadRequestRevision
      || requestedAsOf !== props.asOf
      || requestedStoreScope !== (props.storeScope ?? "current")
    ) return;
    data.value = nextData;
  } finally {
    if (requestRevision === loadRequestRevision) loading.value = false;
  }
}

function position(value: number | null) {
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

function productName(item: QuadrantItem) {
  return item.title || item.sku || item.offer_id;
}

function itemKey(item: QuadrantItem) {
  return item.store_scope_key || `${item.store_code || "current"}:${item.offer_id}`;
}

function compareNullableDesc(
  left: number | null | undefined,
  right: number | null | undefined,
) {
  if (left === null || left === undefined) {
    return right === null || right === undefined ? 0 : 1;
  }
  if (right === null || right === undefined) return -1;
  return right - left;
}

function firstListingLabel(item: QuadrantItem) {
  return item.first_listed_at || "暂无记录";
}

function firstListingTitle(item: QuadrantItem) {
  return item.first_listed_source === "platform"
    ? "首次上架"
    : "首次上架 · 本库最早记录";
}

function restockLabel(item: QuadrantItem) {
  if (!item.latest_restock_date) return "暂无平台库存增加记录";
  const increase =
    item.latest_restock_increase === null
      ? ""
      : ` · 较前次 +${number(item.latest_restock_increase)}`;
  return `${item.latest_restock_date}${increase}`;
}

function sourceImageUrl(item: QuadrantItem) {
  return String(item.image_url ?? "").trim();
}

function imageUrl(item: QuadrantItem) {
  const source = sourceImageUrl(item);
  return source && !failedImageUrls.value.has(source)
    ? productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list)
    : "";
}

function imageFailed(item: QuadrantItem) {
  const source = sourceImageUrl(item);
  return !source || failedImageUrls.value.has(source);
}

function markImageUnavailable(url: string) {
  if (!url) return;
  const failed = new Set(failedImageUrls.value);
  failed.add(url);
  failedImageUrls.value = failed;
}

async function copyPlatformSku(item: QuadrantItem) {
  const sku = String(item.sku ?? "").trim();
  if (!sku) {
    showCopyFeedback("该商品没有平台 SKU，未复制。", "error");
    return;
  }
  try {
    await writeClipboard(sku);
    copiedOfferId.value = itemKey(item);
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
        <p class="section-kicker">PRODUCT POSITION</p>
        <h2>用近30日下单与近30天浏览量查看商品分布</h2>
      </div>
    </div>

    <div v-if="loading" class="state-card">正在计算商品经营坐标……</div>
    <template v-else-if="data">
      <section class="erp-panel quadrant-visual">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">TWO-DIMENSIONAL POSITION</p>
            <h3>商品经营坐标</h3>
          </div>
          <span>
            已定位 {{ plottableItems.length }} 个 ·
            缺少坐标 {{ missingCoordinateCount }} 个保留在下表
          </span>
        </div>
        <div class="matrix-shell">
          <span
            class="matrix-axis-title y"
            aria-label="纵轴：近30日下单件数相对排名，数值由下向上增大"
          >
            近30日下单件数相对排名
          </span>
          <div class="matrix">
          <template v-for="tick in gridTicks" :key="`grid-${tick}`">
            <span
              class="matrix-grid-line vertical"
              :style="{ left: `${tick}%` }"
              aria-hidden="true"
            ></span>
            <span
              class="matrix-grid-line horizontal"
              :style="{ bottom: `${tick}%` }"
              aria-hidden="true"
            ></span>
          </template>
          <button
            v-for="item in plottableItems"
            :key="itemKey(item)"
            class="matrix-dot coordinate"
            :class="[
              {
                copied: copiedOfferId === itemKey(item),
                'missing-sku': !item.sku,
              },
            ]"
            :style="{
              left: position(item.page_views_rank),
              bottom: position(item.ordered_units_rank),
            }"
            :aria-label="`复制平台 SKU ${item.sku || '缺失'}：${item.title || item.offer_id}`"
            :aria-describedby="hoveredItem && itemKey(hoveredItem) === itemKey(item) ? 'quadrant-tooltip' : undefined"
            @mouseenter="hoveredItem = item"
            @mouseleave="hoveredItem = null"
            @focus="hoveredItem = item"
            @blur="hoveredItem = null"
            @click="copyPlatformSku(item)"
          ></button>
          </div>
          <div class="matrix-rank-axis x" aria-hidden="true">
            <span
              v-for="tick in rankTicks"
              :key="`x-${tick}`"
              class="matrix-rank-tick"
              :class="{ start: tick === 0, end: tick === 100 }"
              :style="{ left: `${tick}%` }"
            >
              {{ tick }}
            </span>
          </div>
          <div class="matrix-rank-axis y" aria-hidden="true">
            <span
              v-for="tick in rankTicks"
              :key="`y-${tick}`"
              class="matrix-rank-tick"
              :class="{ start: tick === 0, end: tick === 100 }"
              :style="{ bottom: `${tick}%` }"
            >
              {{ tick }}
            </span>
          </div>
          <div class="matrix-tooltip-layer">
          <aside
            v-if="hoveredItem"
            id="quadrant-tooltip"
            class="matrix-tooltip"
            :class="tooltipClasses"
            :style="tooltipStyle"
            role="tooltip"
          >
            <div class="tooltip-heading">
              <span class="coordinate-tag">商品坐标</span>
              <small>点击小点复制 SKU</small>
            </div>
            <div class="tooltip-product-summary">
              <div class="tooltip-product-image">
                <img
                  v-if="hoveredImageUrl"
                  :src="hoveredImageUrl"
                  :alt="`${hoveredItem.title || hoveredItem.sku || hoveredItem.offer_id} 商品图片`"
                  width="192"
                  height="192"
                  decoding="async"
                  fetchpriority="high"
                  referrerpolicy="no-referrer"
                  @error="markImageUnavailable(sourceImageUrl(hoveredItem))"
                />
                <span v-else>暂无图片</span>
              </div>
              <div class="tooltip-product-copy">
                <strong>{{ hoveredItem.title || hoveredItem.sku || hoveredItem.offer_id }}</strong>
                <div v-if="props.storeScope !== 'current'" class="tooltip-sku">
                  <span>所属店铺</span>
                  <b>{{ hoveredItem.store_name || hoveredItem.store_code || "—" }}</b>
                </div>
                <div class="tooltip-sku">
                  <span>平台 SKU</span>
                  <b>{{ hoveredItem.sku || "缺失" }}</b>
                </div>
                <div class="tooltip-sku">
                  <span>公司 SKU</span>
                  <b>{{ hoveredItem.company_sku || "未关联" }}</b>
                </div>
              </div>
            </div>
            <div class="tooltip-stats">
              <span>
                <small>近30日下单</small>
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
                <small>{{ firstListingTitle(hoveredItem) }}</small>
                <b>{{ firstListingLabel(hoveredItem) }}</b>
              </span>
              <span>
                <small>最近补货时间 · 平台库存增加记录</small>
                <b>{{ restockLabel(hoveredItem) }}</b>
              </span>
              <em>
                库存和指标截至 {{ hoveredItem.metric_date }}；浏览量为平台明确返回的近30天窗口值。
              </em>
            </div>
          </aside>
          </div>
          <span class="matrix-axis-title x">
            横轴 · 近30天浏览量相对排名
          </span>
        </div>
        <p class="method-note">
          图中只表达两个维度在当前店铺范围内的相对位置，不再划分四象限或使用分位边界。
          缺少任一坐标的数据不会按0处理，仍完整保留在下方商品卡片；悬停可查看完整经营信息，点击小点直接复制平台 SKU。
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
            <h3>全部商品</h3>
          </div>
          <div class="coordinate-list-actions">
            <span>
              {{ sortedItems.length }} / {{ allItems.length }} 个商品 ·
              完整展示，无需左右滑动
            </span>
            <label class="compact-field coordinate-search-field">
              <span>搜索商品名称 / 平台 / 公司 SKU</span>
              <input
                v-model="skuQuery"
                type="search"
                placeholder="商品名称支持模糊搜索，也可输入完整或部分平台 / 公司 SKU"
                aria-label="搜索商品名称、平台或公司 SKU"
                autocomplete="off"
              />
            </label>
            <label class="compact-field coordinate-sort-field">
              <span>排序方式</span>
              <select v-model="productSort" aria-label="全部商品排序方式">
                <option value="views_desc">近30天浏览量从高到低</option>
                <option value="orders_desc">近30日下单从高到低</option>
                <option value="stock_desc">平台库存从高到低</option>
                <option value="name_asc">商品名称 A–Z</option>
              </select>
            </label>
          </div>
        </div>
        <div v-if="sortedItems.length" class="coordinate-product-grid">
          <article
            v-for="item in sortedItems"
            :key="itemKey(item)"
            v-memo="[item, imageFailed(item)]"
            class="coordinate-product-card"
          >
            <div class="coordinate-product-card-head">
              <div class="coordinate-product-image">
                <img
                  v-if="imageUrl(item)"
                  :src="imageUrl(item)"
                  :alt="`${productName(item)} 商品图片`"
                  width="192"
                  height="192"
                  loading="lazy"
                  decoding="async"
                  fetchpriority="low"
                  referrerpolicy="no-referrer"
                  @error="markImageUnavailable(sourceImageUrl(item))"
                />
                <span v-else>暂无图片</span>
              </div>
              <div class="coordinate-product-card-title">
                <strong>{{ productName(item) }}</strong>
                <template v-if="props.storeScope !== 'current'">
                  <span>所属店铺</span>
                  <b>{{ item.store_name || item.store_code || "—" }}</b>
                </template>
                <span>平台 SKU</span>
                <b class="mono-value">{{ item.sku || "—" }}</b>
                <span>公司 SKU</span>
                <b class="mono-value">{{ item.company_sku || "未关联" }}</b>
              </div>
            </div>
            <dl class="coordinate-product-metrics">
              <div>
                <dt>近30天浏览量</dt>
                <dd>{{ number(item.page_views_30_days) }}</dd>
              </div>
              <div>
                <dt>近30日下单</dt>
                <dd>{{ number(item.ordered_units) }}</dd>
              </div>
              <div>
                <dt>平台可售库存</dt>
                <dd>{{ number(item.total_stock) }}</dd>
              </div>
              <div class="wide">
                <dt>{{ firstListingTitle(item) }}</dt>
                <dd>{{ firstListingLabel(item) }}</dd>
              </div>
              <div class="wide">
                <dt>最近补货时间 · 平台库存增加记录</dt>
                <dd>{{ restockLabel(item) }}</dd>
              </div>
              <div class="wide">
                <dt>数据截止日期</dt>
                <dd>{{ item.metric_date || "—" }}</dd>
              </div>
            </dl>
          </article>
        </div>
        <div v-else class="state-card coordinate-empty-state">
          未找到匹配“{{ skuQuery.trim() }}”的平台或公司 SKU
        </div>
      </section>
    </template>
  </div>
</template>
