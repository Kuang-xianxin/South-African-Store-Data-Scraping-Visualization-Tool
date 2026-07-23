<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

import {
  collectCompetitor,
  fetchCompetitorDetail,
  fetchCompetitors,
} from "../api";
import type {
  CollectResult,
  CompetitorDetail,
  CompetitorItem,
} from "../types";
import { formatChinaDateTime } from "../time";

defineOptions({ name: "CompetitorsPage" });

const sampleUrls = [
  "https://www.takealot.com/laser-lipo-slimming-machine/PLID72189176",
  "https://www.takealot.com/multifunctional-led-modern-kitchen-sink-waterfall-push-button-te/PLID95526981",
  "https://www.takealot.com/adjustable-hinged-stabilizer-support-fitness-run-knee-brace/PLID96909926?size=Right",
  "https://www.takealot.com/cosmos-healing-enema-kit-medical-grade-silicone-2-litre/PLID94890093",
];

const rawUrls = ref(sampleUrls.join("\n"));
const withStockProbe = ref(true);
const visibleBrowser = ref(false);
const competitors = ref<CompetitorItem[]>([]);
const selectedPlid = ref("");
const detail = ref<CompetitorDetail>({ history: [], reviews: [], variants: [] });
const loading = ref(true);
const collecting = ref(false);
const completed = ref(0);
const total = ref(0);
const collectionResults = ref<CollectResult[]>([]);
const collectionErrors = ref<string[]>([]);
const pageError = ref("");
const reviewFilter = ref<"全部" | "好评" | "中评" | "差评">("全部");

const selected = computed(
  () => competitors.value.find((item) => item.plid === selectedPlid.value) ?? null,
);
const exactStockCount = computed(
  () => competitors.value.filter((item) => item.库存精确).length,
);
const averageRating = computed(() => {
  const ratings = competitors.value
    .map((item) => item.评分)
    .filter((value): value is number => value !== null);
  if (!ratings.length) return "—";
  return (ratings.reduce((sum, value) => sum + value, 0) / ratings.length).toFixed(2);
});
const latestCollection = computed(() => {
  if (!competitors.value.length) return "尚未采集";
  return formatChinaDateTime(competitors.value[0].采集时间);
});
const filteredReviews = computed(() => {
  if (reviewFilter.value === "全部") return detail.value.reviews;
  return detail.value.reviews.filter((review) => {
    if (reviewFilter.value === "好评") return review.星级 >= 4;
    if (reviewFilter.value === "中评") return review.星级 === 3;
    return review.星级 <= 2;
  });
});
const progress = computed(() =>
  total.value ? Math.round((completed.value / total.value) * 100) : 0,
);
const collectionNotices = computed(() =>
  collectionResults.value.filter((result) => result.message !== "采集成功"),
);

onMounted(loadOverview);

watch(selectedPlid, async (plid) => {
  if (!plid) {
    detail.value = { history: [], reviews: [], variants: [] };
    return;
  }
  detail.value = await fetchCompetitorDetail(plid);
});

async function loadOverview() {
  loading.value = true;
  pageError.value = "";
  try {
    competitors.value = await fetchCompetitors();
    if (!selectedPlid.value && competitors.value.length) {
      selectedPlid.value = competitors.value[0].plid;
    }
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : "读取竞品数据失败";
  } finally {
    loading.value = false;
  }
}

function parseUrls(): string[] {
  const unique = new Map<string, string>();
  for (const line of rawUrls.value.split(/\r?\n/)) {
    const url = line.trim();
    if (!url) continue;
    const match = url.match(/PLID(\d+)/i);
    if (!match) throw new Error(`链接中未找到 PLID：${url}`);
    if (!unique.has(match[1])) unique.set(match[1], url);
  }
  return [...unique.values()];
}

async function startCollection() {
  collectionResults.value = [];
  collectionErrors.value = [];
  completed.value = 0;
  try {
    const urls = parseUrls();
    if (!urls.length) throw new Error("请至少填写一个 Takealot 竞品链接");
    total.value = urls.length;
    collecting.value = true;
    for (const url of urls) {
      try {
        collectionResults.value.push(
          await collectCompetitor(url, withStockProbe.value, visibleBrowser.value),
        );
      } catch (error) {
        const plid = url.match(/PLID(\d+)/i)?.[1] ?? "未知商品";
        const message = error instanceof Error ? error.message : "采集失败";
        collectionErrors.value.push(`PLID${plid}：${message}`);
      } finally {
        completed.value += 1;
      }
    }
    await loadOverview();
  } catch (error) {
    collectionErrors.value = [
      error instanceof Error ? error.message : "无法开始采集",
    ];
  } finally {
    collecting.value = false;
  }
}

function formatCurrency(value: number | null) {
  return value === null
    ? "—"
    : new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value);
}

function reviewTone(stars: number) {
  if (stars >= 4) return "positive";
  if (stars === 3) return "neutral";
  return "negative";
}
</script>

<template>
  <div class="competitor-module">
    <header class="hero">
      <div>
        <p class="eyebrow">TAKEALOT MARKET INTELLIGENCE</p>
        <h1>竞品雷达</h1>
        <p class="hero-copy">
          把库存、评论与销量信号放在同一条时间线上，让运营先看到变化，再决定动作。
        </p>
      </div>
      <div class="status-chip">
        <span class="status-dot"></span>
        本机数据 · MySQL
      </div>
    </header>

    <section class="collector panel">
      <div class="section-heading">
        <div>
          <p class="section-kicker">建立与刷新观察样本</p>
          <h2>批量采集竞品</h2>
        </div>
        <p class="section-note">每行一个链接，重复 PLID 会自动去重</p>
      </div>
      <textarea
        v-model="rawUrls"
        aria-label="竞品链接"
        :disabled="collecting"
        spellcheck="false"
      ></textarea>
      <div class="collector-actions">
        <label class="switch-row">
          <input v-model="withStockProbe" type="checkbox" :disabled="collecting" />
          <span class="switch"></span>
          <span>
            <strong>匿名购物车库存探测</strong>
            <small>逐个测试所有变体的当前卖家与 SKU，不进入结算</small>
          </span>
        </label>
        <label class="switch-row compact">
          <input
            v-model="visibleBrowser"
            type="checkbox"
            :disabled="collecting || !withStockProbe"
          />
          <span class="switch"></span>
          <span><strong>显示检测浏览器</strong></span>
        </label>
        <button class="primary-button" :disabled="collecting" @click="startCollection">
          {{ collecting ? `正在采集 ${completed}/${total}` : "开始采集" }}
        </button>
      </div>
      <div v-if="collecting || completed" class="progress-track" aria-live="polite">
        <span :style="{ width: `${progress}%` }"></span>
      </div>
      <p v-if="collecting" class="method-note collection-persistence-note">
        采集正在后台继续；切换到其他页面后再返回，进度和结果仍会保留。
      </p>
      <div v-if="collectionResults.length || collectionErrors.length" class="result-strip">
        <span v-if="collectionResults.length" class="result-good">
          成功 {{ collectionResults.length }} 个
        </span>
        <span v-if="collectionErrors.length" class="result-bad">
          失败 {{ collectionErrors.length }} 个
        </span>
        <span v-for="notice in collectionNotices" :key="notice.plid">
          PLID{{ notice.plid }}：{{ notice.message }}
        </span>
        <span v-for="error in collectionErrors" :key="error">{{ error }}</span>
      </div>
    </section>

    <section class="metrics">
      <article>
        <span>已监控竞品</span>
        <strong>{{ competitors.length }}</strong>
        <small>当前活跃链接</small>
      </article>
      <article>
        <span>精确库存样本</span>
        <strong>{{ exactStockCount }}</strong>
        <small>当前最新快照</small>
      </article>
      <article>
        <span>平均评分</span>
        <strong>{{ averageRating }}</strong>
        <small>公开商品评分</small>
      </article>
      <article>
        <span>最近采集</span>
        <strong class="metric-date">{{ latestCollection }}</strong>
        <small>北京时间</small>
      </article>
    </section>

    <p v-if="pageError" class="error-banner">{{ pageError }}</p>
    <section class="panel overview">
      <div class="section-heading">
        <div>
          <p class="section-kicker">LATEST SNAPSHOT</p>
          <h2>竞品最新状态</h2>
        </div>
        <button class="quiet-button" @click="loadOverview">刷新页面数据</button>
      </div>
      <div v-if="loading" class="empty-state">正在读取本机数据……</div>
      <div v-else-if="!competitors.length" class="empty-state">
        <strong>还没有竞品快照</strong>
        <span>上方 4 个样本链接已经填好，点击“开始采集”即可建立第一条基线。</span>
      </div>
      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>竞品</th>
              <th>价格</th>
              <th>库存上限</th>
              <th>评论 / 评分</th>
              <th>累计销量估算</th>
              <th>观察期信号</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in competitors"
              :key="item.plid"
              :class="{ selected: selectedPlid === item.plid }"
              @click="selectedPlid = item.plid"
            >
              <td>
                <strong>{{ item.商品 }}</strong>
                <span>PLID{{ item.plid }} · {{ item.当前卖家 || "未知卖家" }}</span>
              </td>
              <td>{{ formatCurrency(item.价格) }}</td>
              <td>
                <span
                  class="stock-pill"
                  :class="{
                    exact: item.库存精确,
                    unavailable: item.库存上限 === '没货',
                  }"
                >
                  {{ item.库存上限 }}
                </span>
              </td>
              <td>{{ item.评论数 }} 条 · {{ item.评分 ?? "—" }}</td>
              <td><strong>{{ item.累计销量估算 }}</strong></td>
              <td>
                <span class="signal-label">{{ item.趋势判断 }}</span>
                <small>{{ item.观察期销量信号 }}</small>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="method-note">
        累计销量按 2%–5% 假设评论率反推，只表示商品维度量级；不是平台官方销量。
      </p>
    </section>

    <template v-if="selected">
      <section class="detail-grid">
        <article class="panel decision-card">
          <p class="section-kicker">OPERATING SIGNAL</p>
          <h2>{{ selected.趋势判断 }}</h2>
          <p>{{ selected.判断说明 }}</p>
          <div class="decision-stats">
            <span><small>库存上限</small><strong>{{ selected.库存上限 }}</strong></span>
            <span><small>累计评论</small><strong>{{ selected.评论数 }}</strong></span>
            <span><small>观察期估算</small><strong>{{ selected.观察期销量信号 }}</strong></span>
          </div>
          <a :href="selected.链接" target="_blank" rel="noreferrer">打开 Takealot 商品页</a>
        </article>

        <article class="panel review-balance">
          <p class="section-kicker">REVIEW BALANCE</p>
          <h2>评论结构</h2>
          <div class="balance-row positive">
            <span>好评 4–5 星</span><strong>{{ selected.好评 }}</strong>
            <i :style="{ width: `${(selected.好评 / Math.max(1, selected.评论数)) * 100}%` }"></i>
          </div>
          <div class="balance-row neutral">
            <span>中评 3 星</span><strong>{{ selected.中评 }}</strong>
            <i :style="{ width: `${(selected.中评 / Math.max(1, selected.评论数)) * 100}%` }"></i>
          </div>
          <div class="balance-row negative">
            <span>差评 1–2 星</span><strong>{{ selected.差评 }}</strong>
            <i :style="{ width: `${(selected.差评 / Math.max(1, selected.评论数)) * 100}%` }"></i>
          </div>
        </article>
      </section>

      <section class="panel variant-panel">
        <div class="section-heading">
          <div>
            <p class="section-kicker">VARIANT INVENTORY</p>
            <h2>各变体库存</h2>
          </div>
          <span>{{ detail.variants.length }} 个变体 · 评论共用商品数据</span>
        </div>
        <div v-if="!detail.variants.length" class="empty-state slim">
          这条历史快照尚无变体明细，重新采集后会逐个显示。
        </div>
        <div v-else class="table-wrap">
          <table class="variant-table">
            <thead>
              <tr>
                <th>变体</th>
                <th>平台 SKU</th>
                <th>卖家</th>
                <th>价格</th>
                <th>平台仓库存</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="variant in detail.variants" :key="variant.变体键">
                <td>
                  <a :href="variant.链接" target="_blank" rel="noreferrer">
                    {{ variant.变体 }}
                  </a>
                </td>
                <td>{{ variant.SKU || "—" }}</td>
                <td>{{ variant.卖家 || "未知卖家" }}</td>
                <td>{{ formatCurrency(variant.价格) }}</td>
                <td>
                  <span
                    class="stock-pill"
                    :class="{
                      exact: variant.库存精确,
                      unavailable: variant.库存 === '没货',
                    }"
                  >
                    {{ variant.库存 }}
                  </span>
                </td>
                <td>
                  <small>{{ variant.库存说明 || "—" }}</small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="method-note">
          库存按变体分别探测；供应商调货、长时效到货和当前不可购买的变体按平台仓没货处理。
          评论按 PLID 商品维度共用，不因变体重复采集或重复计数。
        </p>
      </section>

      <section class="panel history-panel">
        <div class="section-heading">
          <div>
            <p class="section-kicker">OBSERVATION HISTORY</p>
            <h2>历史快照</h2>
          </div>
          <span>{{ detail.history.length }} 个时间点</span>
        </div>
        <div v-if="detail.history.length < 2" class="empty-state slim">
          再次采集后，这里会显示库存净流出、新增评论和观察期销量信号。
        </div>
        <div v-else class="timeline">
          <article v-for="item in detail.history" :key="item.采集时间">
            <time>{{ formatChinaDateTime(item.采集时间) }}</time>
            <strong>{{ item.趋势判断 }}</strong>
            <span>库存 {{ item.库存上限 }} · 评论 {{ item.评论数 }}</span>
            <small>净流出 {{ item.库存净流出 ?? "—" }} · 新增评论 {{ item.新增评论 ?? "—" }}</small>
          </article>
        </div>
      </section>

      <section class="panel reviews-panel">
        <div class="section-heading">
          <div>
            <p class="section-kicker">VOICE OF CUSTOMER</p>
            <h2>公开评论</h2>
          </div>
          <div class="filter-tabs">
            <button
              v-for="filter in ['全部', '好评', '中评', '差评'] as const"
              :key="filter"
              :class="{ active: reviewFilter === filter }"
              @click="reviewFilter = filter"
            >
              {{ filter }}
            </button>
          </div>
        </div>
        <div v-if="!filteredReviews.length" class="empty-state slim">暂无对应评论。</div>
        <div v-else class="review-list">
          <article v-for="review in filteredReviews" :key="`${review.评论日期}-${review.标题}`">
            <span class="review-score" :class="reviewTone(review.星级)">
              {{ review.星级 }} 星
            </span>
            <div>
              <strong>{{ review.标题 || "未填写标题" }}</strong>
              <p>{{ review.评论内容 || "未填写评论内容" }}</p>
              <small>{{ review.评论人 || "匿名用户" }} · {{ review.评论日期 || "日期未知" }}</small>
            </div>
          </article>
        </div>
      </section>
    </template>

    <footer class="module-footer">
      库存是各变体在隔离匿名会话中的购物车可售上限；评论按商品共用。所有估算均需结合连续快照判断。
    </footer>
  </div>
</template>
