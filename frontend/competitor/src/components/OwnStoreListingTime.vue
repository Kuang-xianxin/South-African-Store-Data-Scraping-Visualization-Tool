<script setup lang="ts">
import { computed } from "vue";
import { parseUtcDateTime } from "../time";
import type { CompetitorItem } from "../types";

const props = defineProps<{
  item: Pick<CompetitorItem, "自有上架时间" | "自有上架日期">;
}>();

const listingTime = computed(() => {
  const timestamp = parseUtcDateTime(props.item.自有上架时间 ?? null);
  if (!Number.isNaN(timestamp)) {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(timestamp);
  }
  return props.item.自有上架日期 || "未获取";
});
</script>

<template>
  <span
    class="own-store-listing-time competitor-first-monitored-badge is-compact"
    :title="listingTime === '未获取'
      ? '尚未获取平台上架时间'
      : '当前可见自有店铺中最早的平台上架时间（北京时间）'"
  >
    <small>上架时间</small>
    <strong>{{ listingTime }}</strong>
  </span>
</template>

<style scoped>
.own-store-listing-time.competitor-first-monitored-badge {
  border-color: #d8b4fe;
  background: #f3e8ff;
  color: #6b21a8;
}

.own-store-listing-time small {
  color: #7e22ce;
}

.own-store-listing-time strong {
  color: #6b21a8;
}
</style>
