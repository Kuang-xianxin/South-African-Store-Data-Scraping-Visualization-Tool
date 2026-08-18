<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchProductDetail, fetchProducts } from "../api";
import {
  PRODUCT_IMAGE_SIZE,
  productThumbnailUrl,
  type ProductImageSize,
} from "../productImages";
import { matchesProductSearch } from "../productSearch";
import { formatChinaDateTime } from "../time";
import type {
  OwnStoreScope,
  ProductDetailPayload,
  ProductItem,
  ProductsPayload,
} from "../types";

const props = defineProps<{
  asOf: string;
  storeScope?: OwnStoreScope;
  multiStoreLabel?: string;
}>();
const payload = ref<ProductsPayload | null>(null);
const detail = ref<ProductDetailPayload | null>(null);
const selectedKey = ref("");
const query = ref("");
const loading = ref(true);
const detailLoading = ref(false);
const error = ref("");
const detailError = ref("");
const detailModalOpen = ref(false);
const failedImageUrls = ref<Set<string>>(new Set());
let returnFocusElement: HTMLElement | null = null;
let productsRequestRevision = 0;
let detailRequestRevision = 0;

const filtered = computed(() => {
  if (!query.value.trim()) return payload.value?.items ?? [];
  return (payload.value?.items ?? []).filter((item) => matchesProductSearch(
    {
      productNames: [item.title, item.company_product_name],
      otherValues: [
        item.offer_id,
        item.sku,
        item.company_sku,
        item.store_name,
        item.store_code,
        item.tsin_id,
        item.barcode,
      ],
    },
    query.value,
  ));
});
const selected = computed(
  () => payload.value?.items.find((item) => itemKey(item) === selectedKey.value) ?? null,
);
const selectedImageUrl = computed(() =>
  imageUrl(selected.value, PRODUCT_IMAGE_SIZE.detail),
);
const salesAxis = computed(() => {
  const maximumValue = Math.max(
    0,
    ...(detail.value?.history.map((item) => item.ordered_units ?? 0) ?? [0]),
  );
  const step = Math.max(1, Math.ceil(maximumValue / 4));
  const maximum = Math.max(step, Math.ceil(maximumValue / step) * step);
  const ticks: number[] = [];
  for (let value = maximum; value >= 0; value -= step) ticks.push(value);
  return { maximum, ticks };
});

watch(
  [() => props.asOf, () => props.storeScope],
  loadProducts,
  { immediate: true },
);
watch(detailModalOpen, (open) => {
  document.body.style.overflow = open ? "hidden" : "";
});

onMounted(() => {
  window.addEventListener("keydown", handleWindowKeydown);
});

onBeforeUnmount(() => {
  productsRequestRevision += 1;
  detailRequestRevision += 1;
  window.removeEventListener("keydown", handleWindowKeydown);
  document.body.style.overflow = "";
});

async function loadProducts() {
  const requestRevision = ++productsRequestRevision;
  const requestedAsOf = props.asOf;
  const requestedStoreScope = props.storeScope ?? "current";
  detailRequestRevision += 1;
  detailLoading.value = false;
  loading.value = true;
  error.value = "";
  try {
    const nextPayload = await fetchProducts(requestedAsOf, requestedStoreScope);
    if (
      requestRevision !== productsRequestRevision
      || requestedAsOf !== props.asOf
      || requestedStoreScope !== (props.storeScope ?? "current")
    ) return;
    payload.value = nextPayload;
    if (
      selectedKey.value &&
      !payload.value.items.some((item) => itemKey(item) === selectedKey.value)
    ) {
      closeProductDetail();
      selectedKey.value = "";
    }
  } catch (reason) {
    if (requestRevision !== productsRequestRevision) return;
    error.value = reason instanceof Error ? reason.message : "商品数据读取失败";
  } finally {
    if (requestRevision === productsRequestRevision) loading.value = false;
  }
}

async function loadDetail() {
  if (!selected.value) {
    detail.value = null;
    return;
  }
  const requestRevision = ++detailRequestRevision;
  const requestedKey = selectedKey.value;
  const requestedAsOf = props.asOf;
  const requestedStoreCode = selected.value.store_code;
  detailLoading.value = true;
  detailError.value = "";
  try {
    const nextDetail = await fetchProductDetail(
      selected.value.offer_id,
      requestedAsOf,
      requestedStoreCode,
    );
    if (
      requestRevision !== detailRequestRevision
      || selectedKey.value !== requestedKey
      || props.asOf !== requestedAsOf
    ) return;
    detail.value = nextDetail;
  } catch (reason) {
    if (requestRevision !== detailRequestRevision) return;
    detail.value = null;
    detailError.value = reason instanceof Error ? reason.message : "商品详情读取失败";
  } finally {
    if (requestRevision === detailRequestRevision) detailLoading.value = false;
  }
}

function openProductDetail(item: ProductItem, event?: Event) {
  returnFocusElement =
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  selectedKey.value = itemKey(item);
  detail.value = null;
  detailModalOpen.value = true;
  void loadDetail();
}

function itemKey(item: ProductItem) {
  return item.store_scope_key || `${item.store_code || "current"}:${item.offer_id}`;
}

function closeProductDetail() {
  detailRequestRevision += 1;
  detailLoading.value = false;
  detailModalOpen.value = false;
  const target = returnFocusElement;
  returnFocusElement = null;
  if (target) void nextTick(() => target.focus());
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && detailModalOpen.value) closeProductDetail();
}

function imageUrl(
  item: ProductItem | null,
  size: ProductImageSize = PRODUCT_IMAGE_SIZE.list,
) {
  const url = String(item?.image_url ?? "").trim();
  return url && !failedImageUrls.value.has(url)
    ? productThumbnailUrl(url, size)
    : "";
}

function markImageUnavailable(url: string | null | undefined) {
  const normalized = String(url ?? "").trim();
  if (!normalized) return;
  const failed = new Set(failedImageUrls.value);
  failed.add(normalized);
  failedImageUrls.value = failed;
}

function number(value: unknown) {
  return typeof value === "number"
    ? new Intl.NumberFormat("zh-CN").format(value)
    : "—";
}

function currency(value: unknown) {
  return typeof value === "number"
    ? new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value)
    : "—";
}

function rmbCost(value: unknown) {
  return typeof value === "number"
    ? `¥${new Intl.NumberFormat("zh-CN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }).format(value)}`
    : "—";
}

function exchangeRate(value: unknown) {
  return typeof value === "number" ? value.toFixed(4) : "—";
}

function percent(value: unknown) {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "—";
}

function salesAxisPosition(value: number) {
  return `${100 - (value / salesAxis.value.maximum) * 100}%`;
}

function salesBarHeight(value: number | null) {
  return `${(Math.max(0, value ?? 0) / salesAxis.value.maximum) * 100}%`;
}
</script>

<template>
  <div class="erp-page products-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">PRODUCT COMMAND CENTER</p>
        <h2>店铺商品明细表</h2>
      </div>
      <p>
        {{ props.storeScope === "current" ? "点击商品查看每日销售件数快照。" : `${props.multiStoreLabel || "全部店铺"}合并查看；每条记录保留所属店铺。` }}
      </p>
    </div>
    <div class="product-workspace">
      <section class="product-list erp-panel">
        <div class="search-box">
          <input
            v-model="query"
            type="search"
            placeholder="商品名称支持模糊搜索，也可输入平台 SKU、公司 SKU 或条码"
          />
          <span>{{ filtered.length }} 个商品</span>
        </div>
        <div v-if="loading" class="state-card slim">正在读取商品……</div>
        <p v-else-if="error" class="state-card error">{{ error }}</p>
        <div v-else class="product-scroll">
          <button
            v-for="item in filtered"
            :key="itemKey(item)"
            v-memo="[item, failedImageUrls.has(String(item.image_url ?? '').trim()), selectedKey === itemKey(item)]"
            :class="{ active: selectedKey === itemKey(item) }"
            type="button"
            aria-haspopup="dialog"
            :aria-label="`查看 ${item.title || item.sku || item.offer_id} 的商品详情`"
            @click="openProductDetail(item, $event)"
          >
            <span class="product-list-image">
              <img
                v-if="imageUrl(item)"
                :src="imageUrl(item)"
                :alt="`${item.title || item.sku || item.offer_id} 商品图片`"
                width="192"
                height="192"
                loading="lazy"
                decoding="async"
                referrerpolicy="no-referrer"
                @error="markImageUnavailable(item.image_url)"
              />
              <span v-else>暂无图片</span>
            </span>
            <span class="product-list-copy">
              <strong>{{ item.title || item.sku || item.offer_id }}</strong>
              <span v-if="props.storeScope !== 'current'">店铺 {{ item.store_name || item.store_code || "—" }}</span>
              <span>平台 {{ item.sku || "无库存编码" }} · {{ item.status_label || "状态未知" }}</span>
              <span>公司 SKU {{ item.company_sku || "未关联" }}</span>
              <small>
                下单 {{ number(item.ordered_units) }} · 浏览
                {{ number(item.page_views_30_days) }}
              </small>
            </span>
          </button>
          <p v-if="filtered.length === 0" class="state-card slim">
            没有找到匹配的商品。
          </p>
        </div>
      </section>
    </div>

    <Teleport to="body">
      <div
        v-if="detailModalOpen && selected"
        class="competitor-modal-backdrop product-modal-backdrop"
        @click.self="closeProductDetail"
      >
        <section
          class="competitor-modal product-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="product-detail-title"
        >
          <header class="competitor-modal-header">
            <div>
              <p class="section-kicker">PRODUCT DETAIL</p>
              <h2 id="product-detail-title">
                {{ selected.title || selected.sku || selected.offer_id }}
              </h2>
              <span>
                <template v-if="props.storeScope !== 'current'">店铺 {{ selected.store_name || selected.store_code || "—" }} · </template>
                平台 SKU {{ selected.sku || "—" }} · 公司 SKU {{ selected.company_sku || "未关联" }} · 商品编号 {{ selected.offer_id }} ·
                平台商品编号 {{ selected.tsin_id || "—" }}
              </span>
            </div>
            <button
              type="button"
              class="competitor-modal-close"
              aria-label="关闭商品详情"
              @click="closeProductDetail"
            >
              ×
            </button>
          </header>

          <div class="product-modal-content">
            <section class="product-modal-hero">
              <div class="product-modal-image">
                <img
                  v-if="selectedImageUrl"
                  :src="selectedImageUrl"
                  :alt="`${selected.title || selected.sku || selected.offer_id} 商品图片`"
                  width="640"
                  height="640"
                  decoding="async"
                  fetchpriority="high"
                  referrerpolicy="no-referrer"
                  @error="markImageUnavailable(selected.image_url)"
                />
                <span v-else>暂无图片</span>
              </div>
              <div class="product-modal-identity">
                <span class="status-badge">{{ selected.status_label || "状态未知" }}</span>
                <h3>{{ selected.title || selected.sku || selected.offer_id }}</h3>
                <dl>
                  <div><dt>平台 SKU</dt><dd>{{ selected.sku || "缺失" }}</dd></div>
                  <div><dt>公司 SKU</dt><dd>{{ selected.company_sku || "未关联" }}</dd></div>
                  <div v-if="props.storeScope !== 'current'"><dt>所属店铺</dt><dd>{{ selected.store_name || selected.store_code || "—" }}</dd></div>
                  <div><dt>商品编号</dt><dd>{{ selected.offer_id }}</dd></div>
                  <div><dt>平台商品编号</dt><dd>{{ selected.tsin_id || "—" }}</dd></div>
                  <div><dt>条码</dt><dd>{{ selected.barcode || "—" }}</dd></div>
                </dl>
              </div>
            </section>

            <p v-if="detailError" class="state-card error product-modal-state">
              {{ detailError }}
              <button type="button" @click="loadDetail">重新读取</button>
            </p>
            <div v-else-if="detailLoading" class="state-card product-modal-state">
              正在读取商品详情……
            </div>
            <template v-else>
              <section class="mini-kpis" aria-label="商品经营指标">
                <article>
                  <span>当前售价</span>
                  <strong>{{ currency(selected.selling_price) }}</strong>
                </article>
                <article
                  class="product-cost-kpi"
                  :class="{ stale: detail?.cost_conversion.status === 'stale' }"
                >
                  <span>单件成本（兰特）</span>
                  <strong>{{ currency(detail?.cost_conversion.cost_zar) }}</strong>
                  <small v-if="typeof detail?.cost_conversion.cost_rmb === 'number'">
                    {{ rmbCost(detail?.cost_conversion.cost_rmb) }} ×
                    {{ exchangeRate(detail?.cost_conversion.rate) }}
                  </small>
                  <small v-if="detail?.cost_conversion.rate_date">
                    1人民币={{ exchangeRate(detail.cost_conversion.rate) }}兰特 ·
                    汇率日 {{ detail.cost_conversion.rate_date }}
                  </small>
                  <small v-if="detail?.cost_conversion.fetched_at">
                    {{ detail.cost_conversion.source }} ·
                    北京时间 {{ formatChinaDateTime(detail.cost_conversion.fetched_at) }} 获取
                  </small>
                  <small v-if="detail?.cost_conversion.message">
                    {{ detail.cost_conversion.message }}
                  </small>
                </article>
                <article>
                  <span>最新日下单</span>
                  <strong>{{ number(detail?.kpis.latest_ordered_units) }}</strong>
                </article>
                <article>
                  <span>近7日下单</span>
                  <strong>{{ number(detail?.kpis.seven_day_ordered_units) }}</strong>
                </article>
                <article>
                  <span>平台可售库存</span>
                  <strong>{{ number(selected.total_stock) }}</strong>
                </article>
                <article>
                  <span>近30天浏览量</span>
                  <strong>{{ number(detail?.kpis.page_views_30_days) }}</strong>
                </article>
                <article>
                  <span>近30天转化率</span>
                  <strong>{{ percent(detail?.kpis.conversion_percentage_30_days) }}</strong>
                </article>
              </section>

              <article class="erp-panel sales-history-panel">
                <div class="panel-heading">
                  <div>
                    <p class="section-kicker">PRODUCT HISTORY</p>
                    <h3>销售件数快照</h3>
                  </div>
                  <span>数据截止 {{ detail?.kpis.latest_metric_date || "—" }}</span>
                </div>
                <div
                  v-if="detail?.history.length"
                  class="sales-history-chart"
                  role="img"
                  :aria-label="`每日销售件数柱状图，纵轴范围 0 到 ${salesAxis.maximum} 件`"
                >
                  <div class="sales-history-axis-title">件数</div>
                  <div class="sales-history-visual">
                    <div class="sales-history-axis" aria-hidden="true">
                      <span
                        v-for="tick in salesAxis.ticks"
                        :key="`axis-${tick}`"
                        :style="{ top: salesAxisPosition(tick) }"
                      >
                        {{ tick }}
                      </span>
                    </div>
                    <div class="sales-history-plot">
                      <span
                        v-for="tick in salesAxis.ticks"
                        :key="`grid-${tick}`"
                        class="sales-history-grid-line"
                        :style="{ top: salesAxisPosition(tick) }"
                        aria-hidden="true"
                      ></span>
                      <div class="spark-bars sales">
                       <i
                          v-for="item in detail?.history ?? []"
                          :key="`sales-${item.metric_date}`"
                          :title="`${item.metric_date}: ${item.ordered_units ?? 0} 件`"
                          :class="{ zero: (item.ordered_units ?? 0) === 0 }"
                          :style="{ height: salesBarHeight(item.ordered_units) }"
                        ></i>
                      </div>
                    </div>
                  </div>
                  <div class="sales-history-range" aria-hidden="true">
                    <span>{{ detail.history[0]?.metric_date }}</span>
                    <span>{{ detail.history.at(-1)?.metric_date }}</span>
                  </div>
                </div>
                <p v-else class="state-card slim">暂无销售件数历史。</p>
              </article>
            </template>
          </div>

          <footer class="competitor-modal-actions">
            <button type="button" @click="closeProductDetail">关闭详情</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>
