<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { fetchProductDetail, fetchProducts } from "../api";
import type { ProductDetailPayload, ProductItem, ProductsPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const payload = ref<ProductsPayload | null>(null);
const detail = ref<ProductDetailPayload | null>(null);
const selectedId = ref("");
const query = ref("");
const loading = ref(true);
const detailLoading = ref(false);
const error = ref("");

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return payload.value?.items ?? [];
  return (payload.value?.items ?? []).filter((item) =>
    [item.offer_id, item.sku, item.tsin_id, item.barcode, item.title]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle)),
  );
});
const selected = computed(
  () => payload.value?.items.find((item) => item.offer_id === selectedId.value) ?? null,
);
const maxUnits = computed(() =>
  Math.max(1, ...(detail.value?.history.map((item) => item.ordered_units ?? 0) ?? [1])),
);
const maxViews = computed(() =>
  Math.max(
    1,
    ...(detail.value?.history.map((item) => item.page_views_30_days ?? 0) ?? [1]),
  ),
);

watch(() => props.asOf, loadProducts, { immediate: true });
watch(selectedId, loadDetail);

async function loadProducts() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchProducts(props.asOf);
    if (!payload.value.items.some((item) => item.offer_id === selectedId.value)) {
      selectedId.value = payload.value.items[0]?.offer_id ?? "";
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "商品数据读取失败";
  } finally {
    loading.value = false;
  }
}

async function loadDetail() {
  if (!selectedId.value) {
    detail.value = null;
    return;
  }
  detailLoading.value = true;
  try {
    detail.value = await fetchProductDetail(selectedId.value, props.asOf);
  } finally {
    detailLoading.value = false;
  }
}

function select(item: ProductItem) {
  selectedId.value = item.offer_id;
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

function percent(value: unknown) {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "—";
}
</script>

<template>
  <div class="erp-page products-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">PRODUCT COMMAND CENTER</p>
        <h2>从商品列表快速下钻到销售与流量</h2>
      </div>
      <p>流量快照和每日下单分开呈现，避免误读。</p>
    </div>
    <div class="product-workspace">
      <aside class="product-list erp-panel">
        <div class="search-box">
          <input
            v-model="query"
            type="search"
            placeholder="搜索编码、条码或商品名称"
          />
          <span>{{ filtered.length }} 个商品</span>
        </div>
        <div v-if="loading" class="state-card slim">正在读取商品……</div>
        <p v-else-if="error" class="state-card error">{{ error }}</p>
        <div v-else class="product-scroll">
          <button
            v-for="item in filtered"
            :key="item.offer_id"
            :class="{ active: selectedId === item.offer_id }"
            @click="select(item)"
          >
            <strong>{{ item.title || item.sku || item.offer_id }}</strong>
            <span>{{ item.sku || "无库存编码" }} · {{ item.status_label || "状态未知" }}</span>
            <small>下单 {{ number(item.ordered_units) }} · 浏览 {{ number(item.page_views_30_days) }}</small>
          </button>
        </div>
      </aside>

      <section class="product-detail">
        <div v-if="!selected" class="erp-panel state-card">请选择一个商品。</div>
        <template v-else>
          <article class="erp-panel product-hero">
            <div>
              <p class="section-kicker">SELECTED PRODUCT</p>
              <h3>{{ selected.title || selected.sku || selected.offer_id }}</h3>
              <p>
                SKU {{ selected.sku || "—" }} · 商品编号 {{ selected.offer_id }} ·
                平台商品编号 {{ selected.tsin_id || "—" }}
              </p>
            </div>
            <span class="status-badge">{{ selected.status_label || "状态未知" }}</span>
          </article>

          <section class="mini-kpis">
            <article><span>当前售价</span><strong>{{ currency(selected.selling_price) }}</strong></article>
            <article><span>最新日下单</span><strong>{{ number(detail?.kpis.latest_ordered_units) }}</strong></article>
            <article><span>近7日下单</span><strong>{{ number(detail?.kpis.seven_day_ordered_units) }}</strong></article>
            <article><span>平台可售库存</span><strong>{{ number(selected.total_stock) }}</strong></article>
            <article><span>近30天浏览量</span><strong>{{ number(detail?.kpis.page_views_30_days) }}</strong></article>
            <article><span>近30天转化率</span><strong>{{ percent(detail?.kpis.conversion_percentage_30_days) }}</strong></article>
          </section>

          <article class="erp-panel dual-chart">
            <div class="panel-heading">
              <div>
                <p class="section-kicker">PRODUCT HISTORY</p>
                <h3>销售与30天流量快照</h3>
              </div>
              <span v-if="detailLoading">正在更新……</span>
            </div>
            <div class="spark-rows">
              <div>
                <strong>每日下单件数</strong>
                <div class="spark-bars sales">
                  <i
                    v-for="item in detail?.history ?? []"
                    :key="`sales-${item.metric_date}`"
                    :title="`${item.metric_date}: ${item.ordered_units ?? 0}`"
                    :style="{ height: `${Math.max(3, ((item.ordered_units ?? 0) / maxUnits) * 100)}%` }"
                  ></i>
                </div>
              </div>
              <div>
                <strong>近30天浏览量快照</strong>
                <div class="spark-bars views">
                  <i
                    v-for="item in detail?.history ?? []"
                    :key="`views-${item.metric_date}`"
                    :title="`${item.metric_date}: ${item.page_views_30_days ?? '缺失'}`"
                    :class="{ missing: item.page_views_30_days === null }"
                    :style="{ height: `${Math.max(3, ((item.page_views_30_days ?? 0) / maxViews) * 100)}%` }"
                  ></i>
                </div>
              </div>
            </div>
            <p class="method-note">浏览量是滚动30天窗口快照，不是精确当天流量或独立访客数。</p>
          </article>
        </template>
      </section>
    </div>
  </div>
</template>
