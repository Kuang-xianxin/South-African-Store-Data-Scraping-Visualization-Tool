<script setup lang="ts">
import { computed, nextTick, onUnmounted, reactive, ref, watch } from "vue";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import { formatChinaDateTime } from "../time";
import {
  defaultPickerFilters, filterPickerFamilies, pickerQueries, pickerScore,
  pickerStatus, pickerTarget, pickerViewMatches, pickerFilterError, type PickerView, type PickerSort, type PickerFilters,
} from "../titleProductPicker";
import type { SearchRankingProductFamily } from "../searchRankingFamilies";
import type { SearchRankingProduct } from "../types";

const props = defineProps<{
  families: SearchRankingProductFamily[];
  selectedOfferId: string;
  selectedStoreCode: string;
  loading?: boolean;
  disabled?: boolean;
  active?: boolean;
}>();
const emit = defineEmits<{ select: [product: SearchRankingProduct] }>();
const filters = reactive(defaultPickerFilters());
const dialog = ref<HTMLDialogElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const resultsElement = ref<HTMLElement | null>(null);
const isOpen = ref(false);
const advancedOpen = ref(false);
const batchSearchOpen = ref(false);
const page = ref(1);
const pageSize = ref(20);
const failedImages = ref(new Set<string>());
const expandedFamily = ref("");
const queries = computed(() => pickerQueries(filters.search));
const filterError = computed(() => pickerFilterError(filters));
const filtered = computed(() => filterPickerFamilies(props.families, filters));
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const selected = computed(() => props.families.find((family) => family.variants.some((item) =>
  item.offer_id === props.selectedOfferId && String(item.store_code ?? "") === props.selectedStoreCode,
)) ?? null);
const selectedPosition = computed(() => filtered.value.findIndex((family) => family.key === selected.value?.key));
const selectedProduct = computed(() => selected.value?.variants.find((item) => item.offer_id === props.selectedOfferId) ?? null);
const stores = computed(() => [...new Map(props.families.map((family) => {
  const product = family.representative;
  return [String(product.store_code ?? ""), product.store_name || product.store_code || "当前店铺"];
})).entries()].sort((left, right) => left[1].localeCompare(right[1], "zh-CN")));
const views: Array<{ key: PickerView; label: string }> = [
  { key: "all", label: "全部商品" }, { key: "unanalysed", label: "未分析" },
  { key: "low_score", label: "低于70分" }, { key: "manual", label: "待核实" },
  { key: "failed", label: "上次失败" }, { key: "completed", label: "已分析" },
];
const viewCounts = computed(() => {
  const scope = filterPickerFamilies(props.families, { ...filters, view: "all" });
  return Object.fromEntries(views.map((view) => [view.key, scope.filter((family) => pickerViewMatches(family, view.key)).length]));
});
const filterLabels: Record<string, Record<string, string>> = {
  identity: { high: "图题差异大", moderate: "图题中等差异", aligned: "图题一致" },
  score: { "85_plus": "评分85–100", "70_84": "评分70–84", "55_69": "评分55–69", below_55: "评分低于55", unscored: "无有效评分", insufficient: "证据不足", stale: "标题已变化" },
  variants: { multiple: "多变体", single: "单变体" }, sku: { missing: "有SKU未关联", linked: "全部SKU已关联" },
  image: { missing: "有变体缺图", complete: "全部变体有图" },
  searchField: { title: "仅搜商品名称", company_sku: "仅搜公司SKU", sku: "仅搜平台SKU", offer_id: "仅搜Offer", productline_id: "仅搜PLID" },
  searchMatch: { exact: "精确匹配" },
};
const activeChips = computed(() => {
  const chips: Array<{ key: string; fields: (keyof PickerFilters)[]; label: string }> = [];
  if (filters.store) chips.push({ key: "store", fields: ["store"], label: stores.value.find(([code]) => code === filters.store)?.[1] || filters.store });
  const defaults = defaultPickerFilters();
  for (const key of ["identity", "score", "variants", "sku", "image", "searchField", "searchMatch"] as const) {
    if (filters[key] !== defaults[key]) chips.push({ key, fields: [key], label: filterLabels[key][filters[key]] || filters[key] });
  }
  if (filters.exclude.trim()) chips.push({ key: "exclude", fields: ["exclude"], label: `排除：${filters.exclude}` });
  if (filters.stockMin || filters.stockMax) chips.push({ key: "stock", fields: ["stockMin", "stockMax"], label: `库存 ${filters.stockMin || "0"}–${filters.stockMax || "不限"}` });
  if (filters.scoreMin || filters.scoreMax) chips.push({ key: "scoreRange", fields: ["scoreMin", "scoreMax"], label: `评分 ${filters.scoreMin || "0"}–${filters.scoreMax || "100"}` });
  return chips;
});
const hasFilters = computed(() => JSON.stringify(filters) !== JSON.stringify(defaultPickerFilters()));
const stockPresets = [
  { value: "all", label: "全部可售库存", min: "", max: "" },
  { value: "1_5", label: "1–5件", min: "1", max: "5" },
  { value: "6_20", label: "6–20件", min: "6", max: "20" },
  { value: "21_50", label: "21–50件", min: "21", max: "50" },
  { value: "51_plus", label: "51件及以上", min: "51", max: "" },
];
const stockPreset = computed({
  get: () => stockPresets.find((item) => item.min === filters.stockMin && item.max === filters.stockMax)?.value ?? "custom",
  set: (value: string) => {
    if (value === "custom") { advancedOpen.value = true; return; }
    const preset = stockPresets.find((item) => item.value === value)!;
    filters.stockMin = preset.min; filters.stockMax = preset.max;
  },
});
const sortOptions: Array<{ key: string; label: string; asc: PickerSort; desc: PickerSort }> = [
  { key: "priority", label: "问题优先", asc: "priority", desc: "priority" },
  { key: "title", label: "商品名称", asc: "title", desc: "title_desc" },
  { key: "score", label: "标题评分", asc: "score_asc", desc: "score_desc" },
  { key: "stock", label: "可售库存", asc: "stock_asc", desc: "stock_desc" },
  { key: "variants", label: "变体数量", asc: "variants_asc", desc: "variants_desc" },
  { key: "analysis", label: "分析时间", asc: "oldest", desc: "newest" },
  { key: "captured", label: "商品快照时间", asc: "captured_asc", desc: "captured_desc" },
];
const sortDirection = computed({
  get: () => filters.sort !== "priority" && sortOptions.some((option) => option.asc === filters.sort) ? "asc" : "desc",
  set: (value: "asc" | "desc") => { filters.sort = sortOptions.find((option) => option.asc === filters.sort || option.desc === filters.sort)![value]; },
});
const sortMetric = computed({
  get: () => sortOptions.find((option) => option.asc === filters.sort || option.desc === filters.sort)!.key,
  set: (value: string) => { filters.sort = sortOptions.find((option) => option.key === value)![sortDirection.value]; },
});
function clearChip(fields: (keyof PickerFilters)[]) {
  const defaults = defaultPickerFilters();
  Object.assign(filters, Object.fromEntries(fields.map((key) => [key, defaults[key]])));
}
function storeCount(code: string) { return props.families.filter((family) => String(family.representative.store_code ?? "") === code).length; }
function targetFor(family: SearchRankingProductFamily) { return pickerTarget(family, queries.value, filters); }

watch([() => JSON.stringify(filters), pageSize], () => { page.value = 1; expandedFamily.value = ""; });
watch(pageCount, (count) => { page.value = Math.min(page.value, count); });
watch(page, () => { expandedFamily.value = ""; resultsElement.value?.scrollTo({ top: 0 }); });
watch(() => props.active, (active) => { if (active === false) closePicker(); });
watch(stores, (values) => {
  if (filters.store && !values.some(([code]) => code === filters.store)) filters.store = "";
});
onUnmounted(() => { dialog.value?.close(); });

async function openPicker() {
  isOpen.value = true;
  await nextTick();
  if (!isOpen.value || !dialog.value?.isConnected) return;
  if (!dialog.value.open) dialog.value.showModal();
  searchInput.value?.focus();
}
function closePicker() {
  dialog.value?.close();
  isOpen.value = false;
}
function onDialogClose() {
  if (!dialog.value?.open) isOpen.value = false;
}
function choose(family: SearchRankingProductFamily) {
  chooseVariant(targetFor(family));
}
function chooseVariant(product: SearchRankingProduct) {
  if (props.disabled) return;
  closePicker();
  emit("select", product);
}
function moveSelection(direction: number) {
  if (props.disabled || selectedPosition.value < 0) return;
  const family = filtered.value[selectedPosition.value + direction];
  if (family) emit("select", targetFor(family));
}
async function locateSelected() {
  if (!selected.value) return;
  if (selectedPosition.value < 0) Object.assign(filters, defaultPickerFilters());
  await nextTick();
  page.value = Math.floor(selectedPosition.value / pageSize.value) + 1;
  await nextTick();
  resultsElement.value?.querySelector<HTMLElement>('[aria-current="true"]')?.focus({ preventScroll: true });
  resultsElement.value?.querySelector<HTMLElement>('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
}
function imageUrl(product: SearchRankingProduct) {
  if (!product.image_url || failedImages.value.has(product.image_url)) return "";
  return productThumbnailUrl(product.image_url, PRODUCT_IMAGE_SIZE.list);
}
function imageFailed(url: string | null) {
  if (url) failedImages.value = new Set([...failedImages.value, url]);
}
function skuLabel(family: SearchRankingProductFamily) {
  return [...new Set(family.variants.map((item) => item.company_sku).filter(Boolean))].join(" · ") || "公司SKU未关联";
}
function scoreLabel(family: SearchRankingProductFamily) {
  const score = pickerScore(family);
  if (score !== null) return `${score}分`;
  if (family.latest_analysis?.title_score_current_title_match === false) return "历史评分";
  if (family.latest_analysis?.title_score_band === "insufficient_evidence") return "证据不足";
  return "未评分";
}
function handleListKey(event: KeyboardEvent) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const buttons = [...(resultsElement.value?.querySelectorAll<HTMLButtonElement>("button.picker-row, button.picker-variant") ?? [])];
  const current = buttons.indexOf(event.target as HTMLButtonElement);
  if (current < 0) return;
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : Math.max(0, Math.min(buttons.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
  event.preventDefault();
  buttons[index]?.focus();
}
</script>

<template>
  <section class="picker-bar" aria-label="当前商品与选择器">
    <button class="picker-trigger" aria-haspopup="dialog" :disabled="loading || disabled" @click="openPicker">
      <span class="picker-bar-icon" aria-hidden="true">▦</span>
      <span class="picker-bar-copy">
        <small>商品工作台 · {{ families.length }} 个商品族</small>
        <strong>{{ loading ? "正在读取商品…" : selectedProduct?.company_product_name || selected?.shared_title || "选择要优化的商品" }}</strong>
        <span v-if="selectedProduct">{{ selectedProduct.store_name || selectedProduct.store_code }} · PLID{{ selectedProduct.productline_id || "—" }} · {{ selectedProduct.company_sku || selectedProduct.sku || selectedProduct.offer_id }}</span>
      </span>
      <span class="picker-open-label">选择商品 <span aria-hidden="true">↗</span></span>
    </button>
    <div class="picker-stepper" aria-label="按当前筛选顺序切换商品">
      <span>{{ selectedPosition >= 0 ? `${selectedPosition + 1} / ${filtered.length}` : "当前商品在筛选外" }}</span>
      <button aria-label="上一个商品" title="按当前筛选顺序查看上一个" :disabled="disabled || loading || selectedPosition <= 0" @click="moveSelection(-1)">←</button>
      <button aria-label="下一个商品" title="按当前筛选顺序查看下一个" :disabled="disabled || loading || selectedPosition < 0 || selectedPosition >= filtered.length - 1" @click="moveSelection(1)">→</button>
    </div>
  </section>

  <Teleport to="body">
    <dialog ref="dialog" class="title-picker-dialog" aria-labelledby="title-picker-heading" @close="onDialogClose" @keydown.esc.stop.prevent="closePicker" @click="($event.target === dialog) && closePicker()">
      <div v-if="isOpen" class="picker-shell">
        <header class="picker-header">
          <div><p>标题优化 / 商品工作台</p><h2 id="title-picker-heading">选择商品<span>{{ families.length }} 个商品族</span></h2></div>
          <button class="picker-close" aria-label="关闭商品选择器" @click="closePicker">×</button>
        </header>
        <div class="picker-controls">
          <div class="picker-search-line">
            <div class="picker-search-box">
              <span aria-hidden="true">⌕</span>
              <input ref="searchInput" v-model="filters.search" aria-label="搜索商品" type="search" placeholder="搜索名称、SKU、Offer、PLID，或粘贴商品链接" @keydown.enter.stop.prevent="!$event.isComposing && filtered.length === 1 && choose(filtered[0]!)" />
            </div>
            <button :aria-expanded="batchSearchOpen" @click="batchSearchOpen = !batchSearchOpen">批量查找</button>
            <button :aria-expanded="advancedOpen" @click="advancedOpen = !advancedOpen">更多筛选<span v-if="activeChips.length"> · {{ activeChips.length }}</span></button>
          </div>
          <div v-if="batchSearchOpen" class="picker-batch-search">
            <label for="picker-batch-input">粘贴多个SKU、PLID或商品链接，每行一个；匹配任意一项</label>
            <textarea id="picker-batch-input" v-model="filters.search" rows="3" placeholder="支持换行、逗号或分号分隔" />
            <span>已识别 {{ queries.length }} 个搜索条件 · 只查找已有商品</span>
          </div>
          <div class="picker-views" role="group" aria-label="商品快捷筛选">
            <button v-for="view in views" :key="view.key" :aria-pressed="filters.view === view.key" @click="filters.view = view.key">{{ view.label }}<span>{{ viewCounts[view.key] }}</span></button>
          </div>
          <div class="picker-filters picker-primary-filters" role="group" aria-label="商品筛选与排序">
            <label>店铺<select v-model="filters.store"><option value="">当前范围全部店铺 · {{ families.length }}</option><option v-for="[code, name] in stores" :key="code" :value="code">{{ name }} · {{ storeCount(code) }}</option></select></label>
            <label>可售库存<select v-model="stockPreset"><option v-for="preset in stockPresets" :key="preset.value" :value="preset.value">{{ preset.label }}</option><option value="custom">自定义库存区间</option></select></label>
            <label>标题评分<select v-model="filters.score"><option value="all">全部评分</option><option v-for="(label, key) in filterLabels.score" :key="key" :value="key">{{ label }}</option></select></label>
            <label>排序指标<select v-model="sortMetric" aria-label="商品排序指标"><option v-for="option in sortOptions" :key="option.key" :value="option.key">{{ option.label }}</option></select></label>
            <label>排序方向<select v-model="sortDirection" aria-label="商品排序方向" :disabled="sortMetric === 'priority'"><option value="desc">降序 ↓</option><option value="asc">升序 ↑</option></select></label>
            <label>每页显示<select v-model.number="pageSize" aria-label="每页商品数"><option :value="10">10条</option><option :value="20">20条</option><option :value="50">50条</option></select></label>
          </div>
          <div v-if="advancedOpen" class="picker-filters picker-advanced-filters" role="group" aria-label="更多商品筛选">
            <label>搜索范围<select v-model="filters.searchField"><option value="all">全部字段</option><option value="title">商品名称 / 中文品名</option><option value="company_sku">公司SKU</option><option value="sku">平台SKU</option><option value="offer_id">Offer ID</option><option value="productline_id">PLID</option></select></label>
            <label>匹配方式<select v-model="filters.searchMatch"><option value="contains">模糊匹配</option><option value="exact">精确匹配</option></select></label>
            <label>交叉验证<select v-model="filters.identity"><option value="all">全部差异</option><option value="high">差异大</option><option value="moderate">中等差异</option><option value="aligned">一致</option></select></label>
            <label>变体<select v-model="filters.variants"><option value="all">全部变体</option><option value="multiple">多变体</option><option value="single">单变体</option></select></label>
            <label>公司SKU<select v-model="filters.sku"><option value="all">全部关联状态</option><option value="missing">有SKU未关联</option><option value="linked">全部SKU已关联</option></select></label>
            <label>商品图片<select v-model="filters.image"><option value="all">全部图片状态</option><option value="missing">有变体缺图</option><option value="complete">全部变体有图</option></select></label>
            <fieldset class="picker-range"><legend>可售库存区间（本店全变体）</legend><div><input v-model="filters.stockMin" inputmode="numeric" aria-label="可售库存下限" placeholder="最低" /><span>–</span><input v-model="filters.stockMax" inputmode="numeric" aria-label="可售库存上限" placeholder="最高" /></div></fieldset>
            <fieldset class="picker-range"><legend>有效标题评分区间</legend><div><input v-model="filters.scoreMin" inputmode="decimal" aria-label="标题评分下限" placeholder="0" /><span>–</span><input v-model="filters.scoreMax" inputmode="decimal" aria-label="标题评分上限" placeholder="100" /></div></fieldset>
            <label class="picker-exclude">排除词<input v-model="filters.exclude" type="text" placeholder="多个词用逗号分隔" /><small>任一变体命中则隐藏该商品族</small></label>
          </div>
          <div v-if="activeChips.length" class="picker-chips"><button v-for="chip in activeChips" :key="chip.key" :aria-label="`移除筛选：${chip.label}`" @click="clearChip(chip.fields)">{{ chip.label }} ×</button></div>
          <p v-if="filterError" class="picker-filter-error" role="alert">{{ filterError }}</p>
          <div class="picker-results-toolbar">
            <span role="status">显示 <strong>{{ filtered.length }}</strong> / {{ families.length }} 个商品族 · {{ filtered.reduce((count, family) => count + family.variant_count, 0) }} 个Offer</span>
            <div>
              <button v-if="hasFilters" class="picker-text-button" @click="Object.assign(filters, defaultPickerFilters())">清空筛选</button>
              <button v-if="selected" class="picker-text-button" @click="locateSelected">定位当前</button>
            </div>
          </div>
        </div>
        <div class="picker-columns" aria-hidden="true"><span>商品 / 店铺 / 公司SKU</span><span>标题评分</span><span>可售库存</span><span>分析状态 / 时间</span><span></span></div>
        <div ref="resultsElement" class="picker-results" @keydown="handleListKey">
          <div v-if="loading" class="picker-empty" role="status">正在读取商品…</div>
          <div v-else-if="!families.length" class="picker-empty"><strong>当前范围暂无可分析商品</strong><span>仅显示自有、在售、可售库存大于0且快照新鲜的商品。</span></div>
          <div v-else-if="!filtered.length" class="picker-empty"><strong>{{ filterError ? '请调整筛选区间' : '没有匹配当前搜索和筛选的商品' }}</strong><span>{{ filterError || '试试更短的名称、其他SKU，或清空筛选。' }}</span><button @click="Object.assign(filters, defaultPickerFilters())">清空筛选，查看全部</button></div>
          <article v-for="family in pageItems" v-else :key="family.key" class="picker-family">
          <button class="picker-row" :aria-current="selected?.key === family.key ? 'true' : undefined" :disabled="disabled" @click="choose(family)">
            <span class="picker-product">
              <img v-if="imageUrl(targetFor(family))" :src="imageUrl(targetFor(family))" alt="" loading="lazy" width="58" height="58" @error="imageFailed(targetFor(family).image_url)" />
              <span v-else class="picker-image-fallback">暂无图片</span>
              <span class="picker-product-copy"><strong>{{ family.shared_title || family.representative.title || "未命名商品" }}</strong><span v-if="family.representative.company_product_name" class="picker-company-name">{{ family.representative.company_product_name }}</span><small>{{ family.representative.store_name || family.representative.store_code || "当前店铺" }} · PLID{{ family.productline_id || "—" }} · {{ family.variant_count }} 个变体</small><small>{{ skuLabel(family) }}</small><small v-if="queries.length && targetFor(family).offer_id !== family.representative.offer_id" class="picker-match">匹配变体：{{ targetFor(family).company_sku || targetFor(family).sku || targetFor(family).offer_id }}</small></span>
            </span>
            <span class="picker-score" :class="{ low: pickerScore(family) !== null && pickerScore(family)! < 70, muted: pickerScore(family) === null }">{{ scoreLabel(family) }}</span>
            <span class="picker-stock"><strong>{{ family.total_available_stock.toLocaleString('zh-CN') }}</strong><small>件 · 本店全变体</small></span>
            <span class="picker-analysis"><span class="picker-status" :class="{ warning: pickerViewMatches(family, 'manual') || pickerViewMatches(family, 'failed'), complete: pickerStatus(family) === '已分析' }">{{ pickerStatus(family) }}</span><small>{{ formatChinaDateTime(family.latest_analysis?.created_at ?? null, '尚无分析记录') }}</small></span>
            <span class="picker-row-action">{{ selected?.key === family.key ? "当前" : "选择" }}<span aria-hidden="true"> {{ selected?.key === family.key ? '✓' : '↗' }}</span></span>
          </button>
          <button v-if="family.variant_count > 1" class="picker-expand" :aria-expanded="expandedFamily === family.key" :aria-label="`展开或收起 ${family.shared_title} 的${family.variant_count}个变体`" @click="expandedFamily = expandedFamily === family.key ? '' : family.key">{{ expandedFamily === family.key ? '收起变体 ↑' : `选择具体变体 · ${family.variant_count} ↓` }}</button>
          <div v-if="expandedFamily === family.key" class="picker-variants" role="group" :aria-label="`${family.shared_title} 的商品变体`">
            <button v-for="variant in family.variants" :key="variant.offer_id" class="picker-variant" :aria-current="variant.offer_id === selectedOfferId && String(variant.store_code ?? '') === selectedStoreCode ? 'true' : undefined" :disabled="disabled" @click="chooseVariant(variant)">
              <span><strong>{{ variant.title || variant.company_product_name || '未命名变体' }}</strong><small>{{ (family.variant_parameters_by_offer[variant.offer_id] || []).map(parameter => parameter.value).join(' · ') }}</small><small>公司SKU {{ variant.company_sku || '未关联' }} · 平台SKU {{ variant.sku || '—' }} · Offer {{ variant.offer_id }}</small></span>
              <span><strong>{{ variant.available_stock }}件</strong><small>本变体可售</small></span><span class="picker-variant-action">选择 ↗</span>
            </button>
          </div>
          </article>
        </div>
        <footer class="picker-footer">
          <span class="picker-help">↑ ↓ 浏览 · Enter 选择 · Esc 关闭<span>评分仅比较当前标题；时间为北京时间</span></span>
          <div class="picker-pagination"><span>{{ filtered.length ? (page - 1) * pageSize + 1 : 0 }}–{{ Math.min(page * pageSize, filtered.length) }} / {{ filtered.length }}</span><button aria-label="上一页商品" :disabled="page <= 1" @click="page--">←</button><label class="picker-page-jump">第<select v-model.number="page" aria-label="跳转商品页码"><option v-for="number in pageCount" :key="number" :value="number">{{ number }}</option></select>/ {{ pageCount }} 页</label><button aria-label="下一页商品" :disabled="page >= pageCount" @click="page++">→</button></div>
        </footer>
      </div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.picker-bar { display: flex; align-items: center; gap: 16px; padding: 14px 18px; border: 1px solid #d5e0d9; border-radius: 16px; background: #fff; }
button, input, select, textarea { font: inherit; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .45; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid #98b23c; outline-offset: 3px; }
.picker-trigger { display: flex; align-items: center; gap: 14px; flex: 1; min-width: 0; padding: 0; border: 0; background: none; text-align: left; color: #193c2d; }
.picker-bar-icon { display: grid; place-items: center; flex: 0 0 44px; height: 44px; border-radius: 12px; background: #e9f1db; font-size: 26px; }
.picker-bar-copy { display: grid; gap: 3px; min-width: 0; flex: 1; }
.picker-bar-copy small { color: #6a7a70; font-size: 11px; }
.picker-bar-copy strong { font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picker-bar-copy > span { font-size: 11px; color: #65766b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picker-open-label { display: flex; align-items: center; gap: 14px; flex-shrink: 0; padding: 10px 15px; background: #245d45; color: #fff; border-radius: 9px; font-size: 13px; font-weight: 700; }
.picker-stepper { display: flex; align-items: center; gap: 7px; border-left: 1px solid #e0e7e2; padding-left: 16px; }
.picker-stepper > span { max-width: 110px; font-size: 11px; color: #6d7a72; }
.picker-stepper button { width: 34px; height: 34px; border: 1px solid #d5ded7; border-radius: 8px; background: #fff; color: #254a38; }
.title-picker-dialog { box-sizing: border-box; width: min(1160px, calc(100vw - 48px)); max-width: none; max-height: calc(100dvh - 48px); padding: 0; border: 1px solid #dce5df; border-radius: 22px; color: #20392b; background: #fff; box-shadow: 0 28px 100px #102c2440; overflow: hidden; }
.title-picker-dialog::backdrop { background: #10251c88; backdrop-filter: blur(4px); }
:global(body:has(dialog.title-picker-dialog[open])) { overflow: hidden; }
.picker-shell { display: flex; flex-direction: column; height: min(850px, calc(100dvh - 50px)); min-height: 0; }
.picker-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-shrink: 0; padding: 24px 28px 18px; background: linear-gradient(115deg, #f1f6f0, #fafbec); }
.picker-header p { margin: 0 0 7px; font-size: 11px; color: #647e6c; letter-spacing: .12em; }
.picker-header h2 { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; margin: 0; font-size: 24px; letter-spacing: -.04em; }
.picker-header h2 span { border: 1px solid #dce5d8; border-radius: 20px; padding: 5px 10px; background: #ffffff9e; font-size: 11px; color: #647564; font-weight: 500; letter-spacing: 0; }
.picker-close { align-self: flex-start; border: 1px solid #d4dfd3; border-radius: 50%; background: #fff; width: 34px; height: 34px; color: #46644e; font-size: 23px; }
.picker-controls { padding: 18px 28px 0; flex-shrink: 0; max-height: 48dvh; overflow-y: auto; }
.picker-search-line { display: flex; gap: 8px; }
.picker-search-box { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; border: 1px solid #afc5b6; border-radius: 10px; background: #fbfcfb; padding: 0 12px; }
.picker-search-box > span { font-size: 24px; color: #477059; }
.picker-search-box input { flex: 1; width: 100%; min-width: 0; padding: 13px 0; border: 0; background: none; outline-offset: 0; font-size: 14px; color: #203f2c; }
.title-picker-dialog .picker-search-box input:focus-visible { outline: none; }
.picker-search-line > button { flex-shrink: 0; border: 1px solid #d6e0d9; border-radius: 10px; padding: 0 14px; background: #fff; color: #47624f; font-size: 12px; }
.picker-search-line > button[aria-expanded=true] { background: #edf4ec; border-color: #91b39b; }
.picker-views { display: flex; flex-wrap: wrap; gap: 5px; margin: 16px 0 10px; }
.picker-views button { display: flex; gap: 8px; align-items: center; padding: 8px 13px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: #637268; font-size: 12px; }
.picker-views button > span { font-size: 10px; padding: 2px 5px; border-radius: 4px; background: #edf1ed; color: #63796c; }
.picker-views button[aria-pressed=true] { background: #234f3a; color: #fff; }
.picker-views button[aria-pressed=true] > span { color: #edf5d0; background: #ffffff20; }
.picker-filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin: 14px 0; padding: 14px; border-radius: 10px; background: #f5f7f4; }
.picker-primary-filters { padding: 0; background: transparent; }
.picker-advanced-filters { border: 1px solid #e1e7de; }
.picker-filters label { display: grid; min-width: 0; gap: 6px; color: #607466; font-size: 11px; }
.picker-filters select, .picker-filters input, .picker-pagination select { box-sizing: border-box; min-width: 0; max-width: 100%; padding: 8px; border: 1px solid #d7e0d8; border-radius: 7px; background: #fff; color: #36533e; font-size: 12px; }
.picker-range { grid-column: span 2; min-width: 0; margin: 0; padding: 0; border: 0; }
.picker-range legend { margin-bottom: 6px; color: #607466; font-size: 11px; }
.picker-range > div { display: flex; align-items: center; gap: 8px; color: #81907b; }
.picker-range input { width: 0; flex: 1; }
.picker-exclude { grid-column: span 2; }
.picker-exclude small { color: #82907d; }
.picker-filter-error { margin: 8px 0; color: #9f5728; font-size: 12px; }
.picker-batch-search { display: grid; gap: 6px; margin-top: 12px; color: #607466; font-size: 12px; }
.picker-batch-search textarea { box-sizing: border-box; width: 100%; resize: vertical; max-height: 130px; padding: 10px; border: 1px solid #c6d7c8; border-radius: 8px; color: #294432; }
.picker-batch-search > span { font-size: 11px; }
.picker-chips { display: flex; flex-wrap: wrap; gap: 6px; padding-bottom: 8px; }
.picker-chips button { max-width: 100%; overflow-wrap: anywhere; text-align: left; border: 1px solid #d3dfd0; border-radius: 6px; padding: 4px 8px; color: #486b46; background: #f2f7ec; font-size: 11px; }
.picker-results-toolbar, .picker-results-toolbar > div { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.picker-results-toolbar { padding: 9px 0 14px; color: #758378; font-size: 12px; }
.picker-results-toolbar strong { color: #284c35; }
.picker-text-button { padding: 0; border: 0; background: none; color: #49705a; font-size: 11px; }
.picker-columns, .picker-row { display: grid; grid-template-columns: minmax(0, 1fr) 94px 104px 130px 50px; align-items: center; gap: 18px; }
.picker-columns { padding: 10px 28px; color: #7b8b7e; background: #f5f7f4; border-block: 1px solid #e4ebe4; font-size: 11px; flex-shrink: 0; }
.picker-results { flex: 1; min-height: 80px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 0 14px; }
.picker-row { box-sizing: border-box; width: 100%; padding: 16px 13px; border: 1px solid transparent; border-bottom-color: #eaf0e9; background: #fff; color: #304d38; text-align: left; transition: background .14s, border-color .14s; }
.picker-row:hover { background: #f5f8f1; border-radius: 10px; }
.picker-row[aria-current=true] { background: #edf4e7; border-color: #b9cda7; border-radius: 10px; }
.picker-row:focus-visible { outline-offset: -3px; }
.picker-family { border-bottom: 1px solid #eaf0e9; }
.picker-family .picker-row { border-bottom-color: transparent; }
.picker-expand { display: block; margin: -5px 13px 12px auto; padding: 5px 9px; border: 1px solid #dbe4d6; border-radius: 6px; color: #4c7050; background: #f7faf3; font-size: 11px; }
.picker-variants { margin: 0 13px 14px; padding: 0 12px; background: #f6f8f3; border: 1px solid #dfe6d9; border-radius: 10px; }
.picker-variant { display: grid; grid-template-columns: minmax(0, 1fr) 80px 58px; align-items: center; gap: 14px; width: 100%; padding: 12px 0; border: 0; border-bottom: 1px solid #e1e7dc; background: transparent; color: #36553e; text-align: left; }
.picker-variant:last-child { border-bottom: 0; }
.picker-variant > span { display: grid; min-width: 0; gap: 4px; }
.picker-variant strong { font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.picker-variant small { font-size: 10px; color: #798974; overflow-wrap: anywhere; }
.picker-variant:hover, .picker-variant[aria-current=true] { background: #eaf2e2; }
.picker-variant:focus-visible { outline-offset: -2px; }
.picker-variant-action { font-size: 11px; }
.picker-product { display: flex; align-items: flex-start; gap: 13px; min-width: 0; }
.picker-product img, .picker-image-fallback { flex-shrink: 0; width: 58px; height: 58px; border: 1px solid #e3e9e1; border-radius: 10px; object-fit: contain; background: #fff; }
.picker-image-fallback { display: grid; place-items: center; color: #92a08e; font-size: 10px; }
.picker-product-copy { display: grid; gap: 4px; min-width: 0; }
.picker-product-copy strong { font-size: 13px; line-height: 1.5; font-weight: 650; overflow-wrap: anywhere; }
.picker-product-copy small { color: #869080; font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
.picker-company-name { color: #4f7355; font-size: 11px; }
.picker-product-copy small.picker-match { color: #3e7066; }
.picker-score { font-size: 17px; font-weight: 750; color: #296242; }
.picker-score.low { color: #af6a27; }
.picker-score.muted { color: #839180; font-size: 11px; font-weight: 500; }
.picker-stock, .picker-analysis { display: grid; gap: 7px; }
.picker-stock strong { font-size: 15px; font-variant-numeric: tabular-nums; }
.picker-stock small, .picker-analysis small { color: #899384; font-size: 10px; }
.picker-status { width: fit-content; padding: 4px 7px; border-radius: 5px; background: #f0f3ec; color: #75806c; font-size: 11px; }
.picker-status.complete { background: #e6f0e5; color: #447544; }
.picker-status.warning { background: #fbf1df; color: #9d702d; }
.picker-row-action { color: #54734b; font-size: 11px; white-space: nowrap; }
.picker-empty { display: grid; justify-items: center; align-content: center; gap: 13px; min-height: 200px; text-align: center; color: #81907e; font-size: 13px; }
.picker-empty strong { color: #385b3e; font-size: 16px; }
.picker-empty button { border: 1px solid #cadac6; border-radius: 8px; padding: 10px; background: #f2f6eb; color: #497148; }
.picker-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; flex-shrink: 0; padding: 15px 28px; border-top: 1px solid #e1e9df; background: #fbfcf9; }
.picker-help { display: grid; gap: 4px; color: #8a967f; font-size: 10px; }
.picker-help > span { color: #6e8063; }
.picker-pagination { display: flex; align-items: center; gap: 10px; font-size: 11px; color: #778b6c; }
.picker-pagination label { display: flex; align-items: center; gap: 5px; }
.picker-pagination button { width: 30px; height: 30px; border: 1px solid #d5e0d0; border-radius: 7px; background: #fff; color: #55734b; }
@media (prefers-reduced-motion: reduce) { .picker-row { transition: none; } .title-picker-dialog::backdrop { backdrop-filter: none; } }
@media (max-width: 760px) {
  .picker-bar { flex-wrap: wrap; padding: 12px; gap: 10px; }
  .picker-bar-icon { display: none; }
  .picker-open-label { padding: 10px; font-size: 12px; gap: 6px; }
  .picker-stepper { border-left: 0; padding: 0; justify-content: flex-end; width: 100%; }
  .picker-bar-copy strong { font-size: 13px; }
  .title-picker-dialog { width: calc(100vw - 16px); max-height: calc(100dvh - 16px); border-radius: 15px; }
  .picker-shell { height: calc(100dvh - 18px); }
  .picker-header { padding: 16px; }
  .picker-header h2 { font-size: 20px; }
  .picker-controls { padding: 12px 14px 0; }
  .picker-search-line { flex-wrap: wrap; }
  .picker-search-box { flex-basis: 100%; }
  .picker-search-box input { font-size: 16px; padding: 10px 0; }
  .picker-search-line > button { padding: 7px 10px; }
  .picker-views { gap: 3px; margin-top: 12px; }
  .picker-views button { padding: 7px 9px; gap: 5px; font-size: 11px; }
  .picker-columns { display: none; }
  .picker-results-toolbar { padding: 6px 0 10px; gap: 7px; }
  .picker-results-toolbar > div { width: 100%; justify-content: flex-start; gap: 14px; }
  .picker-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .picker-variants { margin-inline: 5px; padding-inline: 8px; }
  .picker-variant { grid-template-columns: minmax(0, 1fr) 64px; gap: 8px; }
  .picker-variant .picker-variant-action { display: none; }
  .picker-results { padding: 0 6px; }
  .picker-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.25fr); gap: 12px; padding: 14px 10px; }
  .picker-product { grid-column: 1 / -1; gap: 10px; }
  .picker-product img, .picker-image-fallback { width: 46px; height: 46px; }
  .picker-product-copy strong { font-size: 12px; }
  .picker-row-action { display: none; }
  .picker-stock strong, .picker-score { font-size: 14px; }
  .picker-footer { padding: 10px 14px; gap: 8px; }
  .picker-help { display: none; }
  .picker-pagination { justify-content: space-between; width: 100%; gap: 5px; }
}
</style>
