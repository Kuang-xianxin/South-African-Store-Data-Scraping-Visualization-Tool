<script setup lang="ts">
import { computed } from "vue";
import type { NfProfitResult } from "../types";

const props = defineProps<{ model: NfProfitResult }>();
const result = computed(() => props.model.calculation);
const money = (value: number | undefined, currency = "CNY") => value == null
  ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency }).format(value);
const fees = [
  ["J", "送仓费"], ["K", "佣金（含VAT）"], ["L", "平台运费"],
  ["M", "运费VAT"], ["N", "换标费"], ["P", "调拨费"],
] as const;
</script>

<template>
  <div class="nf-profit">
    <div v-if="result" class="nf-grid">
      <article>
        <small>采购及头程 / 件</small>
        <strong>{{ money(result.cost_rmb) }}</strong>
        <span>已含头程</span>
      </article>
      <article>
        <small>单件总成本</small>
        <strong>{{ money(result.total_cost_rmb) }}</strong>
        <span>含表内费用、广告；按表内汇损折算</span>
      </article>
      <article>
        <small>当前售价利润（按表计算）</small>
        <strong :class="result.profit_rmb < 0 ? 'loss' : 'gain'">{{ money(result.profit_rmb) }}</strong>
        <span>表内利润率 {{ result.margin_percentage.toFixed(2) }}%</span>
      </article>
    </div>
    <p v-else class="nf-unavailable">暂不能计算：{{ model.message }}</p>
    <details>
      <summary>计算明细与来源</summary>
      <template v-if="result">
        <dl>
          <div><dt>当前售价</dt><dd>{{ money(result.cells.D, "ZAR") }}</dd></div>
          <div><dt>单件分摊箱体积</dt><dd>{{ result.cells.F.toFixed(6) }} m³</dd></div>
          <div><dt>体积重 / 实重</dt><dd>{{ result.cells.G.toFixed(3) }} / {{ result.cells.I.toFixed(3) }} kg</dd></div>
          <div v-for="[column, label] in fees" :key="column">
            <dt>{{ label }}</dt><dd>{{ money(result.cells[column], "ZAR") }}</dd>
          </div>
          <div><dt>广告 {{ (result.cells.S * 100).toFixed(0) }}%</dt><dd>{{ money(result.cells.D * result.cells.S, "ZAR") }}</dd></div>
          <div><dt>人民币回款（扣表内费用）</dt><dd>{{ money(result.cells.T) }}</dd></div>
        </dl>
        <p>表内汇率：1 CNY = {{ result.cells.Q }} ZAR；汇损后保留 {{ (result.cells.R * 100).toFixed(0) }}%。</p>
        <p>人民币利润 = 表内回款 − 采购及头程；兰特利润 = 人民币利润 × 汇率 × 汇损后保留比例；利润率 = 兰特利润 ÷ 售价。</p>
      </template>
      <p>{{ model.source.file || "NF毛利计算.xlsx" }} · 利润计算表
        <span v-if="model.source_rows.length"> · 第 {{ model.source_rows.join("、") }} 行</span>
      </p>
      <p>箱规来自「单件CBM 单重」，费率来自「基数」，采购及头程来自「成本&amp;在库统计」。按导入版本计算；未计入表外费用，不等同实际结算净利润。</p>
    </details>
  </div>
</template>

<style scoped>
.nf-profit { color: #334155; }
.nf-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
article { display: grid; gap: 8px; padding: 16px; background: #f3faf6; border: 1px solid #dcebe2; border-radius: 12px; }
small, span { font-size: 13px; }
strong { font-size: 24px; }
.gain { color: #147548; } .loss { color: #b42318; }
details { margin-top: 16px; font-size: 13px; line-height: 1.7; }
summary { cursor: pointer; font-weight: 600; }
dl { max-width: 580px; } dl div { display: flex; justify-content: space-between; gap: 20px; }
dd { margin: 0; text-align: right; }
.nf-unavailable { padding: 14px; background: #fff8e8; border-radius: 10px; }
@media (max-width: 760px) { .nf-grid { grid-template-columns: 1fr; } }
</style>
