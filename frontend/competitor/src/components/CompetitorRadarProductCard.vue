<script setup lang="ts">
import { computed } from "vue";
import { cachedNumberFormatter } from "../numberFormatters";
import CompetitorObservedSalesMetrics from "./CompetitorObservedSalesMetrics.vue";
import OwnStoreSalesComparisonMetrics from "./OwnStoreSalesComparisonMetrics.vue";
import OwnStoreListingTime from "./OwnStoreListingTime.vue";
import CompetitorPriceSummary from "./CompetitorPriceSummary.vue";
import RadarCardProfit from "./RadarCardProfit.vue";
import RadarProductImage from "./RadarProductImage.vue";
import { radarCardSellers, radarBrandLabel, type RadarSeller } from "../radarSellerProducts";
import { radarCardPlatformUrl } from "../radarCardProfit";
import { ownOfferLatestStatusLabel } from "../ownOfferLatestStatus";
import {
  comparisonOffers,
  followerOffers,
  groupCompetitorOffersBySeller,
} from "../competitorOfferHistory";
import type { CompetitorCategoryBreadcrumb, CompetitorItem, OwnStoreScope } from "../types";
import { formatChinaDateTime } from "../time";

const props = withDefaults(defineProps<{
  item: CompetitorItem;
  cardId?: string;
  selected?: boolean;
  personalWatchlist?: boolean;
  imageSrc?: string;
  showImage?: boolean;
  storeScope?: OwnStoreScope;
  storeCode?: string;
}>(), {
  cardId: undefined,
  selected: false,
  personalWatchlist: false,
  imageSrc: "",
  showImage: false,
  storeScope: "current",
  storeCode: "",
});

const emit = defineEmits<{
  (event: "open-detail", item: CompetitorItem): void;
  (
    event: "open-category",
    category: CompetitorCategoryBreadcrumb,
    mouseEvent: MouseEvent,
  ): void;
  (event: "query-competitors", item: CompetitorItem, mouseEvent: MouseEvent): void;
  (event: "image-error", imageEvent: Event, imageUrl: string | null): void;
  (event: "open-seller", seller: RadarSeller): void;
}>();
const sellers = computed(() => radarCardSellers(props.item));

function formatCurrency(value: number | null): string {
  return value === null
    ? "—"
    : cachedNumberFormatter("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value);
}

function followerSellerCount(item: CompetitorItem): number {
  return groupCompetitorOffersBySeller(followerOffers(item), "default").length;
}

function ownStoreNames(item: CompetitorItem): string {
  return [...new Set(item.自有报价.map((offer) => offer.店铺).filter(Boolean))].join("、");
}

function ownStoreVariantCount(item: CompetitorItem): number {
  return new Set(comparisonOffers(item)
    .filter((offer) => offer.报价来源 === "seller_api")
    .map((offer) => String(offer.TSIN || offer.图片 || offer.变体键 || offer.SKU || offer.offer_id || offer.报价键).trim())
    .filter(Boolean)).size;
}

function periodInventoryTurnoverLabel(item: CompetitorItem): string {
  if (item.周期销售额 === null) {
    return "连续精确库存或对应价格不足，未显示部分金额";
  }
  return [
    `下降 ${item.周期销售件数 ?? 0} 件`,
    `补货 ${item.周期补货量 ?? 0} 件 / ${formatCurrency(item.周期补货货值)}`,
    `周转 ${formatCurrency(item.周期库存周转金额)}`,
  ].join(" · ");
}

function latestReviewCountLabel(item: CompetitorItem): string {
  const value = typeof item.最新评论数 === "number"
    ? item.最新评论数
    : item.评论数可用 === false
      ? null
      : item.评论数;
  return value === null ? "数据不足" : `${value.toLocaleString("zh-CN")} 条`;
}

function categoryPath(item: CompetitorItem): CompetitorCategoryBreadcrumb[] {
  return (item.类目路径 ?? []).filter((breadcrumb) => breadcrumb.name.trim());
}

function categoryLevelLabel(index: number, total: number): string {
  if (total <= 1 || index === total - 1) return "精确类目";
  if (index === 0) return "大类";
  return `${index + 1} 级`;
}
</script>

<template>
  <article
    :id="props.cardId"
    class="competitor-status-card radar-card-refined"
    :class="{
      selected: props.selected,
      'own-store-card competitor-category-product-card is-own-store': props.item.来源 === 'own_store',
    }"
  >
    <header class="competitor-status-header">
      <div class="competitor-status-identity">
        <RadarProductImage :src="props.imageSrc" :title="props.item.商品" :show="props.showImage" @image-error="emit('image-error', $event, props.item.图片)" />
        <div class="competitor-status-title">
          <div class="competitor-status-eyebrow">
            <strong v-if="props.item.来源 === 'own_store'" class="competitor-category-source-badge is-own-store">自有链接</strong>
            <span>PLID{{ props.item.plid }}</span>
            <strong
              v-if="props.personalWatchlist"
              class="personal-watchlist-badge"
            >我的监控池</strong>
          </div>
          <h3 :title="props.item.商品">
            <a v-if="radarCardPlatformUrl(props.item)" class="radar-card-platform-link"
              :href="radarCardPlatformUrl(props.item)!" target="_blank" rel="noopener noreferrer"
              :aria-label="`${props.item.商品}，在新标签页打开平台商品页`">
              {{ props.item.商品 }}<span aria-hidden="true" class="radar-card-external-mark">↗</span>
            </a>
            <span v-else>{{ props.item.商品 }}</span>
          </h3>
          <small v-if="!radarCardPlatformUrl(props.item)" class="radar-card-link-missing">平台链接待补齐</small>
          <div class="radar-brand-sellers">
            <span v-if="radarBrandLabel(props.item) !== '待采集'">品牌 {{ radarBrandLabel(props.item) }}</span>
            <button v-for="seller in sellers.slice(0, 3)" :key="seller.key" type="button" class="radar-seller-link" @click="emit('open-seller', seller)">{{ seller.name }}</button>
            <span v-if="sellers.length > 3" :title="sellers.map(seller => seller.name).join('、')">等{{ sellers.length }}家</span>
            <span v-if="!sellers.length">卖家待采集</span>
          </div>
          <template v-if="props.item.来源 === 'own_store'">
            <p class="own-store-company-skus" :title="props.item.company_skus?.join('、')">
              公司 SKU {{ props.item.company_skus?.length ? props.item.company_skus.join("、") : "未关联" }}
            </p>
            <div class="own-offer-latest-statuses">
              <span>状态</span>
              <strong v-for="status in props.item.最新Offer状态 || []" :key="status"
                class="own-offer-status-pill" :class="`status-${status}`" :title="status">
                {{ ownOfferLatestStatusLabel(status).replace(/\s*[（(].*?[）)]/g, '') }}
              </strong>
              <strong v-if="!props.item.最新Offer状态?.length" class="own-offer-status-pill status-unknown">当前状态缺失</strong>
            </div>
          </template>
          <div class="radar-card-times">
            <OwnStoreListingTime v-if="props.item.来源 === 'own_store'" :item="props.item" />
            <span class="competitor-first-monitored-badge"><small>首次监控</small><strong>{{ formatChinaDateTime(props.item.首次监控时间 ?? null) }}</strong></span>
          </div>
        </div>
      </div>
    </header>

    <div class="competitor-status-summary">
      <div class="radar-card-price-column">
        <CompetitorPriceSummary :item="props.item">
          <template #own-extra>
            <RadarCardProfit :item="props.item" :store-scope="props.storeScope" :store-code="props.storeCode" />
          </template>
        </CompetitorPriceSummary>
      </div>
      <div class="radar-card-stock">
        <span>{{ props.item.来源 === 'own_store' ? '官方库存' : '主报价库存' }}</span>
        <strong
          class="stock-pill"
          :class="{
            exact: props.item.库存精确,
            unavailable: props.item.库存上限 === '没货',
          }"
        >{{ props.item.库存上限 }}</strong>
        <small v-if="props.item.库存参考过期 && props.item.上次成功库存">
          上次成功 {{ props.item.上次成功库存 }} ·
          {{ formatChinaDateTime(props.item.上次成功库存时间) }}
        </small>
        <small v-else-if="props.item.来源 !== 'own_store'">{{ props.item.当前卖家 || "未知卖家" }}</small>
      </div>
      <div class="competitor-period-revenue">
        <span>观察销售额</span>
        <strong>{{ formatCurrency(props.item.周期销售额) }}</strong>
        <small :title="periodInventoryTurnoverLabel(props.item)">库存观察 · 非订单</small>
      </div>
      <div class="competitor-card-category" aria-label="商品类目层级">
        <span>商品类目</span>
        <template v-if="categoryPath(props.item).length">
        <ol>
          <li
            v-for="(category, categoryIndex) in categoryPath(props.item)"
            :key="`${category.id || category.slug || category.name}-${categoryIndex}`"
          >
            <button
              class="competitor-category-node-button"
              type="button"
              :aria-label="`查看 ${category.name} 类目的全部系统商品`"
              @click.stop="emit('open-category', category, $event)"
            >
              <small>{{ categoryLevelLabel(categoryIndex, categoryPath(props.item).length) }}</small>
              <strong>{{ category.name }}</strong>
            </button>
          </li>
        </ol>
        </template>
        <p v-else class="competitor-card-category-empty">
          类目待采集 · 后续成功采集后补齐
        </p>
      </div>
      <div class="radar-card-reviews">
        <span title="最新评论数（PLID 共用）">评论（PLID）</span>
        <strong :title="`评论更新 ${formatChinaDateTime(props.item.最新评论获取时间 ?? null)}`">{{ latestReviewCountLabel(props.item) }}</strong>
        <small>评分 {{ props.item.评分 ?? "—" }}</small>
      </div>
      <OwnStoreSalesComparisonMetrics
        v-if="props.item.来源 === 'own_store'"
        :own-values="props.item.自有官方销量"
        :own-through-date="props.item.自有官方销量截至"
        :follower-values="props.item.跟卖近期观察售出"
        :follower-through-date="props.item.跟卖近期观察售出截至"
        :own-context-label="`${props.item.自有官方销量店铺数 ?? 0}店 · ${props.item.自有官方销量Offer数 ?? 0} Offer`"
        :follower-context-label="`${followerSellerCount(props.item)}卖家 · ${props.item.跟卖报价.length} 报价`"
      />
      <CompetitorObservedSalesMetrics
        v-else
        class="competitor-status-observed-sales"
        :values="props.item.近期观察售出"
        :through-date="props.item.近期观察售出截至"
        context-label="全部卖家 · 全部变体"
        compact
      />
    </div>
    <footer class="competitor-card-query-actions">
      <button type="button" class="radar-card-detail-button"
        :aria-label="props.item.来源 === 'own_store' ? `在新标签页查看 ${props.item.商品} 自有链接详情` : `查看 ${props.item.商品} 完整详情`"
        :aria-haspopup="props.item.来源 === 'own_store' ? undefined : 'dialog'"
        @click="emit('open-detail', props.item)">查看详情</button>
      <button
        type="button"
        class="competitor-query-button"
        :aria-label="`查询 ${props.item.商品} 的竞品`"
        @click.stop="emit('query-competitors', props.item, $event)"
      >
        竞品查询
      </button>

    </footer>
  </article>
</template>
