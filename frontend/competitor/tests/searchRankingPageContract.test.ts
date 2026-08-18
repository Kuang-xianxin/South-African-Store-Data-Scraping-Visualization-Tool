import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/SearchRankingPage.vue", import.meta.url),
  "utf8",
);
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const statusTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingStatus"),
  typesSource.indexOf("export interface SearchRankingAnalysisSummary"),
);
const summaryTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingAnalysisSummary"),
  typesSource.indexOf("export interface SearchRankingProduct extends"),
);
const analysisTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingAnalysis extends"),
  typesSource.indexOf("export interface SearchRankingListPayload"),
);

test("normalizes current and analyzed titles case-insensitively", () => {
  const comparisonBlock = pageSource.slice(
    pageSource.indexOf("const currentTitleChangedSinceAnalysis"),
    pageSource.indexOf("const acceptedKeywords"),
  );

  assert.equal(comparisonBlock.match(/\.toLocaleLowerCase\(\)/g)?.length, 2);
});

test("an explicit strategy contract never reuses legacy reasons for unavailable cards", () => {
  assert.match(
    pageSource,
    /explanation: hasStrategyContract\s+\? "本轮没有按当前核心词门槛形成可用的连续词组方案。"\s+: current\.title_reason/,
  );
  assert.match(
    pageSource,
    /旧记录没有现行S\/A语义关系证据，已安全停用/,
  );
});

test("keeps ordinary specifications at the title tail and gates decision parameters", () => {
  assert.match(pageSource, /商品类型与相关关键词前置，功能卖点居中/);
  assert.match(pageSource, /功率、电压、容量、尺寸、重量、数量及防护等级等明确规格参数默认后置/);
  assert.match(pageSource, /只有运营在上方逐项确认为决策参数/);
  assert.match(pageSource, /300W、IP66 等未确认或验证未通过时仍保持后置/);
  assert.match(typesSource, /"human_confirmed_decision_parameter"/);
  assert.match(typesSource, /same_type_validation_controlled_aliases\?: string\[\];/);
});

test("shows title-bound decision controls without long policy introductions", () => {
  const decisionSectionIndex = pageSource.indexOf("决策参数人工确认");
  const analysisOnlyIndex = pageSource.indexOf('<template v-if="analysis">');

  assert.ok(decisionSectionIndex > 0);
  assert.ok(analysisOnlyIndex > decisionSectionIndex);
  assert.doesNotMatch(pageSource, /decision-parameter-policy/);
  assert.doesNotMatch(pageSource, /product-fact-policy/);
  assert.doesNotMatch(pageSource, /本区随当前选中的 Offer 变体切换/);
  assert.doesNotMatch(pageSource, /商品事实只接受运营人工确认/);
  assert.doesNotMatch(pageSource, /这里只接受运营根据供应商资料或实物作出的人工确认/);
  assert.match(pageSource, /是 决策参数/);
  assert.match(pageSource, /不是 保持后置/);
  assert.match(pageSource, /确认当前标题无可识别规格/);
  assert.match(pageSource, /需要重新验证定位，只有通过同商品族搜索页验证的决策参数才会前置/);
  assert.match(apiSource, /\/decision-parameters\/confirm/);
  assert.match(typesSource, /decision_parameter_confirmation_mode: "manual_per_title"/);
  assert.match(typesSource, /decision_parameter_profile: SearchRankingDecisionParameterProfile/);
});

test("keeps product and store transitions from anchoring the viewport to the root library", () => {
  assert.match(pageSource, /window\.scrollTo\(\{ top: 0, left: 0, behavior: "auto" \}\)/);
  assert.match(pageSource, /const rankingDetailElement = ref<HTMLElement \| null>\(null\)/);
  assert.match(pageSource, /rankingDetailElement\.value\?\.getBoundingClientRect\(\)\.height/);
  assert.match(pageSource, /ref="rankingDetailElement"/);
  assert.match(pageSource, /minHeight: `\$\{rankingDetailMinimumHeight\}px`/);
  assert.match(pageSource, /\.ranking-page \{[^}]*overflow-anchor: none;/);
});

test("distinguishes no scan, first-page screening, and located-but-unadopted queries", () => {
  assert.match(pageSource, /if \(item\.pages_scanned <= 0\) return "未进入扫描范围"/);
  assert.match(pageSource, /return "仅完成首页筛选 未进入后续定位"/);
  assert.match(pageSource, /已定位，但该搜索词未进入推荐词/);
  assert.doesNotMatch(pageSource, />已拦截</);
});

test("uses the current required search-ranking response contract", () => {
  assert.doesNotMatch(statusTypeSource, /opportunity_first_page_threshold/);
  assert.match(statusTypeSource, /opportunity_max_direct_competitors: number;/);
  assert.match(statusTypeSource, /opportunity_max_organic_rank: number;/);
  assert.match(summaryTypeSource, /vision_stage_completed: boolean;/);
  assert.match(summaryTypeSource, /\n  usage: \{/);
  assert.match(summaryTypeSource, /estimated_cost_cny: number \| null;/);
  assert.doesNotMatch(
    summaryTypeSource,
    /(vision_stage_completed|usage|estimated_cost_cny)\?:/,
  );
});

test("allows per-provider usage and cost evidence on model attempts", () => {
  assert.match(
    analysisTypeSource,
    /provider_attempts\?: Array<\{[\s\S]*?usage\?: \{[\s\S]*?input_tokens\?: number;[\s\S]*?output_tokens\?: number;[\s\S]*?total_tokens\?: number;[\s\S]*?estimated_cost_cny\?: number \| null;[\s\S]*?\}>;/,
  );
});

test("separates unusable billed attempts from reusable model results", () => {
  assert.match(pageSource, /const latestAttemptHasUnusableSpend = computed/);
  assert.match(pageSource, /v-else-if="latestAttemptHasUnusableSpend"/);
  assert.match(
    pageSource,
    /模型请求已产生[\s\S]*但识别结构不可用，重试不能复用本次结果。/,
  );
});

test("attributes ranking validation to the previously adopted strategy", () => {
  assert.match(analysisTypeSource, /matched_suggestion\?: string;/);
  assert.match(pageSource, /修改后复采归属/);
  assert.match(pageSource, /上一轮实际采用：/);
  assert.match(pageSource, /不代表本轮三张候选卡已经采用/);
});

test("keeps one manual product action and adds a confirmed all-store serial batch", () => {
  assert.equal(pageSource.match(/@click="runAnalysis"/g)?.length, 1);
  assert.match(pageSource, /图文融合搜索定位/);
  assert.match(pageSource, /当前商品族只走一次链路/);
  assert.match(pageSource, /全部授权店铺一键串行分析/);
  assert.match(pageSource, /确认费用并一键启动/);
  assert.match(pageSource, /全局最大并发为 1/);
  assert.match(apiSource, /\/api\/erp\/search-ranking\/batch\/start/);
  assert.match(apiSource, /\/api\/erp\/search-ranking\/batch\/restart/);
  assert.match(typesSource, /max_concurrency: 1;/);
  assert.match(typesSource, /automatic_retry: false;/);
  assert.match(pageSource, /从头重新开始/);
  assert.match(pageSource, /继续未完成任务/);
  assert.match(pageSource, /同一店铺内相同 PLID 的变体合并为一个商品族/);
  assert.match(pageSource, /旧检查点已合并/);
  assert.match(pageSource, /2_500/);
  assert.match(pageSource, /familyRepresentative.offer_id/);
  assert.match(pageSource, /颜色、尺寸、容量等差异值始终留在各自 Offer/);
  assert.match(pageSource, /代表图不自动验证这些值/);
  assert.match(pageSource, /variant_projection/);
  assert.match(typesSource, /variant_parameters_visually_verified: false;/);
});

test("declares request pacing and shopper-journey evidence", () => {
  assert.match(statusTypeSource, /root_expansion_input_limit: number;/);
  assert.match(statusTypeSource, /root_expansion_followup_root_limit: number;/);
  assert.match(statusTypeSource, /root_expansion_phrase_roots_enabled: true;/);
  assert.match(statusTypeSource, /root_expansion_raw_suggestions_are_selected: false;/);
  assert.match(statusTypeSource, /root_source_priority: SearchRankingRootSource\[\];/);
  assert.match(statusTypeSource, /model_market_context: "South Africa";/);
  assert.match(statusTypeSource, /model_language_variant: "South African English";/);
  assert.match(
    statusTypeSource,
    /model_shopper_context: "South African local customer habits";/,
  );
  assert.match(statusTypeSource, /model_localization_scope: "all_model_generated_text_fields";/);
  assert.match(statusTypeSource, /model_localization_is_measured_demand: false;/);
  assert.match(analysisTypeSource, /model_localization\?: \{/);
  assert.match(analysisTypeSource, /market_context\?: "South Africa";/);
  assert.match(analysisTypeSource, /language_variant\?: "South African English";/);
  assert.match(statusTypeSource, /search_query_attempt_limit: number;/);
  assert.match(statusTypeSource, /public_request_min_interval_seconds: number;/);
  assert.match(statusTypeSource, /public_request_jitter_seconds: number;/);
  assert.match(
    statusTypeSource,
    /public_request_retry_policy: "no_automatic_retry_for_search_endpoints";/,
  );
  assert.match(
    statusTypeSource,
    /operation_scope: "manual_single_offer_or_confirmed_serial_batch";/,
  );
  assert.match(analysisTypeSource, /autocomplete_checks\?: Array/);
  assert.match(analysisTypeSource, /shopper_journey\?: \{/);
  assert.match(pageSource, /所有根词扩展和搜索页公开请求共用间隔/);
  assert.match(pageSource, /均按 3–5 秒严格串行/);
  assert.match(pageSource, /不自动重试/);
  assert.match(pageSource, /human_confirmed_product_fact: "人工确认商品事实"/);
  assert.match(pageSource, /title_cross_check: "图题交叉验证词"/);
  assert.match(pageSource, /image_title_first_instinct: "图文融合模型预测"/);
  assert.match(pageSource, /title_word_root: "主标题确定性拆词"/);
  assert.match(pageSource, /image_title_need_state: "相邻需求模型词根"/);
  assert.match(pageSource, /result_page_learning: "搜索结果页反向学习"/);
  assert.match(pageSource, /index \+ 1/);
  assert.match(pageSource, /主标题根词保留最低覆盖/);
  assert.match(pageSource, /第4级需先取得真实搜索结果页/);
  assert.match(pageSource, /model_market_context/);
  assert.match(pageSource, /model_language_variant/);
  assert.match(pageSource, /本地客户语境/);
  assert.match(pageSource, /视觉事实只取自图片/);
  assert.match(pageSource, /地域语境不补造不可见事实/);
  assert.match(pageSource, /地域语境不代表平台实测搜索量/);
  assert.match(pageSource, /原始返回不直接入选/);
  assert.match(pageSource, /仅保留同品身份或结构化相邻商品族/);
  assert.match(pageSource, /可继续作为词组词根/);
  assert.match(
    typesSource,
    /"human_confirmed_product_fact"[\s\S]*"image_title_first_instinct"[\s\S]*"title_word_root"[\s\S]*"result_page_learning"[\s\S]*"image_title_need_state"[\s\S]*"title_cross_check"/,
  );
});

test("keeps the search method overview compact while preserving operating boundaries", () => {
  const methodBannerSource = pageSource.slice(
    pageSource.indexOf('<section class="method-banner">'),
    pageSource.indexOf('<p v-if="eligibility"'),
  );

  assert.match(methodBannerSource, /IMAGE → TITLE → PLATFORM/);
  assert.match(methodBannerSource, /主图独立识别，标题参与融合/);
  assert.match(methodBannerSource, /class="method-model-route"/);
  assert.match(methodBannerSource, /class="method-guardrail-grid"/);
  assert.match(methodBannerSource, /<details class="method-details">/);
  assert.doesNotMatch(methodBannerSource, /<details class="method-details"[^>]*\sopen/);
  assert.match(methodBannerSource, /扫描边界/);
  assert.match(methodBannerSource, /探索预算/);
  assert.match(methodBannerSource, /请求节流/);
  assert.match(methodBannerSource, /词根与平台扩展规则/);
  assert.doesNotMatch(methodBannerSource, /ISOLATED CROSS-CHECK/);
  assert.doesNotMatch(methodBannerSource, /每个搜索词最多扫描/);
  assert.doesNotMatch(methodBannerSource, /单品探索上限/);
  assert.doesNotMatch(methodBannerSource, /模型预测地域/);
  assert.doesNotMatch(methodBannerSource, /自然排名坐标/);
});

test("separates platform root expansions from South African model-direct queries", () => {
  assert.match(statusTypeSource, /model_south_african_direct: number;/);
  assert.match(statusTypeSource, /takealot_root_expansion: number;/);
  assert.match(typesSource, /query_source_channel\?:/);
  assert.match(pageSource, /平台根词扩展词/);
  assert.match(pageSource, /图文融合·南非完整搜索词/);
  assert.match(pageSource, /平台根词扩展 \+ 南非模型/);
  assert.match(pageSource, /同词可同时属于多类/);
  assert.match(pageSource, /含图文融合南非完整搜索词/);
});

test("shows phrase roots and the per-product platform expansion relevance gate", () => {
  assert.match(analysisTypeSource, /relevance_status\?: "eligible" \| "rejected_irrelevant";/);
  assert.match(analysisTypeSource, /relation\?: "same_product" \| "adjacent_demand" \| "irrelevant";/);
  assert.match(analysisTypeSource, /used_as_followup_root\?: boolean;/);
  assert.match(analysisTypeSource, /root\?: string;/);
  assert.match(analysisTypeSource, /seed\?: string;/);
  assert.match(pageSource, /rootExpansionCheckLabel\(check\)/);
  assert.match(pageSource, /rootExpansionCheckIsPhrase\(check\)/);
  assert.doesNotMatch(pageSource, /check\.root\.trim\(\)/);
  assert.match(pageSource, /本商品词根\/词组与平台扩展筛选/);
  assert.match(pageSource, /只有保留本商品身份，或命中结构化相邻需求所列替代商品族的扩展，才可进入搜索/);
  assert.match(pageSource, /已作为完整词组词根继续观察下一层平台扩展/);
  assert.match(pageSource, /平台原始词根\/词组扩展库/);
  assert.match(pageSource, /本区展示平台原始返回，不代表为当前商品入选/);
  assert.match(typesSource, /phrase_roots_supported: true;/);
  assert.match(typesSource, /raw_expansions_require_product_context_selection: true;/);
});

test("uses the expanded fusion query budget with one conditional recovery query", () => {
  assert.match(statusTypeSource, /adaptive_recovery: number;/);
  assert.match(analysisTypeSource, /valid_platform_root_target\?: number;/);
  assert.match(analysisTypeSource, /adaptive_recovery_used\?: boolean;/);
  assert.match(pageSource, /search_query_attempt_limit \?\? 14/);
  assert.match(pageSource, /model_south_african_direct \?\? 6/);
  assert.match(pageSource, /自适应补救词/);
});

test("shows isolated cross-validation warnings and optional manual facts", () => {
  assert.match(analysisTypeSource, /confirmed_fact_resolved_title_conflict\?: boolean;/);
  assert.match(analysisTypeSource, /identity_deviation_branch\?:/);
  assert.match(analysisTypeSource, /matched_identity_anchors: string\[\];/);
  assert.match(analysisTypeSource, /identity_difference_level\?: "aligned" \| "moderate" \| "high";/);
  assert.match(analysisTypeSource, /manual_fact_required\?: boolean;/);
  assert.match(pageSource, /交叉验证结果单独保存/);
  assert.match(pageSource, /批次已跳过且不会自动重试/);
  assert.match(pageSource, /但不强制补录/);
  assert.doesNotMatch(pageSource, /形态修饰词重合不算身份支持/);
});

test("keeps S A and merged C I metrics without repeating verbose evidence in every card", () => {
  assert.match(typesSource, /semantic_relation_grade\?: "S" \| "A" \| "C\/I";/);
  assert.match(typesSource, /semantic_relation_source_priority_decides_grade\?: false;/);
  assert.match(typesSource, /semantic_relation_alternative_product_terms\?: string\[\];/);
  assert.match(typesSource, /semantic_alias_token_subset_with_retarget_rejection/);
  assert.match(statusTypeSource, /semantic_relation_grades: \["S", "A", "C\/I"\];/);
  assert.doesNotMatch(pageSource, /candidate_rationale \|\| item\.validation_evidence\.reason/);
  assert.doesNotMatch(pageSource, /完整第一页自然商品中/);
  assert.doesNotMatch(pageSource, /S\/A判级与词根来源优先级彼此独立/);
  assert.doesNotMatch(pageSource, /边界案例合并到C\/I级而不猜测/);
  assert.match(pageSource, /S级 \{\{ semanticGradeCounts\.S \}\} 个/);
  assert.match(pageSource, /A · 同一任务的替代商品/);
  assert.match(typesSource, /page_validation_status\?: "completed" \| "not_run";/);
  assert.match(pageSource, /function hasFirstPageValidation/);
  assert.match(pageSource, /没有首页同类率；“未验证”不能解释为 0%/);
});

test("keeps semantic image-title evidence separate before fusion", () => {
  assert.match(analysisTypeSource, /title_identity_support\?: boolean;/);
  assert.match(analysisTypeSource, /cross_validation_isolated\?: true;/);
  assert.match(analysisTypeSource, /fusion_stage_received_source_title\?: true;/);
  assert.match(pageSource, /隔离图片观察/);
  assert.match(pageSource, /独立交叉验证/);
  assert.match(pageSource, /图文融合生成/);
});

test("exposes difference and score selectors with evidence-based title details", () => {
  assert.match(pageSource, /identityDifferenceFilter/);
  assert.match(pageSource, /titleScoreFilter/);
  assert.match(pageSource, /交叉验证/);
  assert.match(pageSource, /标题评分/);
  assert.match(pageSource, /现有主标题质量评分/);
  assert.match(pageSource, /自然排名、首页同类占比、竞争数、价格、库存、广告位和平台扩展顺序均不计分/);
  assert.match(pageSource, /旧版记录按新版标题质量规则进行的本地换算/);
  assert.match(pageSource, /证据缺失·不计分/);
  assert.match(typesSource, /export interface SearchRankingTitleScoreComponent/);
  assert.match(typesSource, /scoring_version: "evidence-title-v2";/);
  assert.match(typesSource, /title_quality_only: true;/);
  assert.match(typesSource, /non_scoring_signals: Array/);
  assert.match(typesSource, /title_score_value\?: number \| null;/);
});

test("removes the reverse-search path from the operator contract", () => {
  assert.match(statusTypeSource, /product_fact_confirmation_mode: "manual_only";/);
  assert.match(analysisTypeSource, /product_fact_recommendation: SearchRankingProductFactRecommendation;/);
  assert.doesNotMatch(pageSource, /openReverseSearchConfirmation/);
  assert.doesNotMatch(pageSource, /confirmReverseSearch/);
  assert.match(pageSource, /不调用倒搜/);
  assert.doesNotMatch(apiSource, /reverse-image-search/);
  assert.doesNotMatch(apiSource, /confirmSearchRankingReverseImageSearch/);
  assert.doesNotMatch(typesSource, /SearchRankingReverseImageSearch/);
});

test("ordinary one-click analysis and manual facts are the only product-fact paths", () => {
  assert.match(
    pageSource,
    /analyzeSearchRanking\(\s*familyRepresentative\.offer_id,\s*familyRepresentative\.store_code,/,
  );
  assert.match(pageSource, /人工确认商品事实/);
  assert.match(pageSource, /\{\{ factRecommendation\.reason \}\}/);
  assert.match(pageSource, /acknowledged_fact_accuracy: true/);
  assert.match(pageSource, /acknowledged_ranking_revalidation: true/);
  assert.match(pageSource, /商品事实档案/);
  assert.match(pageSource, /停用并保留档案/);
  assert.match(apiSource, /\/product-facts\/confirm/);
  assert.match(apiSource, /\/product-facts\/\$\{factId\}\/revoke/);
});

test("manual facts stay operator-available and report progress or errors inside the modal", () => {
  assert.match(pageSource, /const factModalError = ref\(""\)/);
  assert.match(pageSource, /系统建议与人工确认边界/);
  assert.match(pageSource, /class="fact-modal-error" role="alert"/);
  assert.match(pageSource, /正在保存并重新验证…/);
  assert.match(pageSource, /closeProductFactConfirmation/);
  assert.doesNotMatch(
    pageSource,
    /当前分析没有需要人工补证的商品事实缺口，请先重新分析/,
  );
});

test("documents the shared lazy root-expansion cache and ranked expansion evidence", () => {
  assert.match(statusTypeSource, /autocomplete_cache_shared_across_stores: true;/);
  assert.match(statusTypeSource, /autocomplete_cache_ttl_hours: number;/);
  assert.match(
    statusTypeSource,
    /autocomplete_cache_refresh_mode: "refresh_on_first_hit_after_ttl";/,
  );
  assert.match(pageSource, /缓存采集满/);
  assert.match(pageSource, /不会定时刷新/);
  assert.match(pageSource, /第 \{\{ expansion\.rank \}\} 项/);
  assert.match(pageSource, /不是买家搜索量/);
  assert.match(apiSource, /\/root-expansion-library/);
});
