<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { fetchLogisticsOverview } from "../api";
import { formatChinaDateTime } from "../time";
import type { LogisticsOverviewPayload } from "../types";

defineOptions({ name: "LogisticsPage" });
const props = defineProps<{ asOf?: string }>();
void props.asOf;

const loading = ref(true);
const refreshing = ref(false);
const error = ref("");
const payload = ref<LogisticsOverviewPayload | null>(null);

const w8Metrics = computed(() => {
  const summary = payload.value?.w8.summary;
  if (!summary) return [];
  return [
    { label: "产品档案", value: summary.products, note: "长睿 SKU" },
    { label: "当前库存", value: summary.stock_total, note: `${summary.stock_records} 条库存记录` },
    { label: "可用库存", value: summary.usable_stock, note: "可继续分配" },
    { label: "锁定库存", value: summary.locked_stock, note: "暂不可用" },
    { label: "出库占用", value: summary.outbound_allocated, note: "已进入出库流程" },
    { label: "入库单", value: summary.inbound_orders, note: "长睿历史单据" },
    { label: "出库单", value: summary.outbound_orders, note: "一件代发" },
    { label: "退货记录", value: summary.returned_records, note: "按实际返回条目" },
  ];
});

onMounted(() => void load(false));

async function load(force: boolean) {
  if (force) refreshing.value = true;
  else loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchLogisticsOverview(force);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "物流数据读取失败";
  } finally {
    loading.value = false;
    refreshing.value = false;
  }
}

function number(value: number | null | undefined) {
  return new Intl.NumberFormat("zh-CN").format(value ?? 0);
}

function text(value: string | number | null | undefined, fallback = "—") {
  const normalized = String(value ?? "").trim();
  return normalized || fallback;
}

function shipmentState(row: LogisticsOverviewPayload["takealot"]["recent_shipments"][number]) {
  if (row.cancelled) return "已取消";
  if (row.date_unloaded) return "已卸货";
  if (row.shipped) return "已发货";
  return text(row.purchase_order_state, "待发货");
}
</script>

<template>
  <section class="logistics-page">
    <header class="logistics-hero">
      <div>
        <p>LIVE LOGISTICS / READ ONLY</p>
        <h2>长睿仓配与 Takealot 货件总览</h2>
        <span>先把两边真实编号和数量放到同一页，再逐步确认货件、批次、运单与 SKU 的关系。</span>
      </div>
      <button type="button" :disabled="loading || refreshing" @click="load(true)">
        {{ refreshing ? "正在重新读取…" : "重新读取两边接口" }}
      </button>
    </header>

    <div v-if="loading" class="state-card">正在读取长睿和 Takealot 物流数据……</div>
    <div v-else-if="error" class="state-card error">{{ error }}</div>
    <template v-else-if="payload">
      <section class="connection-grid">
        <article :class="['connection-card', { disconnected: !payload.w8.connected }]">
          <div class="connection-heading">
            <span :class="['connection-dot', { off: !payload.w8.connected }]"></span>
            <div>
              <p>LONG REACH W8</p>
              <h3>{{ payload.w8.connected ? "长睿正式环境已连接" : "长睿接口未连接" }}</h3>
            </div>
          </div>
          <strong v-if="payload.w8.warehouse">
            {{ payload.w8.warehouse.code }} · {{ payload.w8.warehouse.name }}
          </strong>
          <span v-else>{{ payload.w8.message || "暂无仓库信息" }}</span>
          <small>
            渠道：{{ payload.w8.channels.map((row) => row.name || row.code).join("、") || "暂无" }}
          </small>
        </article>

        <article :class="['connection-card', { disconnected: !payload.takealot.connected }]">
          <div class="connection-heading">
            <span :class="['connection-dot', { off: !payload.takealot.connected }]"></span>
            <div>
              <p>TAKEALOT MARKETPLACE</p>
              <h3>{{ payload.takealot.connected ? "平台货件接口已连接" : "平台货件接口未连接" }}</h3>
            </div>
          </div>
          <strong>{{ number(payload.takealot.summary.shipments) }} 个 Shipment</strong>
          <span v-if="payload.takealot.message">{{ payload.takealot.message }}</span>
          <small>
            本页生成：{{ formatChinaDateTime(payload.generated_at, "暂无") }} · 北京时间
          </small>
        </article>
      </section>

      <section class="logistics-section">
        <div class="logistics-section-heading">
          <div>
            <p>WAREHOUSE PULSE</p>
            <h3>长睿当前仓配脉搏</h3>
          </div>
          <span>实时只读</span>
        </div>
        <div class="metric-grid">
          <article v-for="metric in w8Metrics" :key="metric.label" class="metric-card">
            <small>{{ metric.label }}</small>
            <strong>{{ number(metric.value) }}</strong>
            <span>{{ metric.note }}</span>
          </article>
        </div>
      </section>

      <section class="logistics-section relation-panel">
        <div class="relation-copy">
          <p>RELATIONSHIP READINESS</p>
          <h3>两边明确编号匹配</h3>
          <strong>{{ number(payload.matching.direct_match_count) }}</strong>
          <span>{{ payload.matching.method }}</span>
        </div>
        <div class="relation-stats">
          <div>
            <small>待关联长睿入库单</small>
            <strong>{{ number(payload.matching.unmatched_w8_inbound) }}</strong>
          </div>
          <div>
            <small>待关联 Takealot Shipment</small>
            <strong>{{ number(payload.matching.unmatched_takealot_shipments) }}</strong>
          </div>
          <div>
            <small>平台带 Tracking Info</small>
            <strong>{{ number(payload.takealot.summary.with_tracking_info) }}</strong>
          </div>
        </div>
      </section>

      <section class="dual-panel">
        <article class="logistics-section status-panel">
          <div class="logistics-section-heading compact">
            <div>
              <p>INBOUND STATUS</p>
              <h3>长睿入库状态</h3>
            </div>
          </div>
          <div class="status-list">
            <div v-for="row in payload.w8.inbound_statuses" :key="row.status">
              <span>{{ row.status }}</span><strong>{{ number(row.count) }}</strong>
            </div>
            <p v-if="!payload.w8.inbound_statuses.length">暂无入库状态</p>
          </div>
        </article>

        <article class="logistics-section status-panel">
          <div class="logistics-section-heading compact">
            <div>
              <p>OUTBOUND STATUS</p>
              <h3>长睿出库状态</h3>
            </div>
          </div>
          <div class="status-list">
            <div v-for="row in payload.w8.outbound_statuses" :key="row.status">
              <span>{{ row.status }}</span><strong>{{ number(row.count) }}</strong>
            </div>
            <p v-if="!payload.w8.outbound_statuses.length">暂无出库状态</p>
          </div>
        </article>
      </section>

      <section class="logistics-section table-section">
        <div class="logistics-section-heading">
          <div><p>RECENT INBOUND</p><h3>最近长睿入库单</h3></div>
          <span>头程号与箱唛可用于关联平台货件</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>长睿入库单</th><th>状态</th><th>头程号</th><th>箱唛</th><th>SKU种类</th><th>预报数量</th><th>上架时间</th></tr></thead>
            <tbody>
              <tr v-for="row in payload.w8.recent_inbound" :key="row.order_no">
                <td><strong>{{ text(row.order_no) }}</strong></td>
                <td><span class="status-chip success">{{ text(row.status) }}</span></td>
                <td>{{ text(row.headway_no) }}</td>
                <td>{{ text(row.shipping_mark) }}</td>
                <td>{{ number(row.sku_types) }}</td>
                <td>{{ number(row.forecast_quantity) }}</td>
                <td>{{ text(row.shelf_date) }}</td>
              </tr>
              <tr v-if="!payload.w8.recent_inbound.length"><td colspan="7">暂无入库单</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="logistics-section table-section">
        <div class="logistics-section-heading">
          <div><p>RECENT OUTBOUND</p><h3>最近长睿出库单</h3></div>
          <span>只显示单号、运单和作业状态，不展示收件人地址</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>长睿出库单</th><th>状态</th><th>运单号</th><th>物流类型</th><th>SKU种类</th><th>数量</th><th>创建时间</th></tr></thead>
            <tbody>
              <tr v-for="row in payload.w8.recent_outbound" :key="row.order_no">
                <td><strong>{{ text(row.order_no) }}</strong></td>
                <td><span class="status-chip">{{ text(row.status) }}</span></td>
                <td class="mono">{{ text(row.waybill_no) }}</td>
                <td>{{ text(row.logistics_type) }}</td>
                <td>{{ number(row.sku_types) }}</td>
                <td>{{ number(row.total_quantity) }}</td>
                <td>{{ text(row.created_at) }}</td>
              </tr>
              <tr v-if="!payload.w8.recent_outbound.length"><td colspan="7">暂无出库单</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="logistics-section table-section">
        <div class="logistics-section-heading">
          <div><p>TAKEALOT SHIPMENTS</p><h3>最近平台货件</h3></div>
          <span>发送、实收和破损来自 Shipment Items</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Shipment / PO</th><th>状态</th><th>目的仓</th><th>发送</th><th>实收</th><th>破损</th><th>要求到仓</th><th>Tracking Info</th></tr></thead>
            <tbody>
              <tr v-for="row in payload.takealot.recent_shipments" :key="String(row.shipment_id)">
                <td><strong>#{{ text(row.shipment_id) }}</strong><small>{{ text(row.purchase_order_number) }}</small></td>
                <td><span class="status-chip platform">{{ shipmentState(row) }}</span></td>
                <td>{{ text(row.destination_region) }}</td>
                <td>{{ number(row.quantity_sending) }}</td>
                <td>{{ number(row.quantity_received) }}</td>
                <td>{{ number(row.quantity_damaged) }}</td>
                <td>{{ text(row.due_date) }}</td>
                <td>{{ text(row.tracking_info) }}</td>
              </tr>
              <tr v-if="!payload.takealot.recent_shipments.length"><td colspan="8">暂无平台货件</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-if="payload.w8.warnings.length || payload.boundaries.length" class="boundary-panel">
        <h3>当前口径与待完善项</h3>
        <ul>
          <li v-for="warning in payload.w8.warnings" :key="warning">{{ warning }}</li>
          <li v-for="boundary in payload.boundaries" :key="boundary">{{ boundary }}</li>
        </ul>
      </section>
    </template>
  </section>
</template>

<style scoped>
.logistics-page { display: grid; gap: 18px; }
.logistics-hero { display: flex; justify-content: space-between; gap: 22px; align-items: end; padding: 24px 26px; border: 1px solid #c8d9cf; border-radius: 20px; background: linear-gradient(135deg, #143f32 0%, #28634f 62%, #c99b4a 160%); color: #fff; box-shadow: 0 18px 40px rgb(23 63 49 / 15%); }
.logistics-hero p, .logistics-section-heading p, .relation-copy p { margin: 0 0 7px; color: #d9bc7b; font-size: 11px; font-weight: 800; letter-spacing: .15em; }
.logistics-hero h2 { margin: 0 0 8px; font-size: clamp(22px, 2.7vw, 34px); }
.logistics-hero span { color: #d9e9e2; line-height: 1.65; }
.logistics-hero button { flex: 0 0 auto; min-height: 42px; padding: 0 17px; border: 1px solid rgb(255 255 255 / 35%); border-radius: 11px; color: #173f31; background: #fff8e7; font-weight: 800; cursor: pointer; }
.logistics-hero button:disabled { opacity: .65; cursor: wait; }
.connection-grid, .dual-panel { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.connection-card, .logistics-section, .boundary-panel { border: 1px solid #d4e0da; border-radius: 17px; background: #fff; box-shadow: 0 10px 28px rgb(29 66 51 / 7%); }
.connection-card { display: grid; gap: 10px; padding: 20px; }
.connection-card.disconnected { border-color: #e7c5b8; background: #fff8f4; }
.connection-heading { display: flex; gap: 11px; align-items: center; }
.connection-heading p { margin: 0 0 3px; color: #789084; font-size: 10px; font-weight: 800; letter-spacing: .13em; }
.connection-heading h3 { margin: 0; color: #24483b; font-size: 16px; }
.connection-dot { width: 10px; height: 10px; border-radius: 50%; background: #3aa978; box-shadow: 0 0 0 5px rgb(58 169 120 / 13%); }
.connection-dot.off { background: #c76b45; box-shadow: 0 0 0 5px rgb(199 107 69 / 13%); }
.connection-card > strong { color: #173f31; font-size: 22px; }
.connection-card > span { color: #79523e; }
.connection-card small { color: #728078; }
.logistics-section { padding: 20px; }
.logistics-section-heading { display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 16px; }
.logistics-section-heading.compact { margin-bottom: 10px; }
.logistics-section-heading h3, .relation-copy h3, .boundary-panel h3 { margin: 0; color: #24483b; }
.logistics-section-heading > span { color: #7a8982; font-size: 12px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric-card { display: grid; gap: 5px; min-height: 112px; padding: 15px; border: 1px solid #e0e8e3; border-radius: 13px; background: #f8faf8; }
.metric-card small, .relation-stats small { color: #718078; font-weight: 700; }
.metric-card strong { color: #173f31; font-size: 27px; }
.metric-card span { color: #829088; font-size: 11px; }
.relation-panel { display: grid; grid-template-columns: minmax(230px, .8fr) minmax(0, 1.5fr); gap: 24px; color: #fff; background: linear-gradient(120deg, #fffaf0 0%, #f4ead3 100%); border-color: #dfcca3; }
.relation-copy strong { display: block; margin: 8px 0; color: #9a681e; font-size: 44px; }
.relation-copy > span { color: #6c5b3e; line-height: 1.6; }
.relation-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.relation-stats > div { display: grid; align-content: center; gap: 7px; padding: 16px; border: 1px solid rgb(158 119 47 / 16%); border-radius: 12px; background: rgb(255 255 255 / 66%); }
.relation-stats strong { color: #315f50; font-size: 25px; }
.status-list { display: grid; gap: 8px; }
.status-list div { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-radius: 10px; background: #f4f8f5; color: #4d655a; }
.status-list strong { color: #1f5643; font-size: 18px; }
.status-list p { color: #84928b; }
.table-section { min-width: 0; }
.table-scroll { overflow-x: auto; border: 1px solid #e0e8e3; border-radius: 12px; }
table { width: 100%; min-width: 930px; border-collapse: collapse; }
th, td { padding: 11px 12px; border-bottom: 1px solid #e7ece9; color: #496157; font-size: 12px; text-align: left; vertical-align: top; }
th { color: #718078; background: #f5f8f6; font-size: 11px; white-space: nowrap; }
td strong { display: block; color: #204c3d; }
td small { display: block; margin-top: 4px; color: #829088; }
.mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; color: #275c49; }
.status-chip { display: inline-flex; padding: 4px 8px; border-radius: 999px; color: #71501d; background: #fff0c9; font-weight: 800; white-space: nowrap; }
.status-chip.success { color: #216247; background: #dff2e8; }
.status-chip.platform { color: #315791; background: #e5edf9; }
.boundary-panel { padding: 20px 22px; border-color: #e1cfac; background: #fffaf0; }
.boundary-panel ul { margin: 12px 0 0; padding-left: 20px; color: #6e6048; line-height: 1.75; }
@media (max-width: 1050px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .relation-panel { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .logistics-hero { align-items: stretch; flex-direction: column; } .connection-grid, .dual-panel, .relation-stats { grid-template-columns: 1fr; } .metric-grid { grid-template-columns: 1fr 1fr; } .logistics-section-heading { align-items: start; flex-direction: column; } }
@media (max-width: 460px) { .metric-grid { grid-template-columns: 1fr; } }
</style>
