<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import {
  confirmLogisticsLink,
  fetchLogisticsOverview,
  revokeLogisticsLink,
} from "../api";
import { formatChinaDateTime } from "../time";
import type { LogisticsOverviewPayload } from "../types";

defineOptions({ name: "LogisticsPage" });
const props = defineProps<{
  asOf?: string;
  canManage?: boolean;
  onPermissionDenied?: () => void;
}>();
void props.asOf;

const loading = ref(true);
const refreshing = ref(false);
const error = ref("");
const actionMessage = ref("");
const savingKey = ref("");
const revokingLinkId = ref<number | null>(null);
const revokeNote = ref("");
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

const candidateGroups = computed(() => {
  if (!payload.value) return [];
  return [
    {
      key: "high",
      label: "高置信候选",
      note: "SKU与数量完全一致，30天内且双方唯一",
      items: payload.value.matching.high_confidence_candidates,
    },
    {
      key: "medium",
      label: "中置信候选",
      note: "整单SKU相同但数量不同，60天内待核对拆批或部分发货",
      items: payload.value.matching.medium_confidence_candidates,
    },
    {
      key: "low",
      label: "低置信候选",
      note: "至少一半SKU重合，整单范围不同，仅供人工排查",
      items: payload.value.matching.low_confidence_candidates,
    },
  ].filter((group) => group.items.length > 0);
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

async function confirmCandidate(
  candidate: LogisticsOverviewPayload["matching"]["high_confidence_candidates"][number],
) {
  if (!props.canManage) {
    props.onPermissionDenied?.();
    return;
  }
  savingKey.value = `candidate-${candidate.w8_order_no}-${candidate.takealot_shipment_id}`;
  actionMessage.value = "";
  try {
    await confirmLogisticsLink(candidate.w8_order_no, candidate.takealot_shipment_id);
    actionMessage.value = "候选关系已由人工确认并永久保存。";
    await load(false);
  } catch (reason) {
    actionMessage.value = reason instanceof Error ? reason.message : "物流关联确认失败";
  } finally {
    savingKey.value = "";
  }
}

function beginRevoke(linkId: number) {
  if (!props.canManage) {
    props.onPermissionDenied?.();
    return;
  }
  revokingLinkId.value = linkId;
  revokeNote.value = "";
}

function cancelRevoke() {
  revokingLinkId.value = null;
  revokeNote.value = "";
}

async function submitRevoke(linkId: number) {
  if (!revokeNote.value.trim()) {
    actionMessage.value = "请填写撤销原因。";
    return;
  }
  savingKey.value = `link-${linkId}`;
  actionMessage.value = "";
  try {
    await revokeLogisticsLink(linkId, revokeNote.value.trim());
    actionMessage.value = "关联已撤销，审计记录仍保留。";
    cancelRevoke();
    await load(false);
  } catch (reason) {
    actionMessage.value = reason instanceof Error ? reason.message : "物流关联撤销失败";
  } finally {
    savingKey.value = "";
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

function confidenceLabel(confidence: "high" | "medium" | "low") {
  return confidence === "high" ? "高置信" : confidence === "medium" ? "中置信" : "低置信";
}

function quantityDelta(value: number) {
  if (value === 0) return "数量一致";
  return `Takealot ${value > 0 ? "多" : "少"} ${number(Math.abs(value))} 件`;
}

function providerStatus(
  provider: LogisticsOverviewPayload["w8"] | LogisticsOverviewPayload["takealot"],
  liveLabel: string,
) {
  if (provider.live_connected) return `${liveLabel}本次同步成功`;
  if (provider.data_source === "local_database" && provider.refresh_attempted) {
    return "本次同步失败 · 保留本地快照";
  }
  if (provider.data_source === "local_database") return "本地最近成功快照";
  return "接口与本地快照均不可用";
}
</script>

<template>
  <section class="logistics-page">
    <header class="logistics-hero">
      <div>
        <p>LOCAL SNAPSHOT / MANUAL + SCHEDULED SYNC</p>
        <h2>长睿仓配与 Takealot 货件总览</h2>
        <span>打开页面只读本地快照；随店铺定时采集同步，也可人工读取两边接口。</span>
      </div>
      <button type="button" :disabled="loading || refreshing" @click="load(true)">
        {{ refreshing ? "正在手动同步…" : "手动同步两边接口" }}
      </button>
    </header>

    <div v-if="loading" class="state-card">正在读取本地物流快照……</div>
    <div v-else-if="error" class="state-card error">{{ error }}</div>
    <template v-else-if="payload">
      <section class="connection-grid">
        <article :class="['connection-card', { disconnected: !payload.w8.live_connected }]">
          <div class="connection-heading">
            <span :class="['connection-dot', { off: !payload.w8.live_connected }]"></span>
            <div>
              <p>LONG REACH W8</p>
              <h3>{{ providerStatus(payload.w8, "长睿正式环境") }}</h3>
            </div>
          </div>
          <strong v-if="payload.w8.warehouse">
            {{ payload.w8.warehouse.code }} · {{ payload.w8.warehouse.name }}
          </strong>
          <span v-else>{{ payload.w8.message || "暂无仓库信息" }}</span>
          <small>
            渠道：{{ payload.w8.channels.map((row) => row.name || row.code).join("、") || "暂无" }}
          </small>
          <small>本地最近同步：{{ formatChinaDateTime(payload.w8.synced_at, "暂无") }}</small>
        </article>

        <article :class="['connection-card', { disconnected: !payload.takealot.live_connected }]">
          <div class="connection-heading">
            <span :class="['connection-dot', { off: !payload.takealot.live_connected }]"></span>
            <div>
              <p>TAKEALOT MARKETPLACE</p>
              <h3>{{ providerStatus(payload.takealot, "平台货件接口") }}</h3>
            </div>
          </div>
          <strong>{{ number(payload.takealot.summary.shipments) }} 个 Shipment</strong>
          <span v-if="payload.takealot.message">{{ payload.takealot.message }}</span>
          <small>
            本地最近同步：{{ formatChinaDateTime(payload.takealot.synced_at, "暂无") }} · 北京时间
          </small>
        </article>
      </section>

      <section class="logistics-section">
        <div class="logistics-section-heading">
          <div>
            <p>WAREHOUSE PULSE</p>
            <h3>长睿当前仓配脉搏</h3>
          </div>
          <span>{{ payload.w8.live_connected ? "实时同步 · 本地已存" : "本地历史快照" }}</span>
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
          <h3>两边明确编号直连</h3>
          <strong>{{ number(payload.matching.direct_match_count) }}</strong>
          <span>{{ payload.matching.method }}</span>
        </div>
        <div class="relation-stats">
          <div>
            <small>人工已确认关联</small>
            <strong>{{ number(payload.matching.confirmed_link_count) }}</strong>
          </div>
          <div>
            <small>高置信待确认</small>
            <strong>{{ number(payload.matching.high_confidence_candidate_count) }}</strong>
          </div>
          <div>
            <small>中置信待核对</small>
            <strong>{{ number(payload.matching.medium_confidence_candidate_count) }}</strong>
          </div>
          <div>
            <small>低置信待排查</small>
            <strong>{{ number(payload.matching.low_confidence_candidate_count) }}</strong>
          </div>
          <div>
            <small>可能拆批组合</small>
            <strong>{{ number(payload.matching.split_batch_group_count) }}</strong>
          </div>
        </div>
      </section>

      <p v-if="actionMessage" class="action-message" role="status">{{ actionMessage }}</p>

      <section class="logistics-section candidate-section">
        <div class="logistics-section-heading">
          <div>
            <p>OPERATOR CONFIRMATION</p>
            <h3>分级候选与永久关联</h3>
          </div>
          <span>候选不是平台原生确认关系；人工确认后写入本地审计表</span>
        </div>

        <div v-if="candidateGroups.length" class="candidate-groups">
          <section v-for="group in candidateGroups" :key="group.key" class="candidate-group">
            <header>
              <div>
                <h4>{{ group.label }}（{{ number(group.items.length) }}）</h4>
                <span>{{ group.note }}</span>
              </div>
            </header>
            <div class="candidate-grid">
              <article
                v-for="candidate in group.items"
                :key="`${group.key}-${candidate.w8_order_no}-${candidate.takealot_shipment_id}`"
                :class="['candidate-card', group.key]"
              >
                <div class="candidate-card-heading">
                  <span>{{ group.label }}</span>
                  <strong>{{ candidate.date_gap_days }} 天</strong>
                </div>
                <div class="candidate-route">
                  <div>
                    <small>长睿入库单</small>
                    <strong>{{ candidate.w8_order_no }}</strong>
                    <span>{{ text(candidate.w8_created_at) }} · W8未注明时区</span>
                  </div>
                  <b>→</b>
                  <div>
                    <small>Takealot Shipment / PO</small>
                    <strong>#{{ candidate.takealot_shipment_id }}</strong>
                    <span>{{ text(candidate.takealot_purchase_order_number) }}</span>
                  </div>
                </div>
                <div class="candidate-evidence">
                  <span>
                    共同SKU {{ candidate.shared_sku_lines }} / 长睿{{ candidate.w8_sku_lines }} /
                    Takealot {{ candidate.takealot_sku_lines }}
                  </span>
                  <span>
                    长睿 {{ number(candidate.w8_quantity) }} → Takealot
                    {{ number(candidate.takealot_quantity) }} 件
                  </span>
                  <span>{{ quantityDelta(candidate.quantity_delta) }}</span>
                  <span v-if="candidate.ambiguous" class="warning-evidence">
                    存在歧义：长睿侧{{ candidate.w8_candidate_count }}个 / 平台侧{{ candidate.takealot_candidate_count }}个候选
                  </span>
                  <span v-else>当前档位双方唯一</span>
                </div>
                <p>{{ candidate.method }}</p>
                <button
                  type="button"
                  :disabled="Boolean(savingKey)"
                  @click="confirmCandidate(candidate)"
                >
                  {{
                    savingKey === `candidate-${candidate.w8_order_no}-${candidate.takealot_shipment_id}`
                      ? "正在确认…"
                      : props.canManage
                        ? "人工核对后确认关联"
                        : "当前账号仅可查看"
                  }}
                </button>
              </article>
            </div>
          </section>
        </div>
        <p v-else class="empty-relation">当前没有可供人工核对的分级候选。</p>

        <div v-if="payload.matching.split_batch_groups.length" class="split-groups">
          <h4>可能拆批组合</h4>
          <article v-for="group in payload.matching.split_batch_groups" :key="`${group.w8_order_no}-${group.takealot_shipment_ids.join('-')}`">
            <strong>{{ group.w8_order_no }} ↔ {{ group.shipment_count }} 个 Takealot Shipment</strong>
            <span>#{{ group.takealot_shipment_ids.join('、#') }} · 合计 {{ number(group.w8_quantity) }} 件 · 最大日期差 {{ group.max_date_gap_days }} 天</span>
            <small>{{ group.method }}；这里只提示组合，不自动建立多单关联。</small>
          </article>
        </div>

        <div v-if="payload.matching.confirmed_links.length" class="confirmed-links">
          <h4>已确认关联</h4>
          <article v-for="link in payload.matching.confirmed_links" :key="link.id">
            <div>
              <strong>{{ link.w8_order_no }} ↔ Shipment #{{ link.takealot_shipment_id }}</strong>
              <span>
                {{ confidenceLabel(link.confidence) }} · PO {{ text(link.takealot_purchase_order_number) }} ·
                {{ link.sku_lines }} 个 SKU · 长睿 {{ number(link.w8_quantity) }} / Takealot
                {{ number(link.takealot_quantity) }} 件
              </span>
              <small>
                {{ link.confirmed_by }} 于 {{ formatChinaDateTime(link.confirmed_at, "暂无") }}（北京时间）确认
              </small>
            </div>
            <button type="button" :disabled="Boolean(savingKey)" @click="beginRevoke(link.id)">
              撤销关联
            </button>
            <form v-if="revokingLinkId === link.id" @submit.prevent="submitRevoke(link.id)">
              <label>
                <span>撤销原因</span>
                <input v-model="revokeNote" maxlength="500" placeholder="说明为什么这笔关系不成立" />
              </label>
              <button type="submit" :disabled="savingKey === `link-${link.id}`">
                {{ savingKey === `link-${link.id}` ? "正在撤销…" : "确认撤销" }}
              </button>
              <button type="button" @click="cancelRevoke">取消</button>
            </form>
          </article>
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

      <section
        v-if="payload.w8.warnings.length || payload.takealot.warnings?.length || payload.matching.warnings.length || payload.boundaries.length"
        class="boundary-panel"
      >
        <h3>当前口径与待完善项</h3>
        <ul>
          <li v-for="warning in payload.w8.warnings" :key="warning">{{ warning }}</li>
          <li v-for="warning in payload.takealot.warnings || []" :key="warning">{{ warning }}</li>
          <li v-for="warning in payload.matching.warnings" :key="warning">{{ warning }}</li>
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
.relation-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap: 10px; }
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
.action-message { margin: 0; padding: 12px 16px; border: 1px solid #bcd9c8; border-radius: 12px; color: #235944; background: #eff9f3; font-weight: 700; }
.candidate-section { display: grid; gap: 16px; }
.candidate-groups, .candidate-group { display: grid; gap: 13px; }
.candidate-group > header { display: flex; justify-content: space-between; gap: 12px; align-items: end; padding-top: 4px; border-top: 1px solid #e4ebe7; }
.candidate-group > header h4 { margin: 12px 0 4px; color: #24483b; }
.candidate-group > header span { color: #77867e; font-size: 12px; }
.candidate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.candidate-card { display: grid; gap: 14px; padding: 17px; border: 1px solid #d9c387; border-radius: 14px; background: #fffaf0; }
.candidate-card.medium { border-color: #d9b88f; background: #fff7ef; }
.candidate-card.low { border-color: #cad5dc; background: #f7fafc; }
.candidate-card-heading { display: flex; justify-content: space-between; align-items: center; }
.candidate-card-heading span { padding: 4px 9px; border-radius: 999px; color: #765116; background: #f5dfac; font-size: 11px; font-weight: 800; }
.candidate-card.medium .candidate-card-heading span { color: #8a4d24; background: #f8dbc5; }
.candidate-card.low .candidate-card-heading span { color: #476173; background: #dfeaf0; }
.candidate-card-heading strong { color: #8a5e1e; }
.candidate-route { display: grid; grid-template-columns: 1fr auto 1fr; gap: 12px; align-items: center; }
.candidate-route div { display: grid; gap: 4px; min-width: 0; }
.candidate-route small, .confirmed-links small { color: #7a887f; }
.candidate-route strong { color: #214f3e; overflow-wrap: anywhere; }
.candidate-route span, .candidate-card p, .confirmed-links span { color: #66766e; font-size: 12px; line-height: 1.55; }
.candidate-route b { color: #b18335; }
.candidate-evidence { display: flex; flex-wrap: wrap; gap: 7px; }
.candidate-evidence span { padding: 5px 8px; border-radius: 8px; color: #315e4e; background: #eaf3ed; font-size: 11px; font-weight: 700; }
.candidate-evidence .warning-evidence { color: #8a4d24; background: #fbe6d7; }
.candidate-card button, .confirmed-links button { min-height: 38px; padding: 0 13px; border: 0; border-radius: 9px; color: #fff; background: #27634d; font-weight: 800; cursor: pointer; }
.candidate-card button:disabled, .confirmed-links button:disabled { opacity: .62; cursor: wait; }
.empty-relation { margin: 0; padding: 14px; border-radius: 11px; color: #77867e; background: #f5f8f6; }
.split-groups { display: grid; gap: 9px; padding: 14px; border: 1px dashed #c9b27b; border-radius: 12px; background: #fffaf0; }
.split-groups h4 { margin: 0; color: #6f511f; }
.split-groups article { display: grid; gap: 4px; padding: 11px; border-radius: 9px; background: #fff; }
.split-groups strong { color: #24483b; }
.split-groups span, .split-groups small { color: #6f776f; line-height: 1.5; }
.confirmed-links { display: grid; gap: 10px; padding-top: 4px; }
.confirmed-links h4 { margin: 0; color: #24483b; }
.confirmed-links > article { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 14px; border: 1px solid #dce7e1; border-radius: 12px; }
.confirmed-links > article > div { display: grid; gap: 4px; }
.confirmed-links form { grid-column: 1 / -1; display: flex; gap: 8px; align-items: end; padding-top: 10px; border-top: 1px solid #e5ece8; }
.confirmed-links form label { display: grid; flex: 1; gap: 5px; color: #65766e; font-size: 12px; }
.confirmed-links form input { min-height: 38px; padding: 0 10px; border: 1px solid #cbd9d1; border-radius: 8px; }
@media (max-width: 1050px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .relation-panel { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .logistics-hero { align-items: stretch; flex-direction: column; } .connection-grid, .dual-panel, .relation-stats, .candidate-grid { grid-template-columns: 1fr; } .metric-grid { grid-template-columns: 1fr 1fr; } .logistics-section-heading { align-items: start; flex-direction: column; } .confirmed-links > article { grid-template-columns: 1fr; } .confirmed-links form { align-items: stretch; flex-direction: column; } }
@media (max-width: 460px) { .metric-grid { grid-template-columns: 1fr; } }
</style>
