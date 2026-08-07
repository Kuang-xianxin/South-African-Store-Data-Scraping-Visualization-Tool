<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import {
  createPlatformWarehouseDirect,
  executePlatformWarehouseAction,
  fetchPlatformWarehouse,
  logoutPlatformWarehousePortal,
  preparePlatformWarehouseAction,
  verifyPlatformWarehouseOtpAndCreate,
} from "../api";
import type { PlatformWarehouseUpstreamAction } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { formatChinaDateTime } from "../time";
import type {
  PlatformWarehouseDraft,
  PlatformWarehouseLinkedShipment,
  PlatformWarehouseOffer,
  PlatformWarehousePayload,
} from "../types";

defineOptions({ name: "PlatformWarehousePage" });
const props = defineProps<{
  canManage?: boolean;
  onPermissionDenied?: () => void;
}>();

type Quantities = {
  cpt_quantity: number;
  jhb_quantity: number;
  dbn_quantity: number;
};

const payload = ref<PlatformWarehousePayload | null>(null);
const loading = ref(true);
const saving = ref(false);
const error = ref("");
const message = ref("");
const activeTab = ref<"create" | "shipments">("create");
const offerQuery = ref("");
const shipmentQuery = ref("");
const quantities = ref<Record<string, Quantities>>({});
const draftNote = ref("");
const failedImages = ref<Set<string>>(new Set());
const portalOtp = ref("");
const pendingOtpDraft = ref<PlatformWarehouseDraft | null>(null);
const otpDestination = ref<string | null>(null);
const directRequestId = ref(crypto.randomUUID());
const pendingAction = ref<Awaited<ReturnType<typeof preparePlatformWarehouseAction>> | null>(null);
const actionConfirmation = ref("");
const actionTracking = ref("");

const filteredOffers = computed(() => {
  const needle = offerQuery.value.trim().toLowerCase();
  const offers = payload.value?.offers ?? [];
  if (!needle) return offers.slice(0, 200);
  return offers.filter((offer) =>
    [offer.sku, offer.tsin_id, offer.title, offer.offer_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle)),
  ).slice(0, 200);
});

const selectedLines = computed(() => (payload.value?.offers ?? [])
  .map((offer) => ({ offer, quantity: quantities.value[offer.offer_id] }))
  .filter((item): item is { offer: PlatformWarehouseOffer; quantity: Quantities } =>
    Boolean(item.quantity) && quantityTotal(item.quantity) > 0,
  ));

const selectedTotal = computed(() => selectedLines.value.reduce(
  (sum, item) => sum + quantityTotal(item.quantity),
  0,
));

const filteredDrafts = computed(() => {
  const needle = shipmentQuery.value.trim().toLowerCase();
  return (payload.value?.drafts ?? []).filter((draft) => {
    if (!needle) return true;
    return [
      draft.draft_number,
      draft.po_number,
      draft.platform_shipment_id,
      draft.tracking_reference,
      ...draft.lines.flatMap((line) => [line.sku, line.tsin_id, line.title]),
      ...draft.shipments.flatMap((shipment) => [shipment.shipment_id, shipment.reference]),
    ].filter(Boolean).some((value) => String(value).toLowerCase().includes(needle));
  });
});

const filteredPlatformShipments = computed(() => {
  const needle = shipmentQuery.value.trim().toLowerCase();
  return (payload.value?.platform_shipments ?? []).filter((shipment) => {
    if (!needle) return true;
    return [
      shipment.shipment_id,
      shipment.purchase_order_number,
      shipment.reference,
      shipment.destination_region,
      shipment.purchase_order_state,
    ].filter(Boolean).some((value) => String(value).toLowerCase().includes(needle));
  });
});

onMounted(() => void load());

async function load() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchPlatformWarehouse();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "约平台仓数据读取失败";
  } finally {
    loading.value = false;
  }
}

function requireManage() {
  if (props.canManage) return true;
  props.onPermissionDenied?.();
  return false;
}

async function verifyOtpAndCreate() {
  const draft = pendingOtpDraft.value;
  if (!draft || !requireManage()) return;
  saving.value = true;
  try {
    const result = await verifyPlatformWarehouseOtpAndCreate(draft.id, portalOtp.value.trim());
    if (payload.value) payload.value.portal = result.portal;
    portalOtp.value = "";
    pendingOtpDraft.value = null;
    otpDestination.value = null;
    finishDirectCreate(result.draft);
    message.value = "2FA 验证成功，Takealot 发货单草稿已创建。";
    await load();
    activeTab.value = "shipments";
  } catch (reason) {
    message.value = reason instanceof Error ? reason.message : "2FA 验证或平台草稿创建失败";
  } finally {
    saving.value = false;
  }
}

async function logoutPortal() {
  if (!requireManage()) return;
  saving.value = true;
  try {
    const result = await logoutPlatformWarehousePortal();
    if (payload.value) payload.value.portal = result.portal;
    pendingOtpDraft.value = null;
    pendingAction.value = null;
    message.value = "Seller Portal 临时会话已清除。";
  } catch (reason) {
    message.value = reason instanceof Error ? reason.message : "退出失败";
  } finally {
    saving.value = false;
  }
}

function quantityFor(offerId: string): Quantities {
  return quantities.value[offerId] ?? {
    cpt_quantity: 0,
    jhb_quantity: 0,
    dbn_quantity: 0,
  };
}

function setQuantity(offerId: string, region: keyof Quantities, value: string) {
  const parsed = Math.max(0, Math.min(10_000, Math.trunc(Number(value) || 0)));
  quantities.value = {
    ...quantities.value,
    [offerId]: { ...quantityFor(offerId), [region]: parsed },
  };
}

function removeSelected(offerId: string) {
  const next = { ...quantities.value };
  delete next[offerId];
  quantities.value = next;
}

async function createDraftDirect() {
  if (!requireManage()) return;
  if (!selectedLines.value.length) {
    message.value = "请至少为一个商品填写 CPT、JHB 或 DBN 补货数量。";
    return;
  }
  if (selectedTotal.value > (payload.value?.portal.max_total_quantity ?? 0)) {
    message.value = `总数量超过安全上限 ${payload.value?.portal.max_total_quantity}，请拆单。`;
    return;
  }
  saving.value = true;
  message.value = "";
  try {
    const result = await createPlatformWarehouseDirect({
      client_request_id: directRequestId.value,
      lines: selectedLines.value.map(({ offer, quantity }) => ({
        offer_id: offer.offer_id,
        ...quantity,
      })),
      note: draftNote.value.trim(),
    });
    if (payload.value) payload.value.portal = result.portal;
    if (result.state === "need_2fa") {
      pendingOtpDraft.value = result.draft;
      otpDestination.value = result.otp_destination ?? result.portal.credential_email;
      portalOtp.value = "";
      message.value = "Takealot 要求 2FA；请输入平台刚发送的验证码，验证后将自动继续创建。";
      await load();
      return;
    }
    finishDirectCreate(result.draft);
    message.value = `${result.draft.draft_number} 已直接创建为 Takealot 发货单草稿。`;
    await load();
    activeTab.value = "shipments";
  } catch (reason) {
    message.value = reason instanceof Error ? reason.message : "Takealot 发货单草稿创建失败";
  } finally {
    saving.value = false;
  }
}

function finishDirectCreate(_draft: PlatformWarehouseDraft) {
  quantities.value = {};
  draftNote.value = "";
  directRequestId.value = crypto.randomUUID();
}

function openOtpForDraft(draft: PlatformWarehouseDraft) {
  pendingOtpDraft.value = draft;
  otpDestination.value = payload.value?.portal.otp_destination
    ?? payload.value?.portal.credential_email
    ?? null;
  portalOtp.value = "";
}

async function prepareAction(
  shipment: PlatformWarehouseLinkedShipment,
  action: PlatformWarehouseUpstreamAction,
) {
  if (!requireManage()) return;
  saving.value = true;
  try {
    pendingAction.value = await preparePlatformWarehouseAction(shipment.shipment_id, action);
    actionConfirmation.value = "";
    actionTracking.value = shipment.tracking_reference ?? "";
    message.value = action === "confirm_po"
      ? "Takealot PO 预览已读取；请核对后输入 Shipment ID。"
      : "操作预检已完成；请输入 Shipment ID 二次确认。";
  } catch (reason) {
    message.value = reason instanceof Error ? reason.message : "操作预检失败";
  } finally {
    saving.value = false;
  }
}

async function submitAction() {
  const approval = pendingAction.value;
  if (!approval) return;
  saving.value = true;
  try {
    await executePlatformWarehouseAction(approval.shipment_id, {
      action: approval.action,
      approval_token: approval.approval_token,
      confirmation_text: actionConfirmation.value.trim(),
      tracking_reference: actionTracking.value.trim(),
    });
    pendingAction.value = null;
    actionConfirmation.value = "";
    actionTracking.value = "";
    message.value = `${actionLabel(approval.action)}已提交 Takealot 并记录审计。`;
    await load();
  } catch (reason) {
    message.value = reason instanceof Error ? reason.message : "平台操作失败";
  } finally {
    saving.value = false;
  }
}

function quantityTotal(quantity: Quantities) {
  return quantity.cpt_quantity + quantity.jhb_quantity + quantity.dbn_quantity;
}

function imageUrl(offer: Pick<PlatformWarehouseOffer, "image_url">) {
  const source = String(offer.image_url ?? "").trim();
  return source && !failedImages.value.has(source)
    ? productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list)
    : "";
}

function markImageFailed(source: string | null) {
  const value = String(source ?? "").trim();
  if (!value) return;
  failedImages.value = new Set([...failedImages.value, value]);
}

function number(value: number | null | undefined) {
  return typeof value === "number" ? new Intl.NumberFormat("zh-CN").format(value) : "—";
}

function dateTime(value: string | null | undefined) {
  return value ? formatChinaDateTime(value) : "—";
}

function platformState(shipment: PlatformWarehousePayload["platform_shipments"][number]) {
  if (shipment.cancelled) return "已取消";
  if (shipment.archived) return "已归档";
  if (shipment.shipped) return "已发货";
  return shipment.purchase_order_state || "待处理";
}

function actionLabel(action: PlatformWarehouseUpstreamAction) {
  return { confirm_po: "确认 PO", confirm_shipped: "确认已发货", archive: "确认归档" }[action];
}
</script>

<template>
  <section class="platform-warehouse-page">
    <header class="module-header">
      <div>
        <p class="eyebrow">平台仓补货</p>
        <h2>约平台仓</h2>
        <p>点击一次直接创建 Takealot 发货单草稿；仅在平台要求 2FA 时暂停并弹出验证码。</p>
      </div>
      <button class="secondary-button" type="button" :disabled="loading" @click="load">刷新数据</button>
    </header>

    <div v-if="payload" class="boundary-notice" role="note">
      <strong>安全边界：</strong>{{ payload.capability.message }}
    </div>
    <p v-if="error" class="error-banner">{{ error }}</p>
    <p v-if="message" class="action-banner">{{ message }}</p>

    <section v-if="payload" class="panel portal-session">
      <div class="portal-copy">
        <h3>Takealot 创建会话</h3>
        <p>
          总开关：{{ payload.portal.enabled ? "已启用" : "关闭" }} ·
          会话：{{ payload.portal.authenticated ? "已登录，可直接创建" : payload.portal.requires_otp ? "等待 2FA" : "未登录，创建时自动验证" }} ·
          单次总量上限：{{ payload.portal.max_total_quantity }}
        </p>
        <small v-if="payload.portal.credential_configured">店铺凭据：{{ payload.portal.credential_email }}；密码只保存在服务器 Windows 凭据管理器，页面不会读取。</small>
        <small v-else>当前店铺尚未在服务器配置 Portal 凭据；已有有效会话仍可直接创建，否则无法触发 2FA。</small>
        <small v-if="payload.portal.credential_error" class="row-error">{{ payload.portal.credential_error }}</small>
      </div>
      <button v-if="payload.portal.authenticated" class="secondary-button" type="button" :disabled="saving" @click="logoutPortal">清除当前会话</button>
      <strong v-else-if="!payload.portal.enabled" class="disabled-copy">真实写入总开关关闭；不会访问 Seller Portal。</strong>
      <strong v-else-if="payload.portal.requires_otp" class="disabled-copy">平台验证码已发送，请从等待 2FA 的创建记录继续。</strong>
      <strong v-else class="disabled-copy">点击创建时自动检查登录状态。</strong>
    </section>

    <nav class="warehouse-tabs" aria-label="约平台仓页面">
      <button type="button" :class="{ active: activeTab === 'create' }" @click="activeTab = 'create'">创建发货单草稿</button>
      <button type="button" :class="{ active: activeTab === 'shipments' }" @click="activeTab = 'shipments'">发货单与 PO</button>
    </nav>

    <div v-if="loading" class="empty-state">正在读取当前店铺商品与草稿…</div>

    <template v-else-if="payload && activeTab === 'create'">
      <div class="create-grid">
        <section class="panel product-panel">
          <div class="panel-heading">
            <div><h3>商品清单</h3><p>最终可约数量只认 Takealot 服务端预审。</p></div>
            <input v-model="offerQuery" type="search" placeholder="SKU / TSIN / 商品名称 / Offer ID" />
          </div>
          <div class="table-scroll product-table-scroll">
            <table>
              <thead><tr><th>商品</th><th>平台库存</th><th>CPT</th><th>JHB</th><th>DBN</th></tr></thead>
              <tbody>
                <tr v-for="offer in filteredOffers" :key="offer.offer_id">
                  <td>
                    <div class="product-cell">
                      <img v-if="imageUrl(offer)" :src="imageUrl(offer)" width="56" height="56" loading="lazy" decoding="async" alt="" @error="markImageFailed(offer.image_url)" />
                      <span v-else class="image-placeholder">暂无图片</span>
                      <div><strong>{{ offer.title || "未命名商品" }}</strong><small>SKU {{ offer.sku || "—" }} · TSIN {{ offer.tsin_id || "—" }}</small><small class="capacity-note">{{ offer.capacity_reason }}</small></div>
                    </div>
                  </td>
                  <td><strong>{{ number(offer.takealot_available_stock) }}</strong><small>在途 {{ number(offer.takealot_stock_on_way) }} · 收货中 {{ number(offer.takealot_stock_in_receiving) }}</small></td>
                  <td v-for="region in (['cpt_quantity', 'jhb_quantity', 'dbn_quantity'] as const)" :key="region">
                    <input class="quantity-input" type="number" min="0" max="10000" :value="quantityFor(offer.offer_id)[region]" :disabled="!canManage" @input="setQuantity(offer.offer_id, region, ($event.target as HTMLInputElement).value)" />
                  </td>
                </tr>
                <tr v-if="!filteredOffers.length"><td colspan="5" class="empty-row">没有匹配商品</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <aside class="panel selection-panel">
          <div class="panel-heading compact"><div><h3>补货清单</h3><p>{{ selectedLines.length }} 个商品 · 共 {{ number(selectedTotal) }} 件</p></div></div>
          <div v-if="selectedLines.length" class="selected-list">
            <article v-for="item in selectedLines" :key="item.offer.offer_id" class="selected-item">
              <div><strong>{{ item.offer.title || item.offer.sku }}</strong><small>SKU {{ item.offer.sku || "—" }}</small></div>
              <button type="button" aria-label="移出补货清单" @click="removeSelected(item.offer.offer_id)">×</button>
              <p>CPT {{ item.quantity.cpt_quantity }} · JHB {{ item.quantity.jhb_quantity }} · DBN {{ item.quantity.dbn_quantity }}</p>
            </article>
          </div>
          <div v-else class="empty-state compact-empty">在左侧填写数量后，商品会进入这里。</div>
          <label class="field-label">草稿说明<textarea v-model="draftNote" maxlength="2000" placeholder="可选：本次补货背景或核对说明" /></label>
          <button class="primary-button full" type="button" :disabled="saving || !payload.portal.enabled || (!payload.portal.authenticated && !payload.portal.credential_configured) || !selectedLines.length || selectedTotal > payload.portal.max_total_quantity" @click="createDraftDirect">{{ saving ? "正在校验并创建…" : "直接创建发货单草稿" }}</button>
          <p class="fine-print">点击后会真实请求 Takealot：已有会话直接创建；没有会话时后台发起登录，平台要求 2FA 才弹验证码。服务端分仓不完整或数量变化会拒绝创建。</p>
        </aside>
      </div>
    </template>

    <template v-else-if="payload">
      <section class="panel shipment-toolbar">
        <input v-model="shipmentQuery" type="search" placeholder="草稿号 / PO / Shipment ID / SKU" />
        <span>平台只读快照同步：{{ dateTime(payload.platform_snapshot_synced_at) }}</span>
      </section>

      <section class="panel">
        <div class="panel-heading"><div><h3>补货草稿与平台 Shipment</h3><p>只有本模块创建并审计的 Shipment 才开放状态操作。</p></div></div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>草稿</th><th>商品 / 数量</th><th>平台 Shipment</th><th>状态</th><th>安全操作</th></tr></thead>
            <tbody>
              <tr v-for="draft in filteredDrafts" :key="draft.id">
                <td><strong>{{ draft.draft_number }}</strong><small>{{ dateTime(draft.created_at) }} · {{ draft.created_by }}</small><small v-if="draft.last_error" class="row-error">{{ draft.last_error }}</small></td>
                <td>
                  <strong>{{ draft.line_count }} 个商品 · {{ number(draft.quantity_totals.cpt_quantity + draft.quantity_totals.jhb_quantity + draft.quantity_totals.dbn_quantity) }} 件</strong>
                  <small>CPT {{ draft.quantity_totals.cpt_quantity }} · JHB {{ draft.quantity_totals.jhb_quantity }} · DBN {{ draft.quantity_totals.dbn_quantity }}</small>
                  <details><summary>查看商品与审计</summary><ul><li v-for="line in draft.lines" :key="line.id">{{ line.sku || line.offer_id }}：CPT {{ line.cpt_quantity }} / JHB {{ line.jhb_quantity }} / DBN {{ line.dbn_quantity }}</li></ul><ul class="audit-list"><li v-for="audit in draft.audits" :key="audit.id">{{ dateTime(audit.created_at) }} · {{ audit.actor_username }} · {{ audit.action_label }}<span v-if="audit.note"> · {{ audit.note }}</span></li></ul></details>
                </td>
                <td>
                  <article v-for="shipment in draft.shipments" :key="shipment.shipment_id" class="linked-shipment">
                    <strong>#{{ shipment.shipment_id }} · {{ shipment.facility_code || shipment.region || "—" }}</strong>
                    <small>{{ shipment.reference || "—" }} · {{ shipment.status_label }}</small>
                    <div class="action-buttons">
                      <button v-if="shipment.status === 'platform_draft'" type="button" :disabled="!payload.portal.authenticated || saving" @click="prepareAction(shipment, 'confirm_po')">确认 PO</button>
                      <button v-if="shipment.status === 'po_confirmed'" type="button" :disabled="!payload.portal.authenticated || !payload.portal.shipped_write_enabled || saving" :title="payload.portal.shipped_write_enabled ? '' : '端点需人工抓包确认后显式启用'" @click="prepareAction(shipment, 'confirm_shipped')">确认已发货</button>
                      <button v-if="shipment.status === 'shipped'" type="button" :disabled="!payload.portal.authenticated || saving" @click="prepareAction(shipment, 'archive')">确认归档</button>
                    </div>
                  </article>
                  <span v-if="!draft.shipments.length">尚未创建平台 Shipment</span>
                </td>
                <td><span class="status-pill" :data-status="draft.status">{{ draft.status_label }}</span><small v-if="draft.review_expires_at">预审有效期 {{ dateTime(draft.review_expires_at) }}</small></td>
                <td>
                  <button v-if="draft.status === 'awaiting_2fa'" class="primary-button" type="button" :disabled="!payload.portal.requires_otp || saving" @click="openOtpForDraft(draft)">填写 2FA 验证码</button>
                  <span v-else-if="['draft', 'reviewed'].includes(draft.status) && draft.upstream_mode === 'guarded_bff'">旧两段式请求已停用，请重新选择商品直接创建</span>
                  <span v-else-if="draft.upstream_mode === 'local_only'">旧本地草稿不可写平台，请重新创建</span>
                  <span v-else-if="draft.status.includes('unknown')">禁止自动重试，请先在 Seller Portal 人工核对</span>
                  <span v-else>按左侧 Shipment 逐项操作</span>
                </td>
              </tr>
              <tr v-if="!filteredDrafts.length"><td colspan="5" class="empty-row">没有匹配草稿</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading"><div><h3>Takealot Shipment 只读快照</h3><p>{{ filteredPlatformShipments.length }} / {{ payload.platform_shipments.length }} 条；普通刷新不会触发上游同步。</p></div></div>
        <div class="table-scroll platform-table-scroll">
          <table>
            <thead><tr><th>Shipment ID</th><th>PO Number</th><th>交付仓库</th><th>Due Date</th><th>入库状态</th><th>发货 / 接收</th><th>Reference</th></tr></thead>
            <tbody>
              <tr v-for="shipment in filteredPlatformShipments" :key="shipment.shipment_id || shipment.reference"><td>{{ shipment.shipment_id || "—" }}</td><td>{{ shipment.purchase_order_number || "—" }}</td><td>{{ shipment.destination_region || "—" }}</td><td>{{ shipment.due_date || "—" }}</td><td>{{ platformState(shipment) }}</td><td>{{ shipment.quantity_sending }} / {{ shipment.quantity_received }}</td><td>{{ shipment.reference || "—" }}</td></tr>
              <tr v-if="!filteredPlatformShipments.length"><td colspan="7" class="empty-row">当前快照没有匹配 Shipment</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <div v-if="pendingOtpDraft" class="modal-backdrop" role="presentation">
      <section class="action-modal" role="dialog" aria-modal="true" aria-label="Takealot 2FA 验证">
        <h3>输入 Takealot 验证码</h3>
        <p>平台要求验证本次登录<span v-if="otpDestination">，验证码已发送至 {{ otpDestination }}</span>。验证成功后会自动继续创建同一张发货单草稿。</p>
        <p class="danger-copy">验证码只用于草稿 {{ pendingOtpDraft.draft_number }}；请勿重复点击创建。</p>
        <label>2FA 验证码<input v-model="portalOtp" inputmode="numeric" autocomplete="one-time-code" maxlength="12" autofocus /></label>
        <div class="modal-actions"><button type="button" :disabled="saving" @click="pendingOtpDraft = null">稍后填写</button><button class="primary-button" type="button" :disabled="saving || !portalOtp.trim()" @click="verifyOtpAndCreate">{{ saving ? "正在验证并创建…" : "验证并继续创建" }}</button></div>
      </section>
    </div>

    <div v-if="pendingAction" class="modal-backdrop" role="presentation" @click.self="pendingAction = null">
      <section class="action-modal" role="dialog" aria-modal="true" :aria-label="actionLabel(pendingAction.action)">
        <h3>{{ actionLabel(pendingAction.action) }}</h3>
        <p>Shipment #{{ pendingAction.shipment_id }} · 该操作会真实写入 Takealot。</p>
        <details v-if="pendingAction.preview"><summary>查看 Takealot PO 预览原始字段</summary><pre>{{ JSON.stringify(pendingAction.preview, null, 2) }}</pre></details>
        <label v-if="pendingAction.action === 'confirm_shipped'">物流单号或发货凭据<input v-model="actionTracking" maxlength="200" /></label>
        <label>请输入 Shipment ID <strong>{{ pendingAction.shipment_id }}</strong><input v-model="actionConfirmation" inputmode="numeric" autocomplete="off" /></label>
        <div class="modal-actions"><button type="button" :disabled="saving" @click="pendingAction = null">取消</button><button class="primary-button" type="button" :disabled="saving || actionConfirmation.trim() !== String(pendingAction.shipment_id) || (pendingAction.action === 'confirm_shipped' && !actionTracking.trim())" @click="submitAction">{{ saving ? "正在提交…" : `确认${actionLabel(pendingAction.action)}` }}</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.platform-warehouse-page { display: grid; gap: 18px; min-width: 0; }
.module-header, .panel-heading, .shipment-toolbar, .portal-session { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.module-header h2, .panel h3 { margin: 0; }
.module-header p, .panel-heading p, .fine-print, .portal-copy p { margin: 6px 0 0; color: var(--text-muted, #64748b); }
.eyebrow { color: #0b73d9 !important; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.boundary-notice, .action-banner, .error-banner { padding: 13px 16px; border-radius: 10px; }
.boundary-notice { background: #eaf6ff; border: 1px solid #80c7ff; color: #164e7a; }
.action-banner { background: #edfdf3; border: 1px solid #86d9a6; color: #166534; }
.error-banner { background: #fff1f2; border: 1px solid #fda4af; color: #9f1239; }
.warehouse-tabs { display: flex; gap: 8px; border-bottom: 1px solid #dbe3ec; }
.warehouse-tabs button { padding: 12px 18px; border: 0; border-bottom: 3px solid transparent; background: transparent; cursor: pointer; }
.warehouse-tabs button.active { color: #0875e1; border-color: #0875e1; font-weight: 700; }
.panel { min-width: 0; padding: 16px; border: 1px solid #e2e8f0; border-radius: 12px; background: #fff; box-shadow: 0 5px 18px rgba(15, 23, 42, .04); }
.portal-copy { min-width: 280px; }
.portal-copy small { display: block; margin-top: 7px; color: #64748b; }
.disabled-copy { max-width: 420px; color: #92400e; }
.create-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 360px); gap: 16px; min-width: 0; }
.panel-heading { margin-bottom: 14px; }
.panel-heading input, .shipment-toolbar input { width: min(420px, 100%); }
input, select, textarea { box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 8px; padding: 9px 10px; background: #fff; color: inherit; }
textarea { width: 100%; min-height: 86px; resize: vertical; }
.table-scroll { overflow: auto; }
.product-table-scroll { max-height: 620px; }
.platform-table-scroll { max-height: 480px; }
table { width: 100%; min-width: 820px; border-collapse: collapse; }
th, td { padding: 11px 10px; border-bottom: 1px solid #e8edf3; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; z-index: 1; background: #f8fafc; color: #334155; }
td small, td strong { display: block; }
td small { margin-top: 4px; color: #64748b; }
.product-cell { display: grid; grid-template-columns: 56px minmax(180px, 1fr); gap: 10px; align-items: center; }
.product-cell img, .image-placeholder { width: 56px; height: 56px; border-radius: 8px; object-fit: contain; background: #f1f5f9; }
.image-placeholder { display: grid; place-items: center; color: #94a3b8; font-size: 12px; }
.capacity-note { color: #b45309 !important; }
.quantity-input { width: 82px; }
.selected-list { display: grid; gap: 9px; max-height: 390px; overflow: auto; }
.selected-item { display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 10px; border-radius: 9px; background: #f8fafc; }
.selected-item p { grid-column: 1 / -1; margin: 0; color: #475569; }
.selected-item small { display: block; color: #64748b; }
.selected-item button { border: 0; background: transparent; font-size: 22px; color: #64748b; cursor: pointer; }
.field-label, .action-modal label { display: grid; gap: 6px; margin-top: 14px; font-weight: 600; }
.primary-button, .secondary-button, .action-buttons button { border: 0; border-radius: 8px; padding: 9px 14px; cursor: pointer; }
.primary-button { background: #0875e1; color: #fff; }
.secondary-button, .action-buttons button { background: #e9f3ff; color: #075faa; }
button:disabled { cursor: not-allowed; opacity: .55; }
.full { width: 100%; margin-top: 14px; }
.fine-print { font-size: 12px; line-height: 1.55; }
.shipment-toolbar { justify-content: flex-start; flex-wrap: wrap; }
.shipment-toolbar span { margin-left: auto; color: #64748b; }
.status-pill { display: inline-flex; padding: 5px 9px; border-radius: 999px; background: #e2e8f0; }
.status-pill[data-status="reviewed"] { background: #fef3c7; color: #92400e; }
.status-pill[data-status="platform_draft"], .status-pill[data-status="po_confirmed"] { background: #dbeafe; color: #1d4ed8; }
.status-pill[data-status="shipped"] { background: #dcfce7; color: #15803d; }
.status-pill[data-status="archived"] { background: #ede9fe; color: #6d28d9; }
.action-buttons { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 7px; }
.linked-shipment { padding: 8px; border-radius: 8px; background: #f8fafc; }
.linked-shipment + .linked-shipment { margin-top: 8px; }
.row-error, .danger-copy { color: #b91c1c !important; font-weight: 600; }
details { margin-top: 7px; }
details ul { margin: 8px 0; padding-left: 18px; }
details pre { max-height: 220px; overflow: auto; padding: 10px; border-radius: 8px; background: #0f172a; color: #e2e8f0; font-size: 11px; }
.audit-list { color: #64748b; font-size: 12px; }
.empty-state, .empty-row { padding: 28px; text-align: center; color: #64748b; }
.compact-empty { padding: 36px 10px; }
.modal-backdrop { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; padding: 18px; background: rgba(15, 23, 42, .52); }
.action-modal { width: min(560px, 100%); max-height: 88vh; overflow: auto; padding: 22px; border-radius: 14px; background: #fff; box-shadow: 0 24px 70px rgba(15, 23, 42, .28); }
.action-modal.wide { width: min(720px, 100%); }
.action-modal h3 { margin: 0; }
.action-modal p { color: #64748b; }
.action-modal input { width: 100%; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.modal-actions > button:not(.primary-button) { border: 0; border-radius: 8px; padding: 9px 14px; }
@media (max-width: 980px) {
  .create-grid { grid-template-columns: 1fr; }
  .selection-panel { order: -1; }
  .module-header, .panel-heading, .portal-session { align-items: flex-start; flex-direction: column; }
  .portal-form { grid-template-columns: 1fr; }
  .panel-heading input { width: 100%; }
  .shipment-toolbar span { width: 100%; margin-left: 0; }
}
</style>
