# CodeGraph：AI 项目理解指南

本项目已经初始化 CodeGraph。本指南用于让接手项目的 AI 先取得结构化代码上下文，再进入精确文件阅读、修改和验证。

CodeGraph 是辅助导航和影响分析工具，不是业务规则来源，也不替代测试、真实数据库核对、浏览器验收或正式 ERP 健康检查。业务口径和安全边界仍以根目录 `AGENT.md` 为准。

## 1. 当前索引范围

初始索引使用 CodeGraph 1.5.0 建立，识别：

- Python 后端、测试和脚本；
- Vue 单文件组件及 TypeScript；
- FastAPI 路由、函数、类、组件、调用和引用关系；
- YAML 配置文件。

`frontend/competitor/dist/` 是可重建生产构建产物，已通过 `codegraph.json` 排除。`.env`、MySQL 数据、日志、备份、导出和运行缓存继续由 `.gitignore` 排除，不应进入代码图谱。

索引数据库位于 `.codegraph/codegraph.db`，只保存在本机；`.codegraph/.gitignore` 保证数据库、日志和守护进程文件不提交到 Git。

## 2. AI 的推荐工作顺序

1. 完整阅读根目录 `AGENT.md`、`README.md` 和 `docs/PROJECT_STATUS.md`。
2. 对架构、调用链、业务流程或改动影响问题，先调用一次 `codegraph_explore`，查询中同时写出功能名称、已知符号和入口文件。
3. 根据图谱返回的源码和关系，只精读真正需要修改或核验的文件。
4. 修改前检查上游调用者、下游依赖和相关测试；不能只看单个函数。
5. 修改后运行与风险相匹配的项目验证。CodeGraph 的“受影响测试”建议只用于确定起点，不能自动证明回归完整。
6. 执行 `codegraph status`，确认索引为 `up to date`；正常情况下 MCP 文件监视器会自动同步。

如果当前 Codex 会话尚未显示 CodeGraph MCP 工具，需要重启 Codex。CLI 仍可直接使用。

## 3. 最有价值的查询

### 运营日报读取链路

```powershell
codegraph explore "fetchDailyReport /api/erp/daily-report daily_report_payload DailyReportPage"
```

目标是同时取得：

- `frontend/competitor/src/pages/DailyReportPage.vue`
- `frontend/competitor/src/api.ts`
- `src/takealot_ops/erp/web.py`
- `src/takealot_ops/erp/daily_report.py`
- `tests/unit/test_daily_report.py`
- `tests/unit/test_erp_web.py`

### 运营日报人工确认与库存连续性

```powershell
codegraph explore "confirm_entry eliminate_stock_alert reopen_stock_alert stock continuity pending actions"
codegraph impact confirm_entry
```

重点检查确认值如何写入、如何向相邻日报日传播、何时恢复待办、页面按钮和接口权限是否一致。

### 全量刷新和跨用户冷却

```powershell
codegraph explore "refreshStoreData /api/erp/refresh RefreshCoordinator cooldown daily-run"
codegraph impact RefreshCoordinator
```

重点检查前端状态、FastAPI 路由、MySQL 持久冷却、管理员豁免和失败不启动冷却。

### 竞品批次、断点和自动续爬

```powershell
codegraph explore "collectCompetitor CollectionBatchRegistry CollectionRequestCoordinator batch status auto resume checkpoint"
codegraph impact CollectionBatchRegistry
```

重点检查浏览器标签页身份、服务端单批次互斥、请求编号复用、人工停止清理和10分钟自动续爬。

### 商品缩略图安全链路

```powershell
codegraph explore "product thumbnail image_url allowed hosts 192 640 productImages"
```

重点检查官方域名白名单、禁止重定向、源文件限制、缓存尺寸、前端统一入口和“暂无图片”回退。

### 权限与会话

```powershell
codegraph explore "permissions auth session CSRF require_permission seven day sliding expiry"
```

重点检查前端可见反馈与后端真实授权是否同时存在，以及账号变更后会话是否撤销。

### 指标和业务口径传播

```powershell
codegraph explore "page_views_30_days missing_capture needs_review SAST Asia/Shanghai"
codegraph impact classify_quadrants
```

重点检查数据库值、API 投影、Vue 文案、Excel/HTML 输出和测试是否使用相同口径。

## 4. 常用 CLI

```powershell
# 查看图谱健康和统计
codegraph status

# 按名称或代码词搜索
codegraph query "daily_report_payload"

# 返回功能相关源码、调用路径和影响摘要
codegraph explore "运营日报确认合并的完整链路"

# 查看符号调用者、被调用者和影响范围
codegraph callers confirm_entry
codegraph callees confirm_entry
codegraph impact confirm_entry

# 根据本次变更建议相关测试
git diff --name-only | codegraph affected --stdin

# 文件监视器未运行时手动增量同步
codegraph sync
```

## 5. 关键边界

- 图谱是静态分析结果。Playwright 页面行为、Vue 运行时状态、字符串拼接 URL、SQL 数据内容和系统代理故障仍需实际验证。
- CodeGraph 返回的影响范围可能包含误报，也可能遗漏反射、动态分派或运行时生成关系；高风险修改仍应运行完整回归。
- 不得因为图谱显示“无调用者”就直接删除代码；还要检查计划任务、CLI、配置、模板、外部入口和历史兼容要求。
- 不得把 CodeGraph 的关系摘要当作业务规则。涉及时间、库存、销量、缺失值、权限或竞品库存时，必须回到 `AGENT.md` 和对应测试确认。
- 完成并验证运行代码修复后，仍按项目规则运行 `scripts/restart_erp.ps1` 并检查 `/api/health`。
