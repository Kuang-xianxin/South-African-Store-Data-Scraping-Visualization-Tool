<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { SearchRankingAnalysis, SearchRankingTitleStrategy, TitleBenchmark } from "../types";
import { keywordRankingContext, keywordRankingRows, primaryTitle, titleDiagnosis, titleWordChanges } from "../titleOptimization";
import { productThumbnailUrl } from "../productImages";

const props = defineProps<{
  analysis: SearchRankingAnalysis;
  currentTitle: string;
  currentOfferId?: string;
  currentPlid?: string | null;
  strategies: SearchRankingTitleStrategy[];
  canReview?: boolean;
  reviewing?: boolean;
}>();
const emit = defineEmits<{ review: [] }>();
const diagnosis = computed(() => titleDiagnosis(props.analysis, props.currentTitle));
const primary = computed(() => primaryTitle(props.strategies));
const proposedTitle = computed(() => diagnosis.value.keep ? props.currentTitle : primary.value?.title ?? "");
const changes = computed(() => titleWordChanges(props.currentTitle, proposedTitle.value));
const references = computed(() => props.analysis.title_benchmarks?.items ?? []);
const rankings = computed(() => keywordRankingRows(props.analysis.keywords));
const rankingContext = computed(() => keywordRankingContext(props.analysis, props.currentTitle, props.currentOfferId, props.currentPlid));
const showAllRankings = ref(false);
const showAll = ref(false);
const selected = ref<TitleBenchmark | null>(null);
const dialog = ref<HTMLDialogElement | null>(null);
const failedImages = ref(new Set<string>());
const copyMessage = ref("");
let returnFocus: HTMLElement | null = null;

watch(() => props.analysis, () => {
  closeReference();
  showAll.value = false;
  showAllRankings.value = false;
  copyMessage.value = "";
});
function imageUrl(item: TitleBenchmark) {
  return item.image_url && !failedImages.value.has(item.image_url)
    ? productThumbnailUrl(item.image_url, 192) : "";
}
function imageFailed(item: TitleBenchmark) {
  if (item.image_url) failedImages.value = new Set([...failedImages.value, item.image_url]);
}
async function openReference(item: TitleBenchmark, event: Event) {
  returnFocus = event.currentTarget as HTMLElement;
  selected.value = item;
  await nextTick();
  dialog.value?.showModal();
}
function closeReference() {
  dialog.value?.close();
  selected.value = null;
  returnFocus?.focus();
  returnFocus = null;
}
async function copyTitle(title: string) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(title);
    copyMessage.value = "已复制标题";
  } catch {
    const previousFocus = document.activeElement as HTMLElement | null;
    const textarea = document.createElement("textarea");
    textarea.value = title;
    textarea.style.cssText = "position:fixed;left:-10000px;top:0";
    document.body.append(textarea);
    textarea.select();
    try {
      copyMessage.value = document.execCommand("copy") ? "已复制标题" : "复制失败，请选中标题手动复制";
    } catch { copyMessage.value = "复制失败，请选中标题手动复制"; }
    finally { textarea.remove(); previousFocus?.focus(); }
  }
}
function observedTime(value: string | null) {
  if (!value) return "时间未记录";
  const date = new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? "时间未记录" : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
function monitoringLabel(item: TitleBenchmark) {
  return ({ available: "已有监测记录", not_monitored: "暂无可用监测记录 · 可参考标题", no_access: "无监测数据查看权限", unavailable: "监测数据读取失败", not_requested: "监测数据待读取" })[item.monitoring_status];
}
</script>

<template>
  <div class="title-workbench">
    <section class="diagnosis-panel" aria-labelledby="title-diagnosis-label">
      <div class="review-heading"><h3 id="title-diagnosis-label">当前标题</h3><strong class="diagnosis-badge">{{ diagnosis.label }}</strong></div>
      <p class="full-title">{{ currentTitle }}</p>
      <ul><li v-for="reason in diagnosis.reasons" :key="reason">{{ reason }}</li></ul>
      <div class="keyword-rankings" aria-labelledby="keyword-rankings-label">
        <div class="review-heading">
          <h4 id="keyword-rankings-label">各搜索词排名{{ rankingContext.sameLink ? '' : ' · 同组参考' }}</h4>
          <small>{{ rankings.length }} 个词</small>
        </div>
        <p v-if="rankingContext.note" class="ranking-context" role="note">{{ rankingContext.note }}</p>
        <p v-if="!rankings.length" class="review-empty">暂无搜索记录，分析商品后查看各词下的自然排名。</p>
        <template v-else>
          <table class="ranking-table">
            <caption class="ranking-caption">{{ rankingContext.sameLink ? '当前链接' : '同组参考商品' }}在不同搜索词下的自然排名</caption>
            <thead><tr><th scope="col">搜索词</th><th scope="col">自然排名</th><th scope="col">采集时间<span>北京时间</span></th></tr></thead>
            <tbody>
              <tr v-for="row in (showAllRankings ? rankings : rankings.slice(0, 5))" :key="row.item.id">
                <td><a :href="row.item.search_url" target="_blank" rel="noopener noreferrer">{{ row.item.keyword }}</a><small>{{ row.relation }}</small></td>
                <td><strong :class="{ 'rank-located': row.located }">{{ row.rank }}</strong><small>{{ row.scope }}</small></td>
                <td><time v-if="row.observedAt" :datetime="row.observedAt">{{ observedTime(row.observedAt) }}</time><span v-else>—</span></td>
              </tr>
            </tbody>
          </table>
          <button v-if="rankings.length > 5" type="button" class="more-rankings" :aria-expanded="showAllRankings" @click="showAllRankings = !showAllRankings">{{ showAllRankings ? '收起搜索词' : `查看全部 ${rankings.length} 个搜索词` }}</button>
          <p class="evidence-note">自然排名不含广告，按采集时结果展示。排名高低与标题质量分别判断。</p>
        </template>
      </div>
    </section>

    <section class="recommendation-panel" aria-labelledby="recommended-title-label">
      <div class="review-heading">
        <h3 id="recommended-title-label">{{ diagnosis.keep ? "保留现有标题" : "首选标题" }}</h3>
        <button v-if="proposedTitle && diagnosis.usable" type="button" @click="copyTitle(proposedTitle)">复制标题</button>
      </div>
      <p v-if="!diagnosis.usable" class="review-empty">{{ diagnosis.reasons[0] }} 可展开分析依据核实。</p>
      <template v-else-if="proposedTitle">
        <p class="full-title recommended-title">{{ proposedTitle }}</p>
        <p v-if="!diagnosis.keep" class="short-reason">{{ changes.reordered ? "调整品名与卖点顺序；商品类型优先，规格默认后置。" : "依据已验证的商品表达整理标题。" }}</p>
        <p v-if="!diagnosis.keep && primary?.evidence_keywords.length" class="short-reason">参考搜索表达：{{ primary.evidence_keywords.slice(0, 2).join(" / ") }}</p>
        <details v-if="!diagnosis.keep" class="review-disclosure">
          <summary>查看修改与备选</summary>
          <p>新增：<mark>{{ changes.added.join(" ") || "无" }}</mark></p>
          <p>移除：<del>{{ changes.removed.join(" ") || "无" }}</del></p>
          <p>词序：{{ changes.reordered ? "已调整，请对照原题与建议标题" : "未调整" }}</p>
          <article v-for="strategy in strategies.filter((item) => item !== primary)" :key="strategy.strategy" class="alternative-title">
            <b>{{ strategy.label }}</b>
            <p class="full-title">{{ strategy.available ? strategy.title : "本轮暂无可用备选" }}</p>
            <button v-if="strategy.available && strategy.title" type="button" @click="copyTitle(strategy.title)">复制备选</button>
            <p>{{ strategy.explanation }}</p>
          </article>
        </details>
      </template>
      <p v-else class="review-empty">本轮没有形成可用标题，请展开分析依据核实商品资料。</p>
      <p v-if="copyMessage" role="status">{{ copyMessage }}</p>
    </section>

    <section class="reference-panel" aria-labelledby="title-references-label">
      <div class="review-heading"><h3 id="title-references-label">竞品标题参考</h3><span>{{ references.length }} 个独立商品</span></div>
      <div v-if="references.length && analysis.title_benchmarks?.review_status !== 'complete'" class="review-empty" role="status">
        <p>{{ analysis.title_benchmarks?.review_status === 'failed' ? '竞品标题分析未完成，可重试。' : '已有完整竞品标题，尚未分析其品名、卖点和表达方式。' }}</p>
        <button v-if="canReview" type="button" :disabled="reviewing" @click="emit('review')">{{ reviewing ? '正在分析竞品标题…' : '分析竞品标题' }}</button>
        <details v-if="analysis.title_benchmarks?.review_error"><summary>查看原因</summary>{{ analysis.title_benchmarks.review_error }}</details>
      </div>
      <p v-if="!references.length" class="review-empty">{{ analysis.title_benchmarks?.review_status === 'complete' ? '本轮候选与我们商品的可比性不足，未作为标题参考。' : '当前记录没有可比竞品的完整标题证据。重新分析后补充；不会用不相关商品凑数。' }}</p>
      <div class="reference-list">
        <article v-for="item in (showAll ? references : references.slice(0, 3))" :key="item.plid" class="reference-card">
          <img v-if="imageUrl(item)" :src="imageUrl(item)" :alt="item.title" width="72" height="72" loading="lazy" @error="imageFailed(item)" />
          <span v-else class="reference-image-empty">暂无图片</span>
          <div class="reference-copy">
            <span class="reference-relation">{{ item.relation === 'direct_same_product' ? '同类商品' : '同需求商品' }}</span>
            <a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer" class="full-title">{{ item.title }}</a>
            <p v-else class="full-title">{{ item.title }}</p>
            <p v-if="item.title_analysis_status === 'complete'">{{ item.title_assessment }}</p>
            <button type="button" @click="openReference(item, $event)">查看对比依据</button>
          </div>
        </article>
      </div>
      <button v-if="references.length > 3" type="button" class="more-references" @click="showAll = !showAll">{{ showAll ? "收起" : `查看全部 ${references.length} 个竞品` }}</button>
    </section>

    <dialog v-if="selected" ref="dialog" class="reference-dialog" aria-labelledby="reference-dialog-title" @cancel.prevent="closeReference" @keydown.esc.stop @click="(event) => { if (event.target === dialog) closeReference(); }">
      <header class="review-heading"><h3 id="reference-dialog-title">竞品对比依据</h3><button type="button" autofocus @click="closeReference">关闭</button></header>
      <a v-if="selected.url" :href="selected.url" target="_blank" rel="noopener noreferrer" class="full-title">{{ selected.title }}</a>
      <p v-else class="full-title">{{ selected.title }}</p>
      <p v-if="!selected.url" class="evidence-note">现有记录缺少完整商品链接，可按标题在平台搜索。</p>
      <p>{{ selected.relation === 'direct_same_product' ? '同类商品' : '同需求替代商品' }} · PLID{{ selected.plid }}</p>
      <p v-if="selected.comparison_reason" class="evidence-note">{{ selected.comparison_reason }}</p>
      <section><h4>标题表达与可借鉴部分</h4>
        <p v-if="selected.title_analysis_status !== 'complete'" class="review-empty">尚未完成竞品标题分析。</p>
        <p v-else>{{ selected.title_assessment }}</p>
        <dl class="reference-facts">
          <dt>平台类目</dt><dd>{{ selected.category_path.map((item) => item.name).join(' / ') || '现有搜索记录未包含平台类目' }}</dd>
          <template v-if="selected.title_analysis_status === 'complete'">
            <dt>商品品名</dt><dd>{{ selected.core_phrases.join(" / ") }}</dd>
            <dt>标题中的卖点</dt><dd>{{ selected.detail_phrases.join(" / ") || "标题未明确写出独立卖点" }}</dd>
            <dt>标题中的规格</dt><dd>{{ selected.specifications.join(" / ") || "标题未写明尺寸或数量等规格" }}</dd>
            <dt>我们可借鉴</dt><dd>{{ selected.borrowable_phrases.length ? `当前主标题也有相应表达，可对照写法：${selected.borrowable_phrases.join(" / ")}` : "可参考上方表达顺序；具体属性需结合我们商品核实" }}</dd>
            <template v-if="selected.requires_confirmation.length"><dt>采用前需核实</dt><dd>{{ selected.requires_confirmation.join(" / ") }}</dd></template>
          </template>
        </dl>
        <p class="evidence-note">竞品写有某项功能，不代表我们的商品也具备。</p>
      </section>
      <section><h4>搜索表现</h4>
        <article v-for="row in selected.search_evidence" :key="`${row.keyword}-${row.captured_at}`" class="query-observation">
          <b>{{ row.keyword }}</b>
          <p>竞品自然位 #{{ row.organic_position }} · 我们 {{ row.target_organic_position ? `#${row.target_organic_position}` : '本次扫描未定位' }}</p>
          <small>{{ observedTime(row.captured_at) }} · 北京时间</small>
          <p v-if="row.title !== selected.title">当时标题：{{ row.title }}</p>
        </article>
        <p class="evidence-note">位置是入选参考，不能证明标题更好或标题导致排名靠前。</p>
      </section>
      <section><h4>已有经营观察</h4><p>{{ monitoringLabel(selected) }}</p>
        <dl v-if="selected.monitored_evidence" class="reference-facts">
          <dt>监测价格</dt><dd>{{ selected.monitored_evidence.price == null ? '数据不足' : `R ${selected.monitored_evidence.price}` }}</dd>
          <dt>价格采集时间</dt><dd>{{ observedTime(selected.monitored_evidence.captured_at) }} · 北京时间</dd>
          <dt>近30天库存观察售出</dt><dd>{{ selected.monitored_evidence.observed_sales_30 ?? '数据不足' }}</dd>
          <dt>观察截止日期</dt><dd>{{ selected.monitored_evidence.observed_sales_through || '暂无' }}</dd>
        </dl>
        <p v-if="selected.monitored_evidence" class="evidence-note">库存下降观察，非官方订单；与上方搜索记录可能不是同一时间。</p>
        <p v-if="selected.monitored_evidence?.title && selected.monitored_evidence.title !== selected.title">监测时标题：{{ selected.monitored_evidence.title }}</p>
      </section>
    </dialog>
  </div>
</template>

<style scoped>
.keyword-rankings{margin-top:22px;padding-top:18px;border-top:1px solid #dfe8e4}.keyword-rankings .review-heading{margin-bottom:6px}.keyword-rankings h4{margin:0;font-size:15px}.keyword-rankings .review-heading small{color:#6b7e77}.ranking-context{padding:10px 12px;border-radius:8px;background:#fff7e7;font-size:13px;line-height:1.6}.ranking-table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:13px;text-align:left}.ranking-caption{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap}.ranking-table th{font-weight:500;color:#6b7e77;padding:10px 8px;border-bottom:1px solid #dfe8e4}.ranking-table td{padding:12px 8px;border-bottom:1px solid #edf1f0;vertical-align:top;overflow-wrap:anywhere;line-height:1.5}.ranking-table th:first-child,.ranking-table td:first-child{width:43%;padding-left:0}.ranking-table th:nth-child(2){width:29%}.ranking-table th:last-child,.ranking-table td:last-child{padding-right:0}.ranking-table a{color:#28584b;font-weight:600}.ranking-table small,.ranking-table th span{display:block;font-size:11px;color:#6b7e77;margin-top:4px;font-weight:400}.ranking-table strong{font-size:13px;font-weight:500}.ranking-table .rank-located{font-size:20px;font-weight:700;color:#216550}.ranking-table time{font-size:12px;color:#596e65}.more-rankings{margin-top:14px}.keyword-rankings .evidence-note{margin-bottom:0}@media(max-width:640px){.ranking-table th:first-child,.ranking-table td:first-child{width:38%}.ranking-table th:nth-child(2){width:31%}.ranking-table td,.ranking-table th{padding-left:6px;padding-right:6px}.ranking-table .rank-located{font-size:18px}.ranking-table a,.ranking-table strong{font-size:12px}.ranking-table time{font-size:11px}}
.title-workbench{display:grid;gap:18px;color:#243b43}.title-workbench section{min-width:0}.diagnosis-panel,.recommendation-panel,.reference-panel{padding:22px;border:1px solid #dbe5e5;border-radius:16px;background:#fff}.recommendation-panel{background:#f3faf7;border-color:#b9d8c9}.review-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}.review-heading h3{margin:0;font-size:17px}.diagnosis-badge{border-radius:18px;padding:6px 12px;background:#edf3f2;font-size:13px}.full-title{display:block;white-space:normal;overflow-wrap:anywhere;line-height:1.65;font-size:16px;color:inherit}.recommended-title{font-size:20px;font-weight:650}.title-workbench button{border:1px solid #b4cac3;border-radius:8px;background:#fff;color:#28584b;padding:8px 13px;cursor:pointer}.title-workbench button:focus-visible,.title-workbench summary:focus-visible{outline:3px solid #207d68;outline-offset:3px}.title-workbench li,.short-reason,.reference-copy p{font-size:13px;line-height:1.6}.title-workbench ul{padding-left:20px;margin-bottom:0}.review-disclosure{margin-top:16px;border-top:1px solid #d9e6e0;padding-top:12px}.review-disclosure summary{cursor:pointer;font-size:13px}.alternative-title{border-top:1px solid #dbe5e5;margin-top:16px;padding-top:14px}.reference-list{display:grid;gap:0}.reference-card{display:grid;grid-template-columns:72px minmax(0,1fr);gap:16px;padding:18px 0;border-top:1px solid #edf1f0}.reference-card:first-child{border-top:0;padding-top:0}.reference-card img{object-fit:contain;border-radius:9px}.reference-image-empty{display:grid;place-items:center;width:72px;height:72px;background:#f1f4f3;color:#71857d;font-size:11px}.reference-relation{font-size:11px;color:#657d73}.reference-copy p{margin:6px 0 9px}.more-references{margin-top:10px}.review-empty,.evidence-note{font-size:13px;color:#677a73;line-height:1.65}.reference-dialog{border:0;border-radius:18px;padding:26px;width:min(740px,calc(100vw - 40px));max-height:85vh;overflow:auto;box-sizing:border-box;color:#243b43}.reference-dialog::backdrop{background:#102c35aa}.reference-dialog section{border-top:1px solid #dfe8e4;margin-top:20px;padding-top:6px}.reference-dialog button{border:1px solid #c2d3cc;border-radius:8px;padding:8px 14px;background:white;cursor:pointer}.reference-dialog .full-title{font-weight:600}.reference-facts{display:grid;grid-template-columns:140px minmax(0,1fr);gap:12px;font-size:14px;line-height:1.5}.reference-facts dt{color:#6b7e77}.reference-facts dd{margin:0;overflow-wrap:anywhere}.query-observation{padding:12px;background:#f4f7f5;border-radius:10px;margin:8px 0}.query-observation p{margin:7px 0;font-size:14px}.query-observation small{color:#6b7e77}mark{background:#d9f1df}del{color:#9b5347}@media(max-width:640px){.diagnosis-panel,.recommendation-panel,.reference-panel{padding:16px}.review-heading{align-items:flex-start;flex-wrap:wrap}.recommended-title{font-size:17px}.reference-card{grid-template-columns:56px minmax(0,1fr);gap:12px}.reference-card img,.reference-image-empty{width:56px;height:56px}.reference-dialog{padding:18px;width:calc(100vw - 24px)}.reference-facts{grid-template-columns:1fr;gap:5px}.reference-facts dd{margin-bottom:10px}}
</style>
