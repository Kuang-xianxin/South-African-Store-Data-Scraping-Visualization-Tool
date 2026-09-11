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

function observedUnits(days: typeof windowDays[number] | "total"): number | null {
  const value = props.values[String(days) as CompetitorObservedSalesWindowKey | "total"];
  return typeof value === "number" ? value : null;
}

function observedUnitsLabel(days: typeof windowDays[number] | "total"): string {
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
        <strong>{{ compact ? title.replace("近期", "") : title }}</strong>
        <small v-if="contextLabel">{{ contextLabel }}</small>
      </span>
      <span>{{ throughDate ? `截至 ${throughDate}` : "暂无可用库存日期" }}</span>
    </header>
    <table v-if="compact && !embedded" class="competitor-observed-sales-table">
      <thead><tr><th scope="col">周期</th><th scope="col">观察售出</th></tr></thead>
      <tbody>
        <tr v-for="days in windowDays" :key="days">
          <th scope="row">{{ days }}天</th>
          <td :class="{ unavailable: observedUnits(days) === null }">{{ observedUnitsLabel(days) }}</td>
        </tr>
        <tr class="sales-total-row" title="全部已采集历史的累计库存观察售出，不等同平台总订单">
          <th scope="row">总销量<small>库存观察累计</small></th>
          <td :class="{ unavailable: observedUnits('total') === null }">{{ observedUnitsLabel('total') }}</td>
        </tr>
      </tbody>
    </table>
    <dl v-else class="competitor-observed-sales-list">
      <div
        v-for="days in windowDays"
        :key="days"
        :class="{ unavailable: observedUnits(days) === null }"
      >
        <dt>{{ days }}天：</dt>
        <dd>{{ observedUnitsLabel(days) }}</dd>
      </div>
    </dl>
    <footer v-if="embedded">
      <span>
        {{ throughDate ? `截至 ${throughDate}` : "暂无可用库存日期" }}
      </span>
    </footer>
  </section>
</template>
