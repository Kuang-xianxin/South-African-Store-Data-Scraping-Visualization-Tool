<script setup lang="ts">
import CompetitorObservedSalesMetrics from "./CompetitorObservedSalesMetrics.vue";
import {
  followerOffers,
  groupCompetitorOffersBySeller,
} from "../competitorOfferHistory";
import type { CompetitorCategoryBreadcrumb, CompetitorItem } from "../types";
import { formatChinaDateTime } from "../time";

const props = withDefaults(defineProps<{
  item: CompetitorItem;
  cardId?: string;
  selected?: boolean;
  personalWatchlist?: boolean;
  imageSrc?: string;
  showImage?: boolean;
}>(), {
  cardId: undefined,
  selected: false,
  personalWatchlist: false,
  imageSrc: "",
  showImage: false,
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
}>();

function formatCurrency(value: number | null): string {
  return value === null
    ? "—"
    : new Intl.NumberFormat("en-ZA", {
        style: "currency",
        currency: "ZAR",
        maximumFractionDigits: 2,
      }).format(value);
}

function followerSellerCount(item: CompetitorItem): number {
  return groupCompetitorOffersBySeller(followerOffers(item), "default").length;
}

function competitorOfferPriceRange(item: CompetitorItem): string {
  const prices = item.跟卖报价
    .map((offer) => offer.价格)
    .filter((price): price is number => price !== null)
    .sort((first, second) => first - second);
  if (!prices.length) return formatCurrency(item.价格);
  const lowest = prices[0]!;
  const highest = prices[prices.length - 1]!;
  return lowest === highest
    ? formatCurrency(lowest)
    : `${formatCurrency(lowest)} – ${formatCurrency(highest)}`;
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
    class="competitor-status-card"
    :class="{ selected: props.selected }"
    tabindex="0"
    role="button"
    aria-haspopup="dialog"
    :aria-label="`查看 ${props.item.商品} 及全部 ${props.item.跟卖报价.length} 个报价的详情`"
    @click="emit('open-detail', props.item)"
    @keydown.enter.self="emit('open-detail', props.item)"
    @keydown.space.self.prevent="emit('open-detail', props.item)"
  >
    <header class="competitor-status-header">
      <div class="competitor-status-identity">
        <div class="competitor-product-image competitor-status-image">
          <img
            v-if="props.showImage"
            :src="props.imageSrc"
            :alt="`${props.item.商品} 商品图片`"
            width="192"
            height="192"
            loading="lazy"
            decoding="async"
            @error="emit('image-error', $event, props.item.图片)"
          />
          <span v-else>暂无图片</span>
        </div>
        <div class="competitor-status-title">
          <div class="competitor-status-eyebrow">
            <span>PLID{{ props.item.plid }}</span>
            <span>{{ formatChinaDateTime(props.item.采集时间) }}</span>
            <strong
              v-if="props.personalWatchlist"
              class="personal-watchlist-badge"
            >我的监控池</strong>
          </div>
          <h3>{{ props.item.商品 }}</h3>
          <p>
            {{ followerSellerCount(props.item) }} 个卖家 ·
            {{ props.item.跟卖报价.length }} 个变体 / 报价 ·
            主卖家 {{ props.item.当前卖家 || "未知" }}
          </p>
        </div>
      </div>
      <div class="competitor-status-header-actions">
        <span class="competitor-first-monitored-badge">
          <small>首次监控</small>
          <strong>{{ formatChinaDateTime(props.item.首次监控时间 ?? null) }}</strong>
        </span>
        <span class="competitor-status-open">查看卖家库存 →</span>
      </div>
    </header>

    <div class="competitor-status-summary">
      <div>
        <span>报价区间 / 主报价</span>
        <strong>{{ competitorOfferPriceRange(props.item) }}</strong>
        <small>主报价 {{ formatCurrency(props.item.价格) }}</small>
      </div>
      <div>
        <span>主报价库存</span>
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
        <small v-else>{{ props.item.当前卖家 || "未知卖家" }}</small>
      </div>
      <div class="competitor-period-revenue">
        <span>周期内销售额</span>
        <strong>{{ formatCurrency(props.item.周期销售额) }}</strong>
        <small>{{ periodInventoryTurnoverLabel(props.item) }}</small>
      </div>
      <div class="competitor-card-category" aria-label="商品类目层级">
        <span>商品类目</span>
        <ol v-if="categoryPath(props.item).length">
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
        <p v-else class="competitor-card-category-empty">
          类目待采集 · 后续成功采集后补齐
        </p>
      </div>
      <div>
        <span>最新评论数（PLID 共用）</span>
        <strong>{{ latestReviewCountLabel(props.item) }}</strong>
        <small v-if="props.item.最新评论获取时间">
          评论更新 {{ formatChinaDateTime(props.item.最新评论获取时间) }} ·
          区间末评分 {{ props.item.评分 ?? "—" }}
        </small>
        <small v-else>公开评论尚未同步 · 区间末评分 {{ props.item.评分 ?? "—" }}</small>
      </div>
      <CompetitorObservedSalesMetrics
        class="competitor-status-observed-sales"
        :values="props.item.近期观察售出"
        :through-date="props.item.近期观察售出截至"
        context-label="全部卖家 · 全部变体"
        compact
      />
    </div>
    <footer class="competitor-card-query-actions">
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
