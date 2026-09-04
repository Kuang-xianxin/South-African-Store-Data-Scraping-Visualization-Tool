<script setup lang="ts">
import { computed } from "vue";

import {
  selectOwnStoreSalesSeries,
  summarizeOwnStoreSalesWindows,
  type OwnStoreSalesWindowSummary,
} from "../ownStoreSalesSummary";
import type { OwnStoreSalesSeries } from "../types";

const props = withDefaults(defineProps<{
  series: OwnStoreSalesSeries[];
  preferredStoreCode?: string | null;
  title?: string;
  ariaLabel?: string;
  listingLabel?: string;
  contextLabel?: string | null;
  emptyMessage?: string;
  sourceLabel?: string;
}>(), {
  preferredStoreCode: null,
  title: "整条链接官方销量（件）",
  ariaLabel: "整条自有链接上架以来官方销量",
  listingLabel: "链接上架时间",
  contextLabel: null,
  emptyMessage: "当前账号可见店铺暂无该链接的 Seller Sales 数据。",
  sourceLabel: "Seller Sales · 整条链接全部 Offer · 从链接上架日起读取",
});

const selectedSeries = computed(() =>
  selectOwnStoreSalesSeries(props.series, props.preferredStoreCode));
const periodSummaries = computed(() =>
  selectedSeries.value ? summarizeOwnStoreSalesWindows(selectedSeries.value) : []);

function unitsLabel(value: number | null): string {
  return value === null
    ? "数据不足"
    : `${new Intl.NumberFormat("zh-CN").format(value)} 件`;
}

function periodCoverageLabel(summary: OwnStoreSalesWindowSummary): string {
  if (!summary.expectedDays) return "暂无覆盖";
  const prefix = summary.expectedDays < summary.days
    ? `上架 ${summary.expectedDays} 天 · `
    : "";
  return `${prefix}已完整 ${summary.verifiedDays}天 · 尚未完整 ${summary.partialDays}天 · 缺失 ${summary.missingDays}天`;
}

function totalCoverageLabel(series: OwnStoreSalesSeries): string {
  return `已完整 ${series.covered_days}天 · 尚未完整 ${series.partial_days}天 · 缺失 ${series.missing_days}天`;
}

function listingTimeLabel(series: OwnStoreSalesSeries): string {
  if (!series.listing_at) return series.listing_date || "数据不足";
  const parsed = new Date(series.listing_at);
  if (Number.isNaN(parsed.getTime())) return series.listing_date || series.listing_at;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(parsed);
}
</script>

<template>
  <section class="own-store-sales-overview" :aria-label="ariaLabel">
    <header>
      <strong>{{ title }}</strong>
      <span v-if="selectedSeries">
        {{ selectedSeries.store_name }}
        <template v-if="contextLabel"> · {{ contextLabel }}</template>
        · 截至 {{ selectedSeries.through_date }}
      </span>
    </header>

    <div v-if="!selectedSeries" class="own-store-sales-overview-empty">
      {{ emptyMessage }}
    </div>
    <template v-else>
      <dl class="own-store-sales-overview-list">
        <div
          v-for="summary in periodSummaries"
          :key="summary.days"
          :class="{ incomplete: summary.partialDays > 0 || summary.missingDays > 0 }"
        >
          <dt>近{{ summary.days }}天</dt>
          <dd>{{ unitsLabel(summary.orderedUnits) }}</dd>
          <small>{{ periodCoverageLabel(summary) }}</small>
        </div>
        <div
          class="own-store-sales-overview-total"
          :class="{ incomplete: selectedSeries.partial_days > 0 || selectedSeries.missing_days > 0 }"
        >
          <dt>总销量</dt>
          <dd>{{ unitsLabel(selectedSeries.total_ordered_units) }}</dd>
          <small>当前可见累计 · {{ totalCoverageLabel(selectedSeries) }}</small>
        </div>
        <div class="own-store-sales-overview-listing">
          <dt>{{ listingLabel }}</dt>
          <dd>{{ listingTimeLabel(selectedSeries) }}</dd>
          <small>{{ selectedSeries.listing_date_source === "platform" ? "平台 created_at" : "本库最早记录" }}</small>
        </div>
      </dl>
      <footer>
        <span>{{ sourceLabel }}</span>
        <span>不受列表区间影响 · 尚未完整的日期销量仍可能增加 · 缺失日期不补 0</span>
      </footer>
    </template>
  </section>
</template>
