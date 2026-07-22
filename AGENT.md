# Takealot 店铺运营数据工具：长期维护档案

最后更新：2026-07-22
项目根目录：`D:\南非店铺数据抓取`
当前维护方式：单 Agent、本机 Windows/PowerShell

## 1. 文件职责

本文件是项目的长期交接与修改记录。任何接手本项目的人员或 Agent，都必须先阅读本文件、`README.md` 和 `docs/PROJECT_STATUS.md`，再开始分析或修改。

## 2. 强制同步更新规则

只要本项目发生有意的项目修改，就必须在同一个任务中同步更新本文件，不允许把更新留到以后。

项目修改包括但不限于：

- 源码、测试、配置、数据库结构或迁移；
- PowerShell、批处理及其他运行脚本；
- 依赖、构建、打包或计划任务配置；
- README、需求、设计、审计、测试报告等项目文档；
- 业务规则、指标口径、安全边界或操作流程。

同步更新至少应完成：

1. 修改顶部“最后更新”日期；
2. 在“修改日志”中新增一条记录；
3. 若项目状态、约束、命令、风险或后续事项发生变化，更新对应章节；
4. 在结束任务前检查 `git diff`，确认项目修改与本文件更新同时存在；
5. 记录实际执行的验证；没有执行的验证必须明确写为“未执行”，不得写成已通过。

数据库采集结果、日志、缓存、备份和日报等正常运行时产物，如果没有被有意纳入版本管理，不单独触发本文件更新；但生成逻辑、格式、目录规则或业务口径发生变化时必须更新。

## 3. 项目目标

这是一个在 Windows 本机运行的 Takealot 店铺运营数据工具，负责：

- 通过官方 Seller API 只读采集 Offer 与 Sales 数据；
- 使用 SQLite 保存历史快照和指标；
- 生成 HTML、Excel、PNG 日报；
- 提供本机 Streamlit 运营看板；
- 更新 NFT102 访客表并生成可追溯核对报告。

## 4. 不可破坏的业务与安全约束

- API 客户端只允许读取操作，不得擅自增加写入、修改或删除店铺数据的请求。
- 真实 API Key 只能放在项目根目录 `.env` 或受控环境变量中；不得进入源码、测试夹具、日志、报表、文档或版本库。
- 日期边界使用南非标准时间（SAST）业务日；不得用本机日期直接替代既定转换。
- `page_views_30_days` 只能称为“近30天浏览量”，不是独立访客数，也不是精确当天流量。
- 相邻30天窗口差值只能称为“30天浏览量窗口净变化”；对于已有历史的商品，不能据此还原精确当天浏览量。
- 只有从全新商品上架第一天开始、并确认此前浏览量为零时，才可能递推每日浏览量；即使如此也仍是浏览量（PV），不是访客数（UV）。
- 缺失流量必须保持缺失，不得补零或伪造。
- Offer 分页必须完整获取后再原子发布快照，任何分页失败都不能发布不完整数据。
- 看板默认只绑定 `127.0.0.1`，不得在没有明确需求和安全设计的情况下扩大监听范围。
- HTML、Excel、PNG 与看板指标必须来自同一指标数据集，避免跨输出口径漂移。
- 项目生成的 Excel 中，所有有内容的单元格必须水平、垂直居中；NFT102 更新副本只对目标工作表应用此规则，其他店铺工作表保持原样。
- 不覆盖用户源文件；NFT102 更新必须输出新副本及核对报告。

## 5. 关键目录

- `src/takealot_ops/`：业务源码
- `tests/`：单元、集成和端到端测试
- `config/`：业务规则与程序设置
- `scripts/`：计划任务和 NFT102 辅助脚本
- `docs/`：需求、实施、审计、测试和项目状态文档
- `data/`：本机 SQLite 数据
- `exports/`、`outputs/`：生成结果
- `backups/`、`logs/`：运行备份与日志

## 6. 常用验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m takealot_ops.cli verify
```

验证范围应与修改风险匹配；修改指标、导出、数据库或采集逻辑时，不能只运行无关的单个测试。

## 7. 当前状态与已知边界

- 数据接口、SQLite 存储、指标计算、异常识别、HTML/Excel/PNG 导出、本地看板和日常运行工具已经完成。
- 合成数据验收已完成；真实店铺只读联调状态以 `docs/PROJECT_STATUS.md` 的最新记录为准。
- 当前官方 Offer 数据提供近30天滚动浏览量，没有可确认的精确每日产品访客数接口。
- 当前单机、单店、单运营人员场景继续使用 SQLite；出现多人并发、远程部署或明显规模增长后再评估迁移。

## 8. 修改日志

| 日期 | 修改摘要 | 主要文件 | 验证与结论 |
|---|---|---|---|
| 2026-07-22 | 统一 Excel 输出对齐规则：运营日报全部有内容单元格水平、垂直居中；NFT102 仅居中目标工作表并保持其他店铺工作表不变 | `src/takealot_ops/exports/excel.py`、`scripts/write_nft102_workbook.py`、`tests/integration/test_excel_export.py`、`tests/unit/test_nft102.py` | 完整测试 `138 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；9 个日报工作表完成样式与视觉检查，公式错误扫描为 0 |
| 2026-07-22 | 补齐 Excel 异常商品表的中文导出映射；`high_views_low_conversion`、`suspected_stockout`、`stale_offer_snapshot` 的异常类型和说明不再回退为英文 | `src/takealot_ops/exports/excel.py`、`tests/integration/test_excel_export.py` | 完整测试 `137 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；修正版 Excel 的未翻译异常字段扫描为 0，公式错误扫描为 0 |
| 2026-07-22 | 建立项目长期维护档案及 Codex 自动入口；确立任何项目修改必须同步更新 `AGENT.md` 的规则 | `AGENT.md`、`AGENTS.md` | 文档规则检查；未运行代码测试（本次未修改运行代码） |
