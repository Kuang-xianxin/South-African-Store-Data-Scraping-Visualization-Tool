<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { fetchRisks } from "../api";
import type { RiskPayload } from "../types";

const props = defineProps<{ asOf: string }>();
const data = ref<RiskPayload | null>(null);
const tab = ref<"anomalies" | "quality">("anomalies");
const anomalyScope = ref<"latest" | "all">("latest");
const loading = ref(true);

const anomalies = computed(() =>
  anomalyScope.value === "latest"
    ? data.value?.latest_anomalies ?? []
    : data.value?.anomalies ?? [],
);

watch(() => props.asOf, load, { immediate: true });

async function load() {
  loading.value = true;
  try {
    data.value = await fetchRisks(props.asOf);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="erp-page risks-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">CONTROL TOWER</p>
        <h2>把经营异常和数据质量放在同一处处理</h2>
      </div>
      <div class="page-tabs">
        <button :class="{ active: tab === 'anomalies' }" @click="tab = 'anomalies'">经营异常</button>
        <button :class="{ active: tab === 'quality' }" @click="tab = 'quality'">数据质量</button>
      </div>
    </div>
    <div v-if="loading" class="state-card">正在读取风险数据……</div>
    <template v-else-if="data">
      <section class="mini-kpis">
        <article><span>最新异常商品</span><strong>{{ data.summary.latest_anomaly_products }}</strong></article>
        <article><span>最新异常记录</span><strong>{{ data.summary.latest_anomaly_records }}</strong></article>
        <article><span>质量事件</span><strong>{{ data.summary.quality_events }}</strong></article>
        <article><span>未知销售状态</span><strong>{{ data.summary.unknown_sale_status }}</strong></article>
      </section>

      <section v-if="tab === 'anomalies'" class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">ANOMALY EVENTS</p>
            <h3>异常商品</h3>
          </div>
          <div class="segmented">
            <button :class="{ active: anomalyScope === 'latest' }" @click="anomalyScope = 'latest'">
              最新指标日
            </button>
            <button :class="{ active: anomalyScope === 'all' }" @click="anomalyScope = 'all'">
              全部历史
            </button>
          </div>
        </div>
        <p class="method-note">
          同一商品触发多种异常时保留多条记录；最新指标日为 {{ data.latest_metric_date || "暂无" }}。
        </p>
        <div v-if="!anomalies.length" class="state-card slim">当前范围没有异常记录。</div>
        <div v-else class="risk-list">
          <article v-for="item in anomalies" :key="`${item.event_date}-${item.offer_id}-${item.anomaly_type}`">
            <span class="severity" :class="item.severity || 'info'">{{ item.severity_label }}</span>
            <div>
              <strong>{{ item.anomaly_label }}</strong>
              <p>{{ item.explanation }}</p>
              <small>{{ item.event_date }} · 商品编号 {{ item.offer_id }}</small>
            </div>
          </article>
        </div>
      </section>

      <section v-else class="erp-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">DATA QUALITY</p>
            <h3>质量事件</h3>
          </div>
          <span>页面只读展示</span>
        </div>
        <div v-if="!data.quality_events.length" class="state-card slim">当前没有已记录的质量事件。</div>
        <div v-else class="erp-table-wrap">
          <table class="erp-table">
            <thead><tr><th>日期</th><th>事件</th><th>级别</th><th>商品编号</th><th>详情</th></tr></thead>
            <tbody>
              <tr v-for="item in data.quality_events" :key="item.event_id">
                <td>{{ item.event_date }}</td>
                <td><strong>{{ item.event_label }}</strong></td>
                <td>{{ item.severity_label }}</td>
                <td>{{ item.offer_id || "—" }}</td>
                <td>{{ item.details_text }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
