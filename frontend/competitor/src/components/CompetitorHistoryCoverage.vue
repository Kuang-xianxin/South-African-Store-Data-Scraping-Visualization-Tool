<script setup lang="ts">
import { computed } from "vue";
import type { OfferObservationPoint } from "../competitorHistoryCoverage";
import { formatChinaDateTime } from "../time";

const props = defineProps<{ points: OfferObservationPoint[] }>();
const matched = computed(() => props.points.filter((point) => point.offer).length);
const missing = computed(() => props.points.length - matched.value);
const inexact = computed(() => props.points.filter((point) => point.offer && point.exactStock === null).length);
</script>

<template>
  <details v-if="points.length" class="history-coverage">
    <summary>
      {{ points.length }} 次采集 · {{ matched }} 次包含该报价
      <span v-if="missing"> · {{ missing }} 次未返回该报价</span>
      <span v-if="inexact"> · {{ inexact }} 次库存未确定</span>
      <small>查看采集明细</small>
    </summary>
    <p v-if="missing || inexact">
      空缺表示当次报价或精确库存未取得，不代表停采或零库存。虚线只连接已知端点，期间实际变化未确认。空心节点仅标示采集时间，位置不代表实测数值。
    </p>
    <div class="history-observations" role="region" aria-label="逐次商品采集明细" tabindex="0">
      <table>
        <thead><tr><th>北京时间</th><th>所选报价价格</th><th>精确库存</th><th>商品评论</th><th>采集状态</th></tr></thead>
        <tbody>
          <tr v-for="point in points" :key="`${point.snapshot.快照ID}:${point.capturedAtMs}`">
            <td>{{ formatChinaDateTime(point.snapshot.采集时间) }}</td>
            <td>{{ point.price === null ? "—" : `R ${point.price.toFixed(2)}` }}</td>
            <td>{{ point.exactStock === null ? "未取得" : `${point.exactStock} 件` }}</td>
            <td>{{ point.reviews ?? "—" }}</td>
            <td>{{ point.observationLabel }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
</template>

<style scoped>
.history-coverage { margin: 10px 0; border: 1px solid #c9dcd2; border-radius: 10px; background: #f5faf7; color: #284b3b; }
summary { padding: 11px 12px; cursor: pointer; font-size: 12px; line-height: 1.7; }
summary small { display: inline-block; margin-left: 10px; text-decoration: underline; }
p { margin: 0 12px 10px; font-size: 12px; line-height: 1.6; }
.history-observations { max-height: 280px; overflow: auto; margin: 0 10px 10px; }
table { width: 100%; border-collapse: collapse; font-size: 11px; }
th, td { padding: 8px; border-bottom: 1px solid #dce7df; text-align: left; }
th { position: sticky; top: 0; background: #eaf3ed; }
td:first-child { white-space: nowrap; }
summary:focus-visible { outline: 2px solid #236649; outline-offset: 2px; }
</style>
