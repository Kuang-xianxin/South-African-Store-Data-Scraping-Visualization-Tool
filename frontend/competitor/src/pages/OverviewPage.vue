<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { fetchSummary } from "../api";
import type { SummaryPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const data = ref<SummaryPayload | null>(null);
const loading = ref(true);
const error = ref("");

const maxUnits = computed(() =>
  Math.max(1, ...(data.value?.sales_series.map((item) => item.ordered_units ?? 0) ?? [1])),
);

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
