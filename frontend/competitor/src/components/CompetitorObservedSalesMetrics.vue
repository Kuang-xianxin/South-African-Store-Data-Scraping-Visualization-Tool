<script setup lang="ts">
import type {
  CompetitorObservedSalesWindowKey,
  CompetitorObservedSalesWindows,
} from "../types";

const props = withDefaults(defineProps<{
  values?: CompetitorObservedSalesWindows;
  throughDate?: string | null;
  compact?: boolean;
}>(), {
  values: () => ({}),
  throughDate: null,
  compact: false,
});

const windowDays = [7, 15, 30, 60, 90] as const;

function observedUnits(days: typeof windowDays[number]): number | null {
  const value = props.values[String(days) as CompetitorObservedSalesWindowKey];
  return typeof value === "number" ? value : null;
}

function observedUnitsLabel(days: typeof windowDays[number]): string {
  const value = observedUnits(days);
  return value === null ? "数据不足" : `${value.toLocaleString("zh-CN")} 件`;
}
</script>

<template>
  <section
    class="competitor-observed-sales"
    :class="{ compact }"
    aria-label="近期库存观察售出"
  >
    <header>
      <strong>近期库存观察售出</strong>
      <span>{{ throughDate ? `截至 ${throughDate}` : "暂无可用库存日期" }}</span>
    </header>
    <div class="competitor-observed-sales-grid">
      <div
        v-for="days in windowDays"
        :key="days"
        :class="{ unavailable: observedUnits(days) === null }"
      >
        <small>近{{ days }}天</small>
        <strong>{{ observedUnitsLabel(days) }}</strong>
      </div>
    </div>
    <p>按同一库存身份的精确下降累计，不等同 Takealot 实际订单销量。</p>
  </section>
</template>
