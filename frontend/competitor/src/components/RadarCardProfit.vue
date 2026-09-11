<script setup lang="ts">
import { computed } from "vue";
import type { CompetitorItem, OwnStoreScope } from "../types";

const props = defineProps<{ item: CompetitorItem; storeScope: OwnStoreScope; storeCode?: string }>();
// The authorized list supplies profit and quotes together; mounting makes no request.
const summary = computed(() => props.item.own_profit_summary ?? {
  profit: null, margin: null, available: 0, total: props.item.自有报价?.length ?? 0,
  reasons: ["暂无当前报价的利润计算依据。"],
});
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
</script>

<template>
  <section class="radar-card-profit" aria-label="单件预计利润" :title="evidence">
    <span class="radar-card-profit-label">预计利润 / 件</span>
    <strong :class="{ 'is-loss': summary.profit && summary.profit[0] < 0 }">{{ range(summary.profit) }}</strong>
    <span v-if="summary.margin" class="radar-card-profit-margin">利润率 {{ range(summary.margin, true) }}</span>
    <small v-if="summary.available > 0 && summary.available < summary.total">{{ summary.available }}/{{ summary.total }} 报价可算</small>
  </section>
</template>
