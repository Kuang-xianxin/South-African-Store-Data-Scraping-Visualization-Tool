<script setup lang="ts">
import { computed } from "vue";
import { competitorPriceSummary } from "../competitorPriceSummary";
import type { CompetitorItem } from "../types";

const props = defineProps<{ item: CompetitorItem }>();
const prices = computed(() => competitorPriceSummary(props.item));
</script>

<template>
  <div class="radar-price-summary" aria-label="商品报价">
    <div class="radar-price-range" :title="prices.isOwn ? '当前可见自有报价与跟卖报价的有效价格区间' : '已记录卖家报价的有效价格区间'">
      <span class="radar-price-label">{{ prices.isOwn ? '全部报价区间' : '报价区间' }}</span>
      <strong class="radar-price-value">{{ prices.all }}</strong>
    </div>
    <div v-if="prices.isOwn" class="radar-price-own" title="当前授权店铺的自有有效报价；多个价格显示区间">
      <span class="radar-price-own-label">自有报价</span>
      <strong class="radar-price-own-value">{{ prices.own }}</strong>
    </div>
    <div v-else class="radar-price-main">
      <span>主报价</span><strong>{{ prices.main }}</strong>
    </div>
  </div>
</template>

<style>
.radar-price-summary {
  display: grid;
  align-content: start;
  gap: 8px;
  min-width: 0;
  font-variant-numeric: tabular-nums;
}
.competitor-status-card .competitor-status-summary > .radar-price-summary { row-gap: 8px; }
.radar-price-summary .radar-price-range {
  display: grid;
  gap: 4px;
  padding: 4px 0;
}
.radar-price-summary .radar-price-label {
  color: #52675c;
  font-size: 12px;
  font-weight: 650;
}
.radar-price-summary .radar-price-value {
  color: #263d31;
  font-size: 17px;
  font-weight: 750;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.radar-price-summary .radar-price-own {
  display: grid;
  gap: 5px;
  padding: 9px 10px;
  border: 1px solid #b4d6c2;
  border-left: 3px solid #25724d;
  border-radius: 8px;
  background: #edf7f0;
}
.radar-price-summary .radar-price-own-label {
  color: #1f6542;
  font-size: 12px;
  font-weight: 800;
}
.radar-price-summary .radar-price-own-value {
  color: #135c36;
  font-size: 19px;
  font-weight: 850;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.radar-price-summary .radar-price-main {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 7px;
  color: #5d6e64;
  font-size: 12px;
}
.radar-price-summary .radar-price-main strong { font-size: 12px; }
.personal-watchlist-product-metrics > .radar-price-summary {
  grid-column: 1 / -1;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: stretch;
  padding: 5px 0;
}
.personal-watchlist-product-metrics .radar-price-range { align-content: center; }
@media (max-width: 480px) {
  .personal-watchlist-product-metrics > .radar-price-summary { grid-template-columns: minmax(0, 1fr); }
}
</style>
