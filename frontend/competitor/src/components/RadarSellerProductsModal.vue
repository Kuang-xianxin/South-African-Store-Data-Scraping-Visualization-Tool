<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { AUTH_SESSION_ENDING_EVENT, fetchCompetitors, fetchOwnStoreCompetitors } from "../api";
import type { CompetitorItem, CompetitorOverview, OwnStoreScope, RadarListRequest } from "../types";
import { matchesRadarSeller, radarBrandLabel, type RadarSeller } from "../radarSellerProducts";
import { radarCardPlatformUrl } from "../radarCardProfit";
import { productThumbnailUrl } from "../productImages";
import RadarProductImage from "./RadarProductImage.vue";
const props = defineProps<{ seller: RadarSeller; storeScope: OwnStoreScope; storeCode?: string }>();
const emit = defineEmits<{ (event: "close"): void; (event: "open-detail", item: CompetitorItem): void }>();
const dialog = ref<HTMLDialogElement | null>(null);
const items = shallowRef<CompetitorItem[]>([]);
const loading = ref(false), error = ref(""), query = ref(""), page = ref(1);
const failedImages = ref(new Set<string>());
const pageSize = 20;
let controller: AbortController | null = null;
let revision = 0;
const filtered = computed(() => {
  const term = query.value.trim().toLowerCase();
  return items.value.filter((item) => !term || `${item.商品} ${item.plid} ${radarBrandLabel(item)} ${(item.company_skus || []).join(" ")}`.toLowerCase().includes(term));
});
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)));
const visible = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize));
watch(query, () => { page.value = 1; });
watch(pages, (value) => { page.value = Math.min(page.value, value); });
async function load() {
  const version = ++revision;
  controller?.abort(); const pending = new AbortController(); controller = pending;
  items.value = []; error.value = ""; loading.value = true; page.value = 1;
  const found = new Map<string, CompetitorItem>();
  try {
    // Explicit seller click only. Server search narrows results; exact identity is rechecked.
    const sources = props.seller.storeCode ? ["own_store"] as const : ["competitor", "own_store"] as const;
    for (const source of sources) {
      let currentPage = 1, totalPages = 1;
      do {
        const paging: RadarListRequest = { page: currentPage, page_size: 100, q: source === "own_store" ? props.seller.id || props.seller.name : "",
          seller: props.seller.id ? `sellers ${props.seller.id}` : props.seller.name,
          stock: "全部", status: "全部", follower: "全部", signal: "全部", direction: "desc", sort: "sales_30", watchlist: false };
        const result = source === "own_store"
          ? await fetchOwnStoreCompetitors(undefined, undefined, props.storeScope, pending.signal, undefined, undefined, paging)
          : await fetchCompetitors(undefined, undefined, props.storeScope, pending.signal, false, undefined, paging);
        if (version !== revision || pending.signal.aborted) return;
        const rows = source === "own_store" ? result.store_items : (result as CompetitorOverview).items;
        for (const item of rows) if (matchesRadarSeller(item, props.seller)) found.set(item.plid, item);
        items.value = [...found.values()];
        totalPages = result.pagination ? Math.max(1, Math.ceil(result.pagination.total / 100)) : 1;
        currentPage++;
      } while (currentPage <= totalPages);
    }
  } catch (reason) {
    if (version !== revision || pending.signal.aborted) return;
    error.value = reason instanceof Error ? reason.message : "卖家商品读取失败";
  } finally { if (version === revision) loading.value = false; }
}
function close() { ++revision; controller?.abort(); dialog.value?.close(); emit("close"); }
function detail(item: CompetitorItem) { close(); emit("open-detail", item); }
watch(() => [props.seller.key, props.storeScope, props.storeCode], load, { immediate: true });
onMounted(() => { dialog.value?.showModal(); window.addEventListener(AUTH_SESSION_ENDING_EVENT, close); });
onBeforeUnmount(() => { ++revision; controller?.abort(); window.removeEventListener(AUTH_SESSION_ENDING_EVENT, close); });
</script>
<template>
  <Teleport to="body">
    <dialog ref="dialog" class="radar-seller-dialog" @keydown.stop @cancel.prevent="close">
      <header><div><h2>{{ seller.name }} · 商品</h2><p>当前授权范围内，系统已收录且匹配该卖家身份的全部商品；未采集商品不在此列表。</p></div><button type="button" autofocus @click="close">关闭</button></header>
      <div class="radar-seller-toolbar"><input v-model="query" type="search" placeholder="搜索商品、PLID、品牌或公司SKU" aria-label="搜索卖家商品" /><span>{{ loading ? '正在读取，已找到' : error ? '已读取部分' : '共' }} {{ items.length }} 个商品</span></div>
      <p v-if="loading" role="status">正在分页核对该卖家的商品…</p>
      <p v-if="error" role="alert">{{ error }}；当前结果可能不完整。<button type="button" @click="load">重试</button></p>
      <div class="radar-seller-table-scroll">
        <table><thead><tr><th>图片</th><th>商品 / 品牌</th><th>链接参考价（ZAR）</th><th>链接库存</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in visible" :key="item.plid">
            <td><RadarProductImage :src="productThumbnailUrl(item.图片)" :show="Boolean(item.图片) && !failedImages.has(item.plid)" :title="item.商品" @image-error="failedImages.add(item.plid)" /></td>
            <td><a v-if="radarCardPlatformUrl(item)" :href="radarCardPlatformUrl(item)!" target="_blank" rel="noopener noreferrer">{{ item.商品 }}</a><strong v-else>{{ item.商品 }}</strong><small>PLID{{ item.plid }} · 品牌：{{ radarBrandLabel(item) }}</small></td>
            <td>{{ item.价格 === null ? '—' : `R ${item.价格.toLocaleString('en-ZA', { minimumFractionDigits: 2 })}` }}</td><td>{{ item.库存上限 }}</td><td><button type="button" @click="detail(item)">查看详情</button></td>
          </tr></tbody>
        </table>
        <p v-if="!loading && !visible.length">{{ query ? '没有匹配搜索的商品' : '当前范围没有可匹配的已收录商品' }}</p>
      </div>
      <footer><button type="button" :disabled="page <= 1" @click="page--">上一页</button><span>{{ page }} / {{ pages }} 页 · 筛选后 {{ filtered.length }} 个</span><button type="button" :disabled="page >= pages" @click="page++">下一页</button></footer>
    </dialog>
  </Teleport>
</template>
