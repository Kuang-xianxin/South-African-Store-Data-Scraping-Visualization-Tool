<script setup lang="ts">
import type { CompetitorItem, CompetitorCategoryBreadcrumb } from "../types";
import { formatChinaDateTime } from "../time";
import CompetitorObservedSalesMetrics from "./CompetitorObservedSalesMetrics.vue";
const props = defineProps<{
  item: CompetitorItem;
  personalMember: boolean;
  showImage: boolean;
  imageUrl: string;
  presentation: {
    formatCurrency: (value: number | null) => string;
    latestReviewCountLabel: (item: CompetitorItem) => string;
    competitorCategoryPath: (item: CompetitorItem) => CompetitorCategoryBreadcrumb[];
    competitorCategoryLevelLabel: (index: number, total: number) => string;
    periodInventoryTurnoverLabel: (item: CompetitorItem) => string;
    competitorOfferPriceRange: (item: CompetitorItem) => string;
    followerSellerCount: (item: CompetitorItem) => number;
  };
}>();
const { formatCurrency, latestReviewCountLabel, competitorCategoryPath, competitorCategoryLevelLabel, periodInventoryTurnoverLabel, competitorOfferPriceRange, followerSellerCount } = props.presentation;
const emit = defineEmits<{
  open: [event: MouseEvent | KeyboardEvent];
  query: [event: MouseEvent];
  category: [category: CompetitorCategoryBreadcrumb, event: MouseEvent];
  'image-error': [event: Event];
}>();
</script>
<template>
  <article class="competitor-status-card" :class="{ 'own-store-card': item.来源 === 'own_store' }"
    tabindex="0" role="button" aria-haspopup="dialog"
    :aria-label="`查看 ${item.商品} 及全部 ${item.跟卖报价.length} 个报价的详情`"
    @click="emit('open', $event)" @keydown.enter.self="emit('open', $event)"
    @keydown.space.self.prevent="emit('open', $event)">
            <header class="competitor-status-header">
              <div class="competitor-status-identity">
                <div class="competitor-product-image competitor-status-image">
                  <img
                    v-if="showImage"
                    :src="imageUrl"
                    :alt="`${item.商品} 商品图片`"
                    width="192"
                    height="192"
                    loading="lazy"
                    decoding="async"
                    @error="emit('image-error', $event)"
                  />
                  <span v-else>暂无图片</span>
                </div>
                <div class="competitor-status-title">
                  <div class="competitor-status-eyebrow">
                    <span>{{ item.来源 === "own_store" ? "自有 · " : "" }}PLID{{ item.plid }}</span>
                    <span>{{ formatChinaDateTime(item.采集时间) }}</span>
                    <strong
                      v-if="personalMember"
                      class="personal-watchlist-badge"
                    >我的监控池</strong>
                  </div>
                  <h3>{{ item.商品 }}</h3>
                  <p>{{ followerSellerCount(item) }} 个卖家 · {{ item.跟卖报价.length }} 个变体 / 报价 · 主卖家 {{ item.当前卖家 || "未知" }}</p>
                </div>
              </div>
              <div class="competitor-status-header-actions">
                <span class="competitor-first-monitored-badge">
                  <small>首次监控</small>
                  <strong>{{ formatChinaDateTime(item.首次监控时间 ?? null) }}</strong>
                </span>
                <button type="button" class="secondary-button competitor-query-button" @click.stop="emit('query', $event)">竞品查询</button>
                <span class="competitor-status-open">查看卖家库存 →</span>
              </div>
            </header>

            <div class="competitor-status-summary">
              <div>
                <span>报价区间 / 主报价</span>
                <strong>{{ competitorOfferPriceRange(item) }}</strong>
                <small>主报价 {{ formatCurrency(item.价格) }}</small>
              </div>
              <div>
                <span>主报价库存</span>
                <strong
                  class="stock-pill"
                  :class="{
                    exact: item.库存精确,
                    unavailable: item.库存上限 === '没货',
                  }"
                >{{ item.库存上限 }}</strong>
                <small v-if="item.库存参考过期 && item.上次成功库存">
                  上次成功 {{ item.上次成功库存 }}
                  · {{ formatChinaDateTime(item.上次成功库存时间) }}
                </small>
                <small v-else>{{ item.当前卖家 || "未知卖家" }}</small>
              </div>
              <div class="competitor-period-revenue">
                <span>周期内销售额</span>
                <strong>{{ formatCurrency(item.周期销售额) }}</strong>
                <small>{{ periodInventoryTurnoverLabel(item) }}</small>
              </div>
              <div class="competitor-card-category" aria-label="商品类目层级">
                <span>商品类目</span>
                <ol v-if="competitorCategoryPath(item).length">
                  <li
                    v-for="(category, categoryIndex) in competitorCategoryPath(item)"
                    :key="`${category.id || category.slug || category.name}-${categoryIndex}`"
                  >
                    <button
                      class="competitor-category-node-button"
                      type="button"
                      :aria-label="`查看 ${category.name} 类目的全部系统商品`"
                      @click.stop="emit('category', category, $event)"
                    >
                      <small>
                        {{ competitorCategoryLevelLabel(categoryIndex, competitorCategoryPath(item).length) }}
                      </small>
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
                <strong>{{ latestReviewCountLabel(item) }}</strong>
                <small v-if="item.最新评论获取时间">
                  评论更新 {{ formatChinaDateTime(item.最新评论获取时间) }} · 区间末评分 {{ item.评分 ?? "—" }}
                </small>
                <small v-else>公开评论尚未同步 · 区间末评分 {{ item.评分 ?? "—" }}</small>
              </div>
              <CompetitorObservedSalesMetrics
                class="competitor-status-observed-sales"
                :values="item.近期观察售出"
                :through-date="item.近期观察售出截至"
                context-label="全部卖家 · 全部变体"
                compact
              />
            </div>
            </article>
</template>
