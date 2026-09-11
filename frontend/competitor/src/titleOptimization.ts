import type { SearchRankingAnalysis, SearchRankingKeywordResult, SearchRankingTitleStrategy } from "./types.ts";

export function keywordRankingRows(keywords: SearchRankingKeywordResult[]) {
  return keywords.map((item, index) => {
    const scanned = item.pages_scanned > 0;
    const located = scanned && item.found && Number.isInteger(item.organic_rank) && item.organic_rank! > 0;
    const adopted = ["accepted", "opportunity", "comparison_resample"].includes(item.relevance_status);
    const scope = !scanned
      ? item.relevance_status === "model_low_confidence" ? "商品识别待核实，未发起搜索" : "尚未完成搜索采集"
      : located && item.page_number ? `第 ${item.page_number} 页`
      : item.found ? "已找到商品，排名数据不完整"
      : item.pages_scanned === 1 && item.relevance_status === "rejected_irrelevant"
        ? "仅检查首页，未继续翻页"
        : `已扫描 ${item.pages_scanned} 页`;
    return {
      item, located, scope,
      rank: located ? `#${item.organic_rank}` : !scanned ? "未采集" : item.found ? "待核实" : "扫描范围内未找到",
      observedAt: scanned ? item.observed_at : null,
      relation: ({ accepted: "已采纳词", opportunity: "拓展词", comparison_resample: "复采词", rejected_irrelevant: "未纳入推荐", model_low_confidence: "待核实词" })[item.relevance_status],
      priority: adopted && scanned ? 0 : located ? 1 : scanned ? 2 : 3,
      index,
    };
  }).sort((a, b) => a.priority - b.priority || a.item.candidate_order - b.item.candidate_order || a.index - b.index);
}

export function keywordRankingContext(analysis: SearchRankingAnalysis, currentTitle: string, currentOfferId?: string, currentPlid?: string | null) {
  const sourceOffer = analysis.source_offer_id;
  const currentOffer = currentOfferId || analysis.variant_projection?.current_offer_id || sourceOffer;
  const sourceVariant = analysis.variant_family?.variants?.find((item) => item.offer_id === sourceOffer);
  const sameLink = sourceOffer === currentOffer || Boolean(currentPlid && sourceVariant?.productline_id === currentPlid);
  const titleChanged = currentTitle.trim().replace(/\s+/g, " ").toLowerCase()
    !== analysis.source_title.trim().replace(/\s+/g, " ").toLowerCase();
  return {
    sameLink,
    note: !sameLink ? `采集对象为同组商品（Offer ${sourceOffer}），以下排名并非当前链接的实测结果。`
      : titleChanged ? "以下为上次采集时的排名，当前标题已变化，需重新采集验证。" : "",
  };
}

export function titleDiagnosis(analysis: SearchRankingAnalysis | null, currentTitle: string) {
  const score = analysis?.title_score;
  const stale = Boolean(analysis && (
    score?.current_title_match === false
    || analysis.variant_projection?.family_snapshot_current === false
    || analysis.variant_projection?.decision_parameter_confirmation_current === false
  ));
  if (!analysis) return { label: "待分析", reasons: ["分析后查看标题建议和真实竞品参考。"], usable: false, keep: false };
  if (stale) return { label: "需要重新分析", reasons: ["标题或商品资料已变化，旧建议暂不适用。"], usable: false, keep: false };
  if (!score || score.band === "insufficient_evidence" || analysis.manual_fact_required) {
    return { label: "资料不足待核实", reasons: ["现有依据不足，暂不判断标题好坏。"], usable: false, keep: false };
  }
  const issues = score.components.filter((part) => part.available && part.score !== null
    && part.max_points > 0 && part.score / part.max_points < 0.7);
  const reasons = issues.slice(0, 2).map((part) => part.summary);
  const keep = score.band === "strong" && issues.length === 0 && Boolean(currentTitle.trim());
  return {
    label: keep ? "建议保持" : score.band === "weak" ? "建议重写" : "小幅优化",
    reasons: reasons.length ? reasons : [keep ? "核心品名和主要事实表达完整，暂未发现必须修改的问题。" : "优先调整完整品名与卖点的排列，保留真实规格。"],
    usable: true, keep,
  };
}

export function primaryTitle(strategies: SearchRankingTitleStrategy[]) {
  return strategies.find((item) => item.strategy === "contiguous_core" && item.available && item.title)
    ?? strategies.find((item) => item.available && item.title) ?? null;
}

export function titleWordChanges(before: string, after: string) {
  const beforeWords = before.trim().split(/\s+/).filter(Boolean);
  const afterWords = after.trim().split(/\s+/).filter(Boolean);
  const beforeKeys = new Set(beforeWords.map((word) => word.toLowerCase()));
  const afterKeys = new Set(afterWords.map((word) => word.toLowerCase()));
  return {
    added: afterWords.filter((word) => !beforeKeys.has(word.toLowerCase())),
    removed: beforeWords.filter((word) => !afterKeys.has(word.toLowerCase())),
    reordered: beforeWords.filter((word) => afterKeys.has(word.toLowerCase())).join(" ").toLowerCase()
      !== afterWords.filter((word) => beforeKeys.has(word.toLowerCase())).join(" ").toLowerCase(),
  };
}
