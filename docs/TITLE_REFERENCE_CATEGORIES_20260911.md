# 标题竞品类目与搜索页内位置

2026-09-11，GREEN已发布；共享功能已范围同步到双BLUE。

## 功能

- 竞品卡片、对比弹窗直接显示完整平台类目层级。弹窗保留来源和北京时间；不从标题猜测类目。
- 缺失类目通过“补充缺失类目”独立读取：先用授权本地竞品档案，最多请求当前分析的10个已验证完整商品链接。普通GET不访问平台，操作沿用搜索定位权限、CSRF和运行锁。
- 补充结果存于原分析的独立JSON证据中，不重跑模型、不新增监测目标、不改变原关键词、排名、Token或标题复核指纹。失败不会覆盖另一节点已保存的成功结果。
- 排名显示“第几页 · 第几个”，采用实际page_number/page_rank。首页竞品来自已保存的首页自然结果；自有商品使用该关键词实际页码和页内序号，不按固定36条反推。自然位、采集时间、未扫描/未定位、同组参考身份保持可查。
- 类目补充期间保留当前对比弹窗，结果回来后更新原弹窗；切换商品或店铺不会套用旧请求结果。

## 实际数据

正式店铺current、Offer241694092、分析89的10个参考商品都有完整类目：4项原搜索记录、2项本地竞品档案、4项本次公开商品接口读取。

截图PLID97221731：`Pets → Equipment & Accessories → Beds & Blankets`，2026-09-11北京时间17:31补充。搜索词`cat foldable tier villa`的历史位置为竞品第1页第27个、自有商品第1页第1个，保留2026-09-09原搜索时间。类目与排名不是同一次采集。

真实GET与受CSRF保护的类目POST在正式本机、公网验证通过；更新前后关键词、usage和review_fingerprint完全相同。缺少CSRF的POST返回403。

## 验证与发布边界

- 后端相关回归211项通过，包含保存分类、实际分页、越权入口、失败、重复读取、过期分析和并发成功保护；Ruff及4个源文件Mypy通过。
- 前端467项覆盖通过：首次从仓库根运行时，466项成功、1项Vite测试因工作目录错误失败；该项在frontend/competitor正确目录复测通过，保留首次日志。类型检查与隔离构建通过。
- 10项模拟Vue渲染/事件检查覆盖卡片全类目、实际位置、加载状态、同一弹窗更新、焦点及无操作权限；并检查GREEN/BLUE编译后API的默认标题操作和类目操作。没有浏览器实点验收。
- GREEN与BLUE分别从当前部署图生成范围包，只修改标题模块、对应CSS和API函数，其他模块只改资源引用。保留其他任务刚发布的竞品匹配；BLUE未覆盖GREEN独有的雷达利润及容量改动。
- GREEN发布图28文件，BLUE29文件；六入口共171项文件HTTP/MIME/SHA检查和健康检查通过。双BLUE范围包33文件，部署后源文件一致，保留旧资源和逐文件备份。
- GREEN等待active_item为空的持久断点后使用scripts/restart_erp.ps1正式重载。原batch scheduled-20260911-30ea3b9ad1ab、revision45、round18、目标和558条结果完整保留；稍后批次与队列自然推进到566。
- 双BLUE按原SYSTEM服务安装流程在控制器、worker空闲且交付已清空时重载。仍为101单写，不启用数据库接管，不修改节点配置、数据库拓扑或原运行状态。
- BLUE真实标题业务验证受既有数据有效期限制：current店铺418个Offer全部快照过期，最后快照2026-09-09T10:07:56 UTC，36小时门槛内无可选商品。正式Offer在BLUE返回404属商品资格限制；没有为验收触发额外全店刷新。BLUE业务操作不可宣称已验证。

## 证据

本机目录：`logs/title-reference-category-rank-20260911/`。

主要文件：`supplement.before.json`、`supplement.after.json`、`green-business.json`、`blue-business-limitation.json`、`backend-regression.log`、`frontend-all.log`、`frontend-cwd-recheck.log`、`component-check.log`、`prepare-summary.json`、`http-summary.json`、`green-continuity.json`、`final-continuity.json`。

持久技能已同步“全类目可见、独立补充、使用实际页码与页内位置、不改历史标题分析”规则：`C:/Users/Mayn/.codex/skills/takealot-erp-verified-release/SKILL.md`。
