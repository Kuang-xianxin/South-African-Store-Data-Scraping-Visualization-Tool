<script setup lang="ts">
import type {
  CompetitorObservedSalesWindowKey,
  CompetitorObservedSalesWindows,
} from "../types";

const props = withDefaults(defineProps<{
  values?: CompetitorObservedSalesWindows;
  throughDate?: string | null;
  title?: string;
  contextLabel?: string | null;
  compact?: boolean;
  embedded?: boolean;
}>(), {
  values: () => ({}),
  throughDate: null,
  title: "近期库存观察售出（件）",
  contextLabel: null,
  compact: false,
  embedded: false,
});

const windowDays = [7, 15, 30, 60, 90] as const;

function observedUnits(days: typeof windowDays[number]): number | null {
  const value = props.values[String(days) as CompetitorObservedSalesWindowKey];
  return typeof value === "number" ? value : null;
}

function observedUnitsLabel(days: typeof windowDays[number]): string {
  const value = observedUnits(days);
  return value === null ? "数据不足" : value.toLocaleString("zh-CN");
}
</script>

<template>
  <section
    class="competitor-observed-sales"
    :class="{ compact, embedded }"
    :aria-label="title"
  >
    <header v-if="!embedded">
      <span class="competitor-observed-sales-heading">
        <strong>{{ title }}</strong>
        <small v-if="contextLabel">{{ contextLabel }}</small>
      </span>
      <span>{{ throughDate ? `截至 ${throughDate}` : "暂无可用库存日期" }}</span>
    </header>
    <dl class="competitor-observed-sales-list">
      <div
        v-for="days in windowDays"
        :key="days"
        :class="{ unavailable: observedUnits(days) === null }"
      >
        <dt>{{ days }}天：</dt>
        <dd>{{ observedUnitsLabel(days) }}</dd>
      </div>
    </dl>
    <footer>
      <span v-if="embedded">
        {{ throughDate ? `截至 ${throughDate}` : "暂无可用库存日期" }}
      </span>
      <span>库存观察 · 不等同订单</span>
    </footer>
  </section>
</template>
