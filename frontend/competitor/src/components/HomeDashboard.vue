<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { fetchHomeDashboard } from "../api";
import { homeCoverageLabel, type HomeDashboard } from "../homeDashboard";
import { cachedNumberFormatter } from "../numberFormatters";
import { useLiveUpdates } from "../liveUpdates";
import { formatChinaDateTime } from "../time";
import type { OwnStoreScope, StoreOverviewItem } from "../types";
import HomeMetricChart from "./HomeMetricChart.vue";

const props = defineProps<{ scope: OwnStoreScope; start: string; end: string; storeName: string; storeInventory?: StoreOverviewItem[] }>();
const emit = defineEmits<{ loadingChange: [busy: boolean] }>();
const data = ref<HomeDashboard | null>(null), busy = ref(false), error = ref("");
watch(busy, value => emit("loadingChange", value), { flush: "sync" });
let revision = 0, controller: AbortController | undefined;
async function load(clear = false) {
  const request = ++revision;
  controller?.abort(); controller = new AbortController();
  if (clear) data.value = null;
  busy.value = true; error.value = "";
  try {
    const result = await fetchHomeDashboard(props.scope, props.start, props.end, controller.signal);
    if (request === revision) data.value = result;
  } catch (reason) {
    if (request === revision && !(reason instanceof DOMException && reason.name === "AbortError")) error.value = reason instanceof Error ? reason.message : "经营指标读取失败";
  } finally { if (request === revision) busy.value = false; }
  return !error.value;
}
watch(() => [props.scope, props.start, props.end, props.storeName], () => void load(true), { immediate: true });
useLiveUpdates("overview", () => load(), { busy: () => busy.value });
onBeforeUnmount(() => { ++revision; controller?.abort(); });
const number = (value: number | null | undefined) => value == null ? "—" : cachedNumberFormatter("zh-CN", { maximumFractionDigits: 0 }).format(value);
const money = (value: number | null | undefined, prefix = "R") => value == null ? "—" : `${prefix}${number(value)}`;
const rmb = (value: number | null | undefined) => value == null || !data.value?.rate.rate ? "人民币参考值待补" : `≈ ￥${number(value / data.value.rate.rate)}`;
const percent = (value: number | null) => value === null ? "—" : `${value.toFixed(2)}%`;
const financeCards = [
  { key: "current", title: "账户余额", note: "当前账面余额" },
  { key: "held_back", title: "暂缓余额", note: "平台暂缓支付" },
  { key: "available", title: "可用余额", note: "平台可用金额" },
  { key: "paid_out", title: "已回款金额", note: "已采集回款 · 扣除冲回" },
] as const;
const inventoryByStore = computed(() => new Map((props.storeInventory ?? []).map(store => [store.store_code, store.inventory])));
const totalPeriods = computed(() => data.value?.periods ?? []);
</script>

<template>
  <div class="home-dashboard" :aria-busy="busy">
    <div v-if="error" class="home-error" role="status">{{ error }}{{ data ? '，保留最近成功结果。' : '' }} <button type="button" @click="load()">重试</button></div>
    <div v-if="!data && busy" class="home-skeleton" aria-label="正在读取经营指标"><div v-for="i in 10" :key="i" /></div>
    <template v-if="data">
      <div class="home-caption"><span>南非业务日 {{ data.today }} <b>今日持续更新</b></span><span>{{ data.store_count }} 家店铺 · {{ storeName }}</span></div>
      <section class="home-kpis" aria-label="订单统计">
        <article v-for="period in totalPeriods" :key="period.key" class="home-stat" :class="{ featured: period.key === 'today' }">
          <div class="stat-label"><span>{{ period.label }}订单</span><span class="stat-icon" aria-hidden="true">{{ period.key === 'today' ? '↗' : '▤' }}</span></div>
          <div class="stat-value">{{ number(period.orders) }}<small>单</small></div>
          <p>{{ number(period.units) }} 件 <span>／ {{ number(period.sold_skus) }} SKU 出单</span></p>
          <div v-if="period.key !== 'total'" class="stat-ratios"><div><span>库存动销率</span><b>{{ percent(period.inventory_sell_through) }}</b></div><div><span>SKU出单率</span><b>{{ percent(period.sku_selling_rate) }}</b></div></div>
          <small class="stat-coverage">{{ homeCoverageLabel(period) }}{{ period.missing_order_ids ? ` · ${period.missing_order_ids} 行缺订单编号` : '' }}</small>
        </article>
      </section>
      <section class="home-kpis home-revenues" aria-label="销售额统计">
        <article v-for="period in totalPeriods" :key="period.key" class="home-stat" :class="{ featured: period.key === 'today' }">
          <span class="stat-label">{{ period.label }}销售额</span><div class="stat-value amount">{{ money(period.revenue) }}</div><p>{{ rmb(period.revenue) }}</p>
          <small class="stat-coverage">{{ period.start }}{{ period.end !== period.start ? ` — ${period.end}` : '' }}{{ period.missing_price_lines ? ` · ${period.missing_price_lines} 行金额缺失` : '' }}</small>
        </article>
      </section>
      <slot name="forecast" />
      <details class="home-methods"><summary>指标口径与汇率</summary><div>
        <p>订单数按「店铺 + 订单编号」去重，件数取订单行数量；销售额直接汇总订单行金额，不再乘数量，包含后续取消/退货前的下单口径。累计从本库 {{ data.history_start }} 起，不代表账户全生命周期。</p>
        <p>库存动销率 = 期间下单件数 ÷（期间下单件数 + 期末官方仓可售库存），用于动销参考。SKU出单率 = 出单店铺SKU ÷（期末有货店铺SKU与期间出单店铺SKU的并集）；跨店分别计数，期间售罄或已退出商品保留在分母。期间销售或期末库存覆盖不完整时，比例显示“—”。</p>
        <p>卡片本月包含今日；下方日趋势及销售来源明细只展示已结束的南非业务日。所选日期作用于日趋势，12个月图以所选截止月结束，固定今日/本月卡片仍按当前日期。</p>
        <p v-if="data.rate.rate">1 人民币 ≈ R{{ data.rate.rate }} · 汇率日期 {{ data.rate.rate_date }} · {{ formatChinaDateTime(data.rate.fetched_at) }} 获取{{ data.rate.stale ? '（缓存已超过1小时）' : '' }}。仅供估算，非交易结算价。</p><p v-else>尚无可用汇率，人民币换算与兰特毛利保持缺失。</p>
      </div></details>
      <section class="home-funds" aria-label="资金总览">
        <article v-for="card in financeCards" :key="card.key" class="home-fund" :class="card.key">
          <span>{{ card.title }}</span><strong>{{ money(data.finance.totals[card.key]) }}</strong><p>{{ rmb(data.finance.totals[card.key]) }}</p><small>{{ card.note }} · {{ card.key === 'paid_out' ? data.finance.payout_coverage : data.finance.balance_coverage }}/{{ data.store_count }} 店有记录</small>
        </article>
      </section>
      <details class="home-methods finance-details"><summary>查看各店资金、回款覆盖与采集时间</summary><div class="home-table-wrap"><table><thead><tr><th>店铺</th><th>账户余额</th><th>暂缓余额</th><th>可用余额</th><th>已回款</th><th>采集 / 覆盖</th></tr></thead><tbody><tr v-for="store in data.finance.stores" :key="store.store_code"><td>{{ store.store_name }}</td><td>{{ money(store.current) }}</td><td>{{ money(store.held_back) }}</td><td>{{ money(store.available) }}</td><td>{{ money(store.paid_out) }}</td><td><span>{{ formatChinaDateTime(store.captured_at) }}</span><small v-if="store.latest_status !== 'success'">最近刷新未完成，显示最近成功记录</small><small>回款 {{ store.payout_start?.slice(0, 10) ?? '待采集' }} — {{ store.payout_end?.slice(0, 10) ?? '—' }}</small></td></tr></tbody></table></div></details>
      <section class="home-warehouse">
        <header><div><span class="home-kicker">WAREHOUSE</span><h3>官方仓库存</h3></div><span>平台可售库存 · {{ data.warehouse.stores.filter(store => store.stock !== null).length }}/{{ data.store_count }} 店有记录</span></header>
        <div class="warehouse-kpis">
          <article><span>官方仓库存总数</span><strong>{{ number(data.warehouse.totals.stock) }} <small>件</small></strong><p>参与毛利计算 {{ data.warehouse.profit_skus }} 个店铺SKU</p></article>
          <article><span>货物价值</span><strong>{{ money(data.warehouse.totals.cost_rmb, '￥') }}</strong><p>有效采购价 × 可售库存{{ data.warehouse.missing_cost_skus ? ' · 已知成本部分合计' : '' }}</p></article>
          <article><span>潜在毛利 <small>估算</small></span><strong>{{ money(data.warehouse.totals.gross_zar) }}</strong><p>{{ rmb(data.warehouse.totals.gross_zar) }} · 按现价售出</p></article>
        </div>
        <div v-if="data.warehouse.missing_cost_skus" class="home-data-note">{{ data.warehouse.missing_cost_skus }} 个有库存店铺SKU缺成本，未计入货值与毛利。</div>
        <div v-if="data.warehouse.stores.some(store => store.stock === null || store.stock_missing_offers > 0)" class="home-data-note">库存覆盖不完整，当前展示已知部分；缺失店铺或Offer见下表。</div>
        <div class="home-table-wrap"><table><thead><tr><th>店铺</th><th>库存总数</th><th>CPT</th><th>JHB</th><th>DBN</th><th v-if="storeInventory?.length">在途</th><th v-if="storeInventory?.length">收货中</th><th>货物价值（￥）</th><th>潜在毛利（R）</th><th>参与毛利 SKU</th><th>成本缺失</th></tr></thead><tbody><tr v-for="store in data.warehouse.stores" :key="store.store_code"><td><b>{{ store.store_name }}</b><small>{{ formatChinaDateTime(store.captured_at) }}</small><small v-if="store.stock_missing_offers">{{ store.stock_missing_offers }} 个Offer库存缺失</small></td><td>{{ number(store.stock) }}</td><td v-for="region in ['CPT', 'JHB', 'DBN']" :key="region" :title="`仓库明细采集：${formatChinaDateTime(store.regions_captured_at)}`">{{ number(store.regions[region] ?? (store.regions_complete ? 0 : null)) }}</td><td v-if="storeInventory?.length">{{ number(inventoryByStore.get(store.store_code)?.platform_stock_on_way) }}<small v-if="(inventoryByStore.get(store.store_code)?.platform_stock_on_way_coverage ?? 0) < (inventoryByStore.get(store.store_code)?.offer_count ?? 0)">部分覆盖</small></td><td v-if="storeInventory?.length">{{ number(inventoryByStore.get(store.store_code)?.platform_stock_in_receiving) }}<small v-if="(inventoryByStore.get(store.store_code)?.platform_stock_in_receiving_coverage ?? 0) < (inventoryByStore.get(store.store_code)?.offer_count ?? 0)">部分覆盖</small></td><td>{{ money(store.cost_rmb, '￥') }}</td><td>{{ money(store.gross_zar) }}</td><td>{{ store.profit_skus }}</td><td :class="{ missing: store.missing_cost_skus }">{{ store.missing_cost_skus || '—' }}</td></tr></tbody></table></div>
        <p class="warehouse-footnote">毛利 = 现价销售收入 − 已知采购成本；未扣平台费、头程、税费等，不是净利润。</p>
        <slot name="warehouse-logistics" />
      </section>
      <slot name="store-comparison" />
      <section class="home-charts" aria-label="订单与销售趋势">
        <HomeMetricChart title="每日订单趋势" :points="data.daily" metric="orders" />
        <HomeMetricChart title="每日销售额趋势" :points="data.daily" metric="revenue" />
        <HomeMetricChart title="月度订单统计" :points="data.monthly" metric="orders" bars />
        <HomeMetricChart title="月度销售额统计" :points="data.monthly" metric="revenue" bars />
      </section>
      <slot name="revenue-evidence" />
    </template>
  </div>
</template>

<style scoped>
.home-dashboard{display:grid;gap:14px;min-width:0}.home-caption{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:12px}.home-caption b{font-size:10px;font-weight:500;padding:3px 7px;background:#e0eedf;color:#306744;border-radius:5px;margin-left:6px}
.home-kpis,.home-skeleton{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.home-stat{min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:17px 16px 13px;transition:transform 180ms ease,box-shadow 180ms ease,border-color 180ms ease}.home-stat.featured{background:#f1f7e9;border-color:#b9d19b}.stat-label{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#5a6f60;gap:5px}.stat-icon{font-size:16px;color:#589069}.stat-value{font-size:clamp(23px,2.25vw,35px);font-weight:700;letter-spacing:-.035em;margin:10px 0 6px;font-variant-numeric:tabular-nums;color:#183e2b;white-space:nowrap}.stat-value small{font-size:11px;font-weight:400;margin-left:6px;color:#748074}.stat-value.amount{font-size:clamp(18px,1.65vw,27px)}.home-stat p{font-size:11px;margin:0;color:#506b58}.home-stat p span{color:#7a867d}.stat-ratios{display:grid;gap:7px;margin:14px 0 11px;padding-top:12px;border-top:1px solid #dce5d6;font-size:11px}.stat-ratios>div{display:flex;justify-content:space-between;gap:4px}.stat-ratios span{color:#7a857c}.stat-ratios b{font-variant-numeric:tabular-nums;font-weight:600;color:#486d4f}.stat-coverage{display:block;font-size:10px;color:#859087;line-height:1.5;margin-top:10px}.home-revenues .home-stat{padding-top:14px;padding-bottom:12px}
.home-methods{font-size:12px;color:var(--muted)}.home-methods summary{cursor:pointer;width:fit-content}.home-methods>div{padding:12px 16px;border:1px solid var(--line);background:#f6f9f3;border-radius:10px;margin-top:10px;line-height:1.8}.home-methods p:last-child{margin-bottom:0}
.home-funds{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:6px}.home-fund{background:#f9fbf6;border:1px solid var(--line);border-radius:14px;padding:20px;display:grid;gap:8px}.home-fund>span{font-size:12px;color:#657862}.home-fund strong{font-size:clamp(22px,2vw,32px);letter-spacing:-.025em;font-variant-numeric:tabular-nums}.home-fund p{font-size:12px;margin:0;color:#6c7b69}.home-fund small{font-size:10px;color:#899383}.home-fund.current{background:#214a35;color:#f1f7e9;border-color:#214a35}.home-fund.current>span,.home-fund.current p,.home-fund.current small{color:#cbddbd}.home-fund.available{background:#f0f3de}.home-fund.held_back strong{color:#927335}
.home-warehouse{border:1px solid var(--line);border-radius:16px;padding:21px;background:var(--paper);min-width:0}.home-warehouse header{display:flex;justify-content:space-between;gap:12px;align-items:center}.home-kicker{font:10px Consolas,monospace;letter-spacing:.12em;color:#859075}.home-warehouse h3{font-size:17px;margin:6px 0 0}.home-warehouse header>span{font-size:12px;color:var(--muted)}.warehouse-kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin:24px 0}.warehouse-kpis article{display:grid;gap:8px;border-right:1px solid var(--line);padding-right:12px}.warehouse-kpis article:last-child{border:0}.warehouse-kpis span{font-size:12px;color:var(--muted)}.warehouse-kpis strong{font-size:clamp(23px,2vw,32px);color:#254f36;letter-spacing:-.025em;font-variant-numeric:tabular-nums}.warehouse-kpis small{font-size:11px;font-weight:400}.warehouse-kpis p{font-size:11px;color:#818b7a;margin:0}.home-table-wrap{overflow-x:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}th,td{padding:13px 12px;text-align:right;border-bottom:1px solid #e4e9df}th{background:#f0f4eb;color:#6b7863;font-size:11px;font-weight:500}th:first-child,td:first-child{text-align:left}td:first-child{min-width:175px}td b{font-weight:500}td small{display:block;font-size:10px;color:#8a9285;line-height:1.6}.missing{color:#b87926}.home-data-note{font-size:12px;color:#996e28;padding:9px 12px;background:#f7f0df;border-radius:8px;margin-bottom:12px}.warehouse-footnote{font-size:11px;line-height:1.7;color:#7a8675;margin:12px 0 0}.home-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.home-continuation{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;border-top:1px solid var(--line);padding-top:24px}.home-continuation h3{font-size:17px;margin:0}.home-continuation span{font-size:12px;color:var(--muted)}.home-error{font-size:13px;color:#9d422b;background:#fff0e9;padding:12px;border-radius:10px}.home-error button{border:0;background:transparent;color:inherit;text-decoration:underline;cursor:pointer}.home-skeleton>div{height:190px;background:#e5ebdf;border-radius:14px}
@media(hover:hover){.home-stat:hover{transform:translateY(-2px);box-shadow:0 8px 18px #24432c0d;border-color:#b6c9ad}}
@media(max-width:1150px){.home-kpis,.home-skeleton{grid-template-columns:repeat(3,minmax(0,1fr))}.home-funds{grid-template-columns:repeat(2,minmax(0,1fr))}.stat-value.amount{font-size:24px}}
@media(max-width:700px){.home-kpis,.home-skeleton{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.home-stat{padding:13px 12px}.home-kpis .home-stat:first-child{grid-column:1/-1}.home-kpis .home-stat:first-child .stat-value{font-size:29px}.home-funds{gap:9px}.home-fund{padding:15px 12px}.home-fund strong{font-size:22px}.home-charts{grid-template-columns:1fr}.home-caption,.home-continuation{flex-wrap:wrap}.home-warehouse{padding:16px 12px}.warehouse-kpis{grid-template-columns:1fr;gap:16px;margin:20px 0}.warehouse-kpis article{border-right:0;border-bottom:1px solid var(--line);padding-bottom:14px}.home-warehouse header>span{font-size:10px}.home-revenues .stat-value.amount{font-size:21px}}
@media(prefers-reduced-motion:reduce){.home-stat{transition:none}.home-stat:hover{transform:none}}
</style>
