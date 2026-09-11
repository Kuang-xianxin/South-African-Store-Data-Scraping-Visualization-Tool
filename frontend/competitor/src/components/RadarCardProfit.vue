<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { AUTH_SESSION_ENDING_EVENT, fetchCompetitorDetail } from "../api";
import { radarCardProfitSummary } from "../radarCardProfit";
import type { CompetitorItem, OwnStoreProfitabilityItem, OwnStoreScope } from "../types";

const props = defineProps<{ item: CompetitorItem; storeScope: OwnStoreScope; storeCode?: string }>();
const items = shallowRef<OwnStoreProfitabilityItem[] | null>(null);
const loading = ref(false);
const error = ref("");
let controller: AbortController | null = null;
let revision = 0;
const summary = computed(() => radarCardProfitSummary(items.value ?? [], props.item.plid));
const evidence = computed(() => [
  'NF运营表模型预计利润，不等同实际结算净利润。',
  `${summary.value.available} / ${summary.value.total} 个自有报价可核算；缺失报价未计入区间。`,
  ...summary.value.reasons,
].join('\n'));
const money = new Intl.NumberFormat("en-ZA", { style: "currency", currency: "ZAR", maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
function range(value: [number, number] | null, percentage = false) {
  if (!value) return "待核算";
  const format = (n: number) => percentage ? `${percent.format(n)}%` : money.format(n);
  return value[0] === value[1] ? format(value[0]) : `${format(value[0])} – ${format(value[1])}`;
}
function reset() {
  ++revision;
  controller?.abort(); controller = null;
  items.value = null; error.value = ""; loading.value = false;
}
watch(() => JSON.stringify([props.item.plid, props.item.来源, props.item.自有报价, props.storeScope, props.storeCode]), reset);
async function load() {
  if (loading.value || props.item.来源 !== "own_store") return;
  const requestRevision = ++revision;
  controller?.abort();
  const pending = new AbortController(); controller = pending;
  loading.value = true; error.value = "";
  try {
    // Explicit user action only; normal list renders, filtering and paging stay request-free.
    const detail = await fetchCompetitorDetail(props.item.plid, undefined, undefined, props.storeScope, pending.signal);
    if (requestRevision !== revision || pending.signal.aborted) return;
    items.value = detail.own_store_profitability?.items ?? [];
  } catch (reason) {
    if (requestRevision !== revision || pending.signal.aborted) return;
    error.value = reason instanceof Error ? reason.message : "利润暂时读取失败";
  } finally {
    if (requestRevision === revision) { loading.value = false; controller = null; }
  }
}
if (typeof window !== "undefined") window.addEventListener(AUTH_SESSION_ENDING_EVENT, reset);
onBeforeUnmount(() => {
  reset();
  if (typeof window !== "undefined") window.removeEventListener(AUTH_SESSION_ENDING_EVENT, reset);
});
</script>

<template>
  <section class="radar-card-profit" aria-label="单件预计利润" :title="items ? evidence : '按NF运营表成本与当前自有报价核算'">
    <div class="radar-card-profit-heading"><span class="radar-card-profit-label">预计利润 / 件</span>
      <button v-if="items !== null" type="button" class="radar-card-profit-load" aria-label="重新读取利润" title="重新读取利润" :disabled="loading" @click.stop="load">↻</button>
    </div>
    <template v-if="props.item.来源 !== 'own_store'">
      <strong>待核算</strong><small>暂无该竞品的成本依据</small>
    </template>
    <template v-else-if="items === null">
      <button type="button" class="radar-card-profit-load" :disabled="loading" @click.stop="load">
        {{ loading ? "读取中…" : error ? "重新读取利润" : "查看利润" }}
      </button>
      <small v-if="error" role="status">{{ error }}</small>
    </template>
    <template v-else>
      <strong :class="{ 'is-loss': summary.profit && summary.profit[0] < 0 }">{{ range(summary.profit) }}</strong>
      <span v-if="summary.margin" class="radar-card-profit-margin">利润率 {{ range(summary.margin, true) }}</span>
      <small v-if="summary.available < summary.total">{{ summary.available }}/{{ summary.total }} 报价可算</small>
      <small v-if="error" role="status">更新失败，以上保留上次结果：{{ error }}</small>
    </template>
  </section>
</template>
