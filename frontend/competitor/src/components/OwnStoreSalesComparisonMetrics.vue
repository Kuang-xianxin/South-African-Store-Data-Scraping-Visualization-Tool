<script setup lang="ts">
import type {
  CompetitorObservedSalesWindowKey,
  CompetitorObservedSalesWindows,
} from "../types";

const props = withDefaults(defineProps<{
  ownValues?: CompetitorObservedSalesWindows;
  ownThroughDate?: string | null;
  followerValues?: CompetitorObservedSalesWindows;
  followerThroughDate?: string | null;
  ownContextLabel?: string | null;
  followerContextLabel?: string | null;
}>(), {
  ownValues: () => ({}),
  ownThroughDate: null,
  followerValues: () => ({}),
  followerThroughDate: null,
  ownContextLabel: null,
  followerContextLabel: null,
});

const windowDays = [7, 15, 30, 60, 90] as const;

function valueLabel(
  values: CompetitorObservedSalesWindows,
  days: typeof windowDays[number],
): string {
  const value = values[String(days) as CompetitorObservedSalesWindowKey];
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "数据不足";
}
</script>

<template>
  <section class="own-store-sales-comparison" aria-label="自有官方销量与全部跟卖库存观察售出对比">
    <header>
      <strong>销量对比（件）</strong>
      <span>全部自有链接 vs 全部跟卖报价</span>
    </header>
    <table>
      <thead>
        <tr>
          <th scope="col">周期</th>
          <th scope="col">
            <strong>自有官方</strong>
            <small v-if="ownContextLabel">{{ ownContextLabel }}</small>
          </th>
          <th scope="col">
            <strong>跟卖观察</strong>
            <small v-if="followerContextLabel">{{ followerContextLabel }}</small>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="days in windowDays" :key="days">
          <th scope="row">{{ days }}天</th>
          <td :class="{ unavailable: valueLabel(ownValues, days) === '数据不足' }">
            {{ valueLabel(ownValues, days) }}
          </td>
          <td :class="{ unavailable: valueLabel(followerValues, days) === '数据不足' }">
            {{ valueLabel(followerValues, days) }}
          </td>
        </tr>
      </tbody>
    </table>
    <footer>
      <span>自有截至 {{ ownThroughDate || "数据不足" }}</span>
      <span>跟卖截至 {{ followerThroughDate || "数据不足" }}</span>
      <small>自有为 Seller Sales；跟卖为库存观察，不等同订单</small>
    </footer>
  </section>
</template>
