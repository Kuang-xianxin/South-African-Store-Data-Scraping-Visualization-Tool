# Takealot 店铺运营数据工具：长期维护档案

最后更新：2026-07-24
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
- 使用本机 MySQL 8.0 保存全部店铺、指标和竞品历史；
- 生成 HTML、Excel、PNG 日报；
- 提供统一的 Vue 3 + TypeScript 小型 ERP，覆盖经营总览、商品、四象限、风险/质量、竞品、报表和用户权限；
- 保留 Streamlit 旧版看板作为临时兼容回退，不作为正式功能入口；
- 更新 NFT102 访客表并生成可追溯核对报告。

## 4. 不可破坏的业务与安全约束

- API 客户端只允许读取操作，不得擅自增加写入、修改或删除店铺数据的请求。
- 真实 API Key 只能放在项目根目录 `.env` 或受控环境变量中；不得进入源码、测试夹具、日志、报表、文档或版本库。
- 日期边界使用南非标准时间（SAST）业务日；不得用本机日期直接替代既定转换。
- ERP 中“最近采集”和竞品历史快照属于记录时刻，必须固定按北京时间（Asia/Shanghai）显示；指标日期仍按 SAST 业务日，两种口径不得混淆。
- `page_views_30_days` 只能称为“近30天浏览量”，不是独立访客数，也不是精确当天流量。
- 经营四象限以最新近30天浏览量和截至最新可用指标日的近7个自然日下单件数分类；相对排名只用于图上拉开差异，悬停和明细必须展示真实值。销量为0必须留在低销量侧，缺失指标必须保持未分类。
- 经营四象限的十字分界线和中心交点必须使用当前25/50/75分位分类接口返回的真实排名分界同步移动，不得固定在50%位置造成图形与分类口径不一致。
- 经营四象限散点必须显式使用固定高对比填充色，不得继承按钮默认颜色导致近黑色，其中“待优化”固定为亮金色；散点必须使用无原生等待的自定义悬浮信息卡识别商品，并支持点击复制平台 SKU；复制成功、SKU 缺失或复制失败都必须给出中文反馈。
- 经营四象限必须固定标出横轴“近30天浏览量相对排名”和纵轴“近7日下单件数相对排名”；悬浮信息卡必须展示平台可售库存、指标截止日期、首次上架/最早记录、最近补货时间和平台明确返回的近30天浏览量；信息卡不得被图框裁切，字段标题与数值必须保持清晰可读。
- Offers 接口的 `created_at` 是首次上架的优先来源；旧快照缺失时只能标为“本库最早记录”。同一商品后一条平台库存快照高于前一条有效快照时，后一条快照的实际采集时间就是最近补货时间，并记录库存增加数量；该记录不得称为“估算”，没有观察到库存增加时只能显示暂无记录。补货快照采集时间固定按北京时间显示。Offers 接口没有独立近7日流量字段时，不得用近30天浏览量按比例推算或展示近7日流量，也不得把浏览量称为访客数。
- 下单件数图的每日实际值和坐标刻度必须使用整数；不得为了平滑趋势绘制或伪装成实际件数的小数值。
- 相邻30天窗口差值只能称为“30天浏览量窗口净变化”；对于已有历史的商品，不能据此还原精确当天浏览量。
- 只有从全新商品上架第一天开始、并确认此前浏览量为零时，才可能递推每日浏览量；即使如此也仍是浏览量（PV），不是访客数（UV）。
- 缺失流量必须保持缺失，不得补零或伪造。
- Offer 分页必须完整获取后再原子发布快照，任何分页失败都不能发布不完整数据。
- 正式 ERP 默认绑定 `0.0.0.0:8501` 供同一受信任局域网访问；MySQL 仍只允许连接本机 `127.0.0.1`，不得开放数据库端口。公网访问必须另行增加 HTTPS、反向代理和网络访问控制，不得直接暴露 Uvicorn。
- ERP 所有业务接口必须要求登录并在后端执行角色校验：`viewer` 只读和下载，`operator` 可刷新、采集和生成，`admin` 额外管理用户。首个管理员只能从服务器本机 `127.0.0.1` 初始化；密码必须使用带随机盐的 scrypt 哈希保存，浏览器会话必须为可撤销的 HttpOnly、SameSite=Strict Cookie，所有写操作必须校验 CSRF。不得只依赖前端隐藏按钮实现权限。
- ERP 浏览、筛选和页面切换必须使用 MySQL 数据库级只读事务；只有 `operator`、`admin` 点击“刷新全部数据”或 Windows 每日任务运行时，才允许调用既有只读 API 采集和完整 `daily-run` 流程。
- 导出中心允许基于当前只读数据集写入 `exports/YYYY-MM-DD/`，但不得触发平台接口或数据库写入；一键导出必须同时尝试 HTML、Excel、PNG，并对已存在文件提供页面下载入口。
- 异常商品 KPI 必须统计最新可用指标日的去重商品数；异常明细允许同一商品因多种异常保留多行。看板默认显示最新指标日并可手动切换全部历史，Excel、HTML、PNG 默认不得混入历史异常日期。
- 运营日报自动更新默认使用中国时间10:05早间完整采集、18:00晚间完整采集和18:30待办快照/自动导出；三个任务必须忽略并发重复实例，并在错过计划时间后尽快补跑。
- 默认不增加09:50正式采集：Sales接口按SAST日期范围查询且每日重拉最近7日，10点后仍可补回前一日订单；只有连续运行证据证明平台删除历史时，才允许新增不参与最终值的原始保险快照。
- 运营日报的早间、晚间、人工候选和最终确认值必须分开持久化，后一次采集不得覆盖前一次版本；人工修改、最终确认和取消库存红标必须记录用户、时间及备注。无差异商品可以批量确认，有差异商品必须选择具体来源后确认。
- 运营日报库存异常口径固定为“前一日已确认库存（未确认时使用最新系统版本）- 当天订单数 != 当天平台仓可售库存”；红标可由运营填写原因后取消，普通异常备注独立存在。存在任一未合并数据时必须阻止运营日报 Excel 导出并返回具体日期和商品。
- 看板的导航、按钮、上传提示、字段名、状态和说明必须使用中文；框架自带的英文工具栏应隐藏。平台原始商品名称、店铺代码、商品编码和南非货币符号属于真实业务数据，保持原值，不得擅自翻译或伪造。
- HTML、Excel、PNG 与看板指标必须来自同一指标数据集，避免跨输出口径漂移。
- 项目生成的 Excel 中，所有有内容的单元格必须水平、垂直居中；NFT102 更新副本只对目标工作表应用此规则，其他店铺工作表保持原样。
- 不覆盖用户源文件；NFT102 更新必须输出新副本及核对报告。
- NFT102 网页续写必须以用户本次明确上传的运营最终版为基准，原样归档后再生成；不得自动改用其他模板，也不得覆盖归档基准。
- 竞品采集只允许通过 Playwright 驱动本机 Chrome/Edge 访问公开商品/评论地址和隔离匿名购物车；平台拒绝普通 HTTP 客户端时可以用真实浏览器导航读取公开 JSON，但不得接入竞品卖家后台，不得进入结算或下单；每次库存探测结束后必须尽力清空测试购物车。
- 竞品库存探测只允许点击用户提供链接所对应 PLID 的主商品购买区按钮，严禁点击“You Might Also Like”“What’s Hot”等推荐位商品；进入购物车后必须再次按目标 PLID 定位商品，不得因为购物车中只有一个商品就猜测它是目标竞品。
- 竞品“库存”必须按尺寸、颜色、左右款等每个变体分别记录，标注为当前匿名会话、当前卖家和当前 SKU 的平台仓购物车可售上限，不得称为平台物理总库存；任何快捷数量或自定义数量校验一旦出现 `current stock = N` 明确仓库提示，必须立即把 `N` 作为精确平台仓库存并终止该变体的后续超量测试和二分校验；只有始终没有明确提示时才允许继续探测。异步加购必须等待购物车状态落库，数量菜单必须容忍关闭动画并重试；只有未标记 `aria-hidden=true` 的自定义数量输入框可视为已打开，临时更新错误必须在仍锁定同一 PLID 的前提下重新加载并有限重试，营销遮罩只允许定向清理阻挡目标控件的 Braze 元素。公开接口标记为 `is_leadtime` 的供应商调货/长时效到货变体及当前不可加入购物车的变体必须统一标记“没货”，不计有效平台仓库存，也不得参与库存净流出销量推断。
- 竞品评论按 PLID 商品维度共享，变体不得重复抓取或重复计数；只有前后快照的变体键、SKU 与卖家集合一致时，才允许比较汇总库存并产生库存净流出信号。
- 竞品累计销量仅可按明确展示的2%–5%假设评论率给出低可信度区间；观察期销量只使用可比库存净流出和新增评论，价格、排名或卖家变化不得直接换算为订单。
- 评论属于商品维度，可能跨卖家或变体；评论分类固定为4–5星好评、3星中评、1–2星差评。竞品评论区必须支持分类与起止日期组合筛选，并支持按日期新旧和评分高低排序；日期缺失记录仅在未限定时间时保留。
- 竞品页面和接口复用正式 ERP 的登录、角色和 `8501` 局域网访问边界；浏览历史使用 MySQL 只读事务，只有 `operator`、`admin` 明确点击采集按钮或运行 `collect-competitors` 时才允许访问公开接口和写入竞品表。
- 竞品采集任务开始后，ERP 页面导航不得销毁任务页面状态；切换到其他模块再返回时，必须保留链接输入、采集选项、完成数量、进度及逐条成功/失败结果。
- 竞品批量链接必须在发起采集前逐行校验 Takealot 域名和 PLID；发现错误时不得请求外部接口，必须自动聚焦并选中第一个错误链接、滚动到对应输入行，并用红色诊断条显示行号、具体原因和完整链接；用户修改输入后立即清除该错误状态。
- 正式运行数据库固定为本机 MySQL 8.0、`mysql+pymysql` 同步驱动和 `utf8mb4`；应用不得用 root 账号运行，不得自动回退读取 SQLite。旧 `data/takealot.db` 只允许作为一次性迁移源、历史留档和隔离测试夹具。
- MySQL 密码只能存在于被 Git 忽略的 `.env` 或进程环境中，不得进入源码、命令输出、日志、测试、报表、文档或版本库；备份调用必须通过进程环境传递密码，不得把密码拼进命令参数。

## 5. 关键目录

- `src/takealot_ops/`：业务源码
- `src/takealot_ops/competitors/`：竞品公开接口、库存探测、估算、持久化和本机 API
- `src/takealot_ops/erp/`：统一 ERP 的只读数据投影、动作接口和静态前端服务
- `frontend/competitor/`：统一 Vue 3 + TypeScript ERP 源码及生产构建（沿用竞品目录名）
- `tests/`：单元、集成和端到端测试
- `config/`：业务规则与程序设置
- `scripts/`：计划任务和 NFT102 辅助脚本
- `docs/`：需求、实施、审计、测试和项目状态文档
- `data/`：原始响应、NFT102 基准及已退役 SQLite 迁移源；正式结构化数据位于本机 MySQL
- `exports/`、`outputs/`：生成结果
- `backups/`、`logs/`：运行备份与日志

## 6. 常用验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m takealot_ops.cli verify
Set-Location .\frontend\competitor; npm.cmd run build
```

验证范围应与修改风险匹配；修改指标、导出、数据库或采集逻辑时，不能只运行无关的单个测试。

## 7. 当前状态与已知边界

- 数据接口、MySQL 存储、指标计算、异常识别、HTML/Excel/PNG 导出、统一 Vue ERP 和日常运行工具已经完成。
- 统一 ERP 已增加 MySQL 用户、可撤销会话和 `viewer`/`operator`/`admin` 三级权限，正式监听地址为 `0.0.0.0:8501`；首个管理员需在服务器本机打开 `http://127.0.0.1:8501` 初始化，之后同事使用服务器局域网 IP 登录。
- 统一 ERP 已包含 NFT102 续写，可校验并归档运营最终版、识别连续下一日期、调用既有只读采集脚本并提供新表格下载。
- ERP 侧边栏已显示最近成功采集时间和最新指标日期，并提供完整流程手动刷新按钮；Windows 运营日报任务默认时间为中国时间10:05、18:00和18:30。
- 合成数据验收已完成；真实店铺只读联调状态以 `docs/PROJECT_STATUS.md` 的最新记录为准。
- 当前官方 Offer 数据提供近30天滚动浏览量，没有可确认的精确每日产品访客数接口。
- 正式运行已切换到本机 MySQL 8.0；旧 SQLite 的12张表共4204行已全量迁移并逐表核对一致，ERP、采集、指标、竞品、报表、完整性检查和每日备份均读取或写入 MySQL。
- 竞品观察最小闭环已完成：公开商品/评论采集已由普通 HTTP 请求切换为 Playwright 复用一个本机浏览器会话逐地址导航，采集器、ERP 接口和 CLI 全链路使用异步调用；支持多链接逐条采集、单条失败隔离、全部变体枚举及独立库存、PLID 共享完整评论、历史快照、累计销量区间和观察期信号。该页面现为统一 Vue ERP 内的正式模块，与店铺经营页面共用 `8501` 和 MySQL。
- 经营四象限分位切换会同步移动十字分界中心；竞品采集页在模块导航切换期间保持任务状态，返回后继续显示原进度和结果。
- 经营四象限悬浮卡已加入平台库存、首次上架/本库最早记录、平台库存增加对应的最近补货时间、平台明确返回的近30天浏览量和指标截止日期，并在图框外固定显示横纵轴含义；独立近7日流量因接口未提供而不再推算或展示；悬浮卡使用图框外浮层避免裁切，字段标题已提高对比度。MySQL Offer 当前表和历史快照表已增加可空 `created_at` 字段。
- 当前11个竞品链接保留在本机 MySQL；重新采集后已有13条商品快照、14条变体结果和425条共享去重评论，`PLID70540744` 与 `PLID99275672` 最新精确平台仓购物车上限分别为4和10。
- 运营日报模块已接入统一 Vue ERP：MySQL 保存10:05/18:00不可覆盖版本、人工候选、最终确认、操作审计和18:30待办快照；页面支持销量高亮、库存不平红标及人工取消、任意异常备注、无差异批量合并、差异逐项选源、历史待办全局提醒和未合并导出阻断。确认完成后按旧表商品列/日期四行阅读习惯自动导出 Excel。
- 竞品公开评论区已支持评价分类、起止日期组合筛选和日期/评分排序，并显示筛选结果数与评论总数。
- 竞品批量链接输入已支持编译器式错误定位：非 Takealot 域名、格式无效或缺少 PLID 时自动回跳并红色选中第一条错误链接，同时显示错误行号、原因和完整链接。
- 当前是用户要求的最小模块；原独立工具中的关键词/类目排名、卖家上新、Buy Box 历史占有率和六工作表 Excel 尚未迁入，后续只能在保持现有销量口径与批量架构的前提下逐项增加。

## 8. 修改日志

| 日期 | 修改摘要 | 主要文件 | 验证与结论 |
|---|---|---|---|
| 2026-07-24 | 新增可替代人工横向表格的运营日报闭环：MySQL冻结10:05早间和18:00晚间商品订单/平台仓库存/近30天浏览量，人工候选与系统值并存，确认和修改强制备注并写入审计；无差异可批量合并，差异逐项选源，确认后新系统值变化会重新待办；库存按前一日库存减当天订单核对并支持带原因取消红标；18:30保存跨天待办快照，全局提醒历史未确认项，未合并时阻止旧表布局Excel导出并定位日期/SKU，全部确认后自动本地导出；Windows任务改为10:05/18:00/18:30三时点 | `src/takealot_ops/erp/daily_report.py`、`src/takealot_ops/storage/models.py`、`src/takealot_ops/erp/web.py`、`src/takealot_ops/cli.py`、`frontend/competitor/`、`scripts/install_scheduled_task.ps1`、测试与文档 | 完整回归 `199 passed`（1条第三方TestClient弃用提示），日报接口/Excel专项11项通过；Ruff、Mypy（44个源码文件）、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 通过；真实MySQL无损新增5张日报表，三个计划任务下一次分别为2026-07-25 10:05/18:00/18:30；正式 `8501` 页面确认新导航、空白起始状态、禁用导出和控制台无新错误 |
| 2026-07-24 | 完成 Hermes 留下的竞品 Playwright 异步迁移并修复明确库存提示仍被继续探测：公开商品/评论由普通 HTTP 客户端改为复用本机 Chrome/Edge 导航公开 JSON，采集器、ERP 接口和 CLI 同步改为异步链路；快捷菜单和自定义数量统一把 `current stock = N` 设为最高优先级，数量控件显示成功后保留3秒提示结算窗口，一旦读到数字立即返回精确库存，不再等待、翻倍或二分；恢复原先被临时跳过的9个异步库存安全测试 | `src/takealot_ops/competitors/`、`src/takealot_ops/erp/web.py`、`src/takealot_ops/cli.py`、`tests/unit/test_competitor_stock.py`、`tests/unit/test_competitors.py`、前端停止采集交互及文档 | 新增异步回归测试覆盖“Qty 9先显示、稍后出现current stock=4”、首次超量即命中后不再探测及命中后无额外等待；完整回归 `193 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy（43个源码文件）、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 全部通过，数据库检查为2026-07-24无质量事件 |
| 2026-07-24 | 将 ERP 首个管理员、管理员新增账号和重置密码的最短密码长度统一由12位调整为8位；后端仍统一使用 scrypt 随机盐哈希，前端表单、提示和运行文档同步更新 | `src/takealot_ops/erp/auth.py`、`frontend/competitor/src/pages/LoginPage.vue`、`frontend/competitor/src/pages/UsersPage.vue`、测试与文档 | 后端专项验证7位密码返回422、8位密码可完成初始化；完整回归 `190 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy（43个源码文件）和 Vue TypeScript/生产构建通过 |
| 2026-07-24 | 为局域网共享 ERP 增加完整用户权限并将正式服务改为 `0.0.0.0:8501`：新增 MySQL 用户/会话表、scrypt 密码哈希、12小时可撤销 HttpOnly 会话、SameSite=Strict Cookie、写请求 CSRF、5分钟登录失败限流和首个管理员仅本机初始化；后端强制执行查看员/运营员/管理员三级角色，Vue 增加登录页、账号状态、退出和管理员“用户权限”模块，查看员的刷新、采集、上传和生成操作同时由前端禁用；MySQL 保持仅本机连接 | `src/takealot_ops/erp/auth.py`、`src/takealot_ops/erp/web.py`、`src/takealot_ops/storage/models.py`、`src/takealot_ops/settings.py`、`src/takealot_ops/dashboard/launcher.py`、`frontend/competitor/`、配置、测试与文档 | 完整回归 `190 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy（43个源码文件）、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 通过；专项接口验证远程不能抢占首个管理员、未登录返回401、查看员写操作和用户列表返回403、缺少 CSRF 返回403、唯一管理员不可停用或降级 |
| 2026-07-24 | 增强竞品雷达批量链接校验：采集前逐行检查 URL 格式、Takealot 域名和 PLID，发现错误后停止外部请求，自动聚焦输入框、滚动并选中第一条错误链接；输入框使用红色边框、红色选区和三次脉冲提示，下方编译器式诊断条显示行号、具体原因及完整链接，用户开始修改后立即清除错误状态 | `frontend/competitor/src/pages/CompetitorsPage.vue`、`frontend/competitor/src/styles.css`、构建产物及文档 | Vue TypeScript/生产构建、Ruff、Mypy、`takealot_ops.cli verify` 通过；完整回归 `188 passed`（1条第三方 TestClient 弃用提示）。浏览器以第12行为 Mercado Libre 链接验证：输入框获得焦点、内部滚动 `170px`、完整错误链接被精确选中，`aria-invalid=true`，红色边框和脉冲动画生效，诊断条明确显示“第 12 行 / 不是 Takealot 商品链接 / 完整链接”；修改输入后诊断条、错误样式和“失败 1 个”旧状态均立即清除，运行中的 `8501` 已提供新 JS/CSS 资源 |
| 2026-07-24 | 增强竞品雷达公开评论区：现有好评/中评/差评可与开始、结束日期叠加筛选，兼容平台 `DD Mon YYYY` 和既有 ISO 日期；增加最新评论、最早评论、评分高到低、评分低到高四种展示排序，显示当前结果数/总评论数并支持一键清除时间；无日期记录在限定时间后自动排除，移动端控件改为纵向全宽布局；评论卡键加入评论人和展示序号，修复同日同标题评论在排序后重复或残留 | `frontend/competitor/src/pages/CompetitorsPage.vue`、`frontend/competitor/src/styles.css`、文档 | Vue TypeScript/生产构建、Ruff、Mypy、`takealot_ops.cli verify` 通过；完整回归 `188 passed`（1条第三方 TestClient 弃用提示）。浏览器以65条真实评论验证日期边界 `2024-03-18` 至 `2026-05-18`，最早/最新排序首尾正确，2026年日期与差评组合筛出3条，清除时间恢复6条差评，评分高低排序首条分别为5星和1星，显示数量与实际卡片数均为65 |
| 2026-07-24 | 移除经营四象限的近7日流量推算：Offers 接口没有独立近7日流量字段，因此后端不再按近30天浏览量比例生成 `page_views_7_day_estimate`，前端悬浮卡删除该卡片和估算说明，只保留平台明确返回的近30天浏览量；增加接口防回归断言并同步业务文档 | `src/takealot_ops/erp/service.py`、`frontend/competitor/`、测试与文档 | 完整回归 `188 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 通过；运行中接口确认 `page_views_7_day_estimate` 已不存在而 `page_views_30_days` 保留，浏览器悬浮卡确认无“近7日流量”文字且仍显示近30天浏览量 |
| 2026-07-24 | 修正经营四象限悬浮卡的展示与补货口径：信息卡移出散点图裁切容器，并按点位上下半区自动选择展开方向，避免向上弹出时被遮挡；字段标题提高字号、字重和明暗对比；补货信息改为读取完整 Offer 历史快照，后一条有效平台库存高于前一条时直接记录该快照的北京时间及增加数量，不再称为估算 | `src/takealot_ops/metrics/service.py`、`src/takealot_ops/erp/service.py`、`frontend/competitor/`、测试与文档 | 后端专项测试 `27 passed`；完整回归 `188 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 通过。真实 MySQL 接口确认库存增加商品返回北京时间及增加量，例如 `9902340483776` 为 `2026-07-24 10:10`、较前次 `+10`；浏览器确认顶部商品点改为向下展开，信息卡超出图框仍完整可见，字段标题计算样式为 84% 白色、10.08px、600字重 |
| 2026-07-23 | 增强经营四象限商品时效信息：持久化 Offers 接口 `created_at` 作为平台首次上架时间，旧历史回退为本库最早完整记录；按相邻有效平台库存上升估算最近补货日期和增加量；按近30天日均乘7提供明确标注“估算”的7日流量参考；悬浮卡新增库存、指标截止日期及上述时间信息，图框外增加固定横纵轴说明；现有 MySQL 两张 Offer 表无损增加可空 `created_at` | `src/takealot_ops/domain.py`、`src/takealot_ops/storage/`、`src/takealot_ops/erp/service.py`、`frontend/competitor/`、测试与文档 | 完整回归 `188 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy、Vue TypeScript/生产构建和 `takealot_ops.cli verify` 通过；MySQL 架构升级成功。真实接口返回397个商品，其中旧历史397个暂按本库最早记录显示、3个商品识别到库存上升补货估算；浏览器确认横纵轴及悬浮卡的库存、7日流量参考、首次记录、补货估算和截止日期均可见，ERP 健康检查通过 |
| 2026-07-23 | 修复统一 ERP 两项页面状态：经营四象限不再把十字线固定在50%，改用接口返回的浏览量与下单排名分界同步移动两条线、轴标签及中心交点；竞品雷达通过 Vue `KeepAlive` 在模块导航时保留正在运行的采集实例、输入、进度和结果，并在采集中明确提示可安全切页 | `frontend/competitor/src/App.vue`、`frontend/competitor/src/pages/QuadrantsPage.vue`、`frontend/competitor/src/pages/CompetitorsPage.vue`、`frontend/competitor/src/styles.css`、`README.md`、`docs/PROJECT_STATUS.md`、`AGENT.md` | Vue TypeScript 检查及生产构建通过；完整回归 `187 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy、`takealot_ops.cli verify` 通过。浏览器真实数据实测标准分位交点约为50%/31%，宽松分位横向移动至约25%，严格分位移动至约75%/75%；竞品页本地校验状态在切到经营总览再返回后输入与结果均保留，控制台错误为0 |
| 2026-07-23 | 修复两个有货竞品被标记“未探测”：等待主商品异步加购完成，数量菜单关闭动画增加等待和重开；按 `aria-hidden` 排除未启用的数量输入节点，绕过目标商品卡链接对已核验输入框的指针拦截；定向清理 Braze 营销遮罩，购物车临时更新失败时重新加载并有限重试，自定义数量被平台接受且编辑器收起时通过 `Qty: N` 确认结果 | `src/takealot_ops/competitors/stock.py`、`tests/unit/test_competitor_stock.py`、`README.md`、`docs/PROJECT_STATUS.md`、`AGENT.md` | 完整回归 `187 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 通过。真实隔离匿名购物车取得 `PLID70540744=4`、`PLID99275672=10`，均为精确值并已重新采集写入 MySQL；当前数据库为11个目标、13条商品快照、14条变体结果、425条共享去重评论，测试购物车已清理 |
| 2026-07-23 | 修复竞品库存探测误点推荐商品：不再遍历页面全部 `Add to Cart`，只在当前目标 PLID 商品页的 `main aside` 主购买区接受唯一按钮；商品页 URL 必须匹配目标 PLID，进入购物车后也必须再次找到同一 PLID 的商品行，移除“购物车只有一个数量控件就视为目标”的危险回退；补充防回归测试；保留11个链接并清空修复前受影响的重新采集结果 | `src/takealot_ops/competitors/stock.py`、`tests/unit/test_competitor_stock.py`、`README.md`、`docs/PROJECT_STATUS.md` | 完整回归 `181 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 通过。4个真实商品页逐一核对：3个可购买页的主购买区各仅1个按钮，推荐区分别有22、27、7个按钮；不可购买护膝页主购买区为0且不会回退推荐位。真实隔离购物车以 `PLID95526981` 跑通同 PLID 二次核对并取得精确库存90，测试购物车已清理；ERP 8501 已重启，健康检查通过且竞品快照为0 |
| 2026-07-23 | 正式数据库由 SQLite 全面切换为本机 MySQL 8.0：新增 PyMySQL、MySQL 默认配置和本机/同步驱动校验，建立受限应用账号和 `utf8mb4` 数据库；增加空目标保护的一次性全表迁移命令、MySQL 数据库级只读 ERP 会话、`CHECK TABLE` 完整性检查及 `mysqldump --single-transaction` 每日备份；前端数据库状态与运行文档同步改为 MySQL，SQLite 只保留为退役迁移源和测试夹具 | `src/takealot_ops/settings.py`、`src/takealot_ops/storage/`、`src/takealot_ops/scheduler.py`、`src/takealot_ops/erp/`、`frontend/competitor/`、配置、测试与文档 | 旧 SQLite 的12张表4204行迁入 MySQL 并逐表数量完全一致；MySQL `CHECK TABLE`、只读事务拒绝 UPDATE、ERP 五个核心接口、HTML/Excel/PNG 读取和1,041,068字节 SQL 备份通过；完整回归 `177 passed`（1条第三方 TestClient 弃用提示），Ruff、Mypy、`takealot_ops.cli verify` 和 Vue 生产构建通过 |
| 2026-07-23 | 增加竞品变体级库存：递归枚举同一 PLID 下尺寸、颜色等选择器组合，在一个隔离浏览器会话中逐个变体探测并把 SKU、卖家、价格、库存写入独立 SQLite 明细表；不可购买或长时效变体显示“没货”；评论继续按 PLID 只抓取、去重一次；前后变体集合一致才比较汇总库存；Vue 竞品详情新增“各变体库存”表 | `src/takealot_ops/competitors/`、`src/takealot_ops/storage/models.py`、`src/takealot_ops/erp/web.py`、`frontend/competitor/`、测试与文档 | 完整回归 `172 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 和 Vue 生产构建通过。真实 `PLID96909926` 识别 Right Hand、Left Hand 两个变体，分别保存标识、价格与“没货”状态；数据库共18条商品快照、2条新变体明细、192条共享去重评论 |
| 2026-07-23 | 提升竞品有效库存口径：快捷菜单接受9件后自动进入自定义数量，优先解析 `current stock = N`，否则以超量测试和二分校验求精确平台仓上限；公开接口 `is_leadtime` 的供应商调货/长时效到货商品直接标记“没货”，排除出库存净流出销量推断；前端为没货状态增加红色标签 | `src/takealot_ops/competitors/`、`frontend/competitor/`、测试与文档 | 重点测试 `13 passed`，完整回归 `171 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 和 Vue 生产构建通过。真实隔离购物车已跑通100、54拒绝后在31测试中解析出 `current stock = 13`；4个首批竞品重新采集成功，最新值为2个没货、精确库存6和1，共17条快照/192条评论；测试购物车已清理。ERP 已重启，健康和竞品接口均为 HTTP 200 |
| 2026-07-23 | 修正经营四象限当前悬停/选中的单个“待优化”散点过黑：四类散点改为显式固定高对比填充色，“待优化”使用亮金色；散点关闭浏览器原生按钮外观，并在悬停、焦点、按下和复制状态显式保持原填充色，避免交互状态叠加后近黑 | `frontend/competitor/src/styles.css`、`README.md`、`docs/PROJECT_STATUS.md` | Vue TypeScript 检查及生产构建通过，生成 `index-CBLF9BE9.css`；运行中的 `8501` 返回 HTTP 200 并已提供该资源，构建 CSS 已确认包含原生外观关闭、亮金色和按下状态固定填充；`git diff --check` 通过（仅现有 CRLF 提示） |
| 2026-07-23 | 将经营四象限浏览器原生延迟提示替换为即时自定义 ERP 信息卡：鼠标和键盘焦点进入即显示分类、商品名、平台 SKU、近30天浏览量与近7日下单，自动避让图表边缘；保留点击复制 SKU | `frontend/competitor/src/pages/QuadrantsPage.vue`、`frontend/competitor/src/styles.css`、文档 | Vue TypeScript 检查及生产构建通过；原生 `title` 已移除，浮层使用80毫秒入场动画；运行中 `8501` 返回 HTTP 200 并已提供新 JS/CSS 哈希资源 |
| 2026-07-23 | 统一 ERP 记录时间显示：SQLite 返回的无偏移时间按 UTC 解析，“最近采集”和竞品历史快照固定转换为北京时间（Asia/Shanghai），并将“本机记录时间”改为“北京时间”；SAST 指标日期口径保持不变 | `frontend/competitor/src/time.ts`、`frontend/competitor/src/App.vue`、`frontend/competitor/src/pages/CompetitorsPage.vue`、文档 | Vue TypeScript 检查及生产构建通过；实际样例 `2026-07-23T04:30:26` 转换为 `07/23 12:30`；运行中 `8501` 返回 HTTP 200 并已提供新哈希静态资源 |
| 2026-07-23 | 改善经营四象限散点识别与运营操作：散点放大并增加悬停/键盘焦点状态，提示中显示商品和平台 SKU，点击直接复制平台 SKU；成功、SKU 缺失及复制失败均显示中文反馈 | `frontend/competitor/src/pages/QuadrantsPage.vue`、`frontend/competitor/src/styles.css`、`README.md`、`docs/PROJECT_STATUS.md` | Vue TypeScript 检查及生产构建通过；完整回归 `170 passed`（1条第三方 TestClient 弃用提示）；新构建使用哈希静态资源，运行中的 `8501` 刷新页面后加载新交互 |
| 2026-07-23 | 将原 Streamlit 南非店铺看板迁移为统一 Vue 3 + TypeScript 小型 ERP：新增经营总览、商品中心、经营四象限、风险与质量、竞品雷达、报表及 NFT102 六个模块；新增只读 ERP 数据投影和 FastAPI 动作接口，正式 `dashboard` 启动入口改为单端口 `8501`，保留 `dashboard-legacy` 回退；空数据库保持可打开且不被读取页面创建 | `src/takealot_ops/erp/`、`frontend/competitor/`、`src/takealot_ops/dashboard/launcher.py`、`src/takealot_ops/cli.py`、测试与文档 | 完整测试 `170 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 和 Vue 生产构建通过；真实 `8501` 健康、静态页、总览、四象限及竞品接口均为 HTTP 200，读取397个商品/4个竞品，`8502` 已释放；浏览器逐页及390像素窄屏检查无控制台错误 |
| 2026-07-23 | 合并原 Node 竞品库存/评论/销量工具的最小闭环并原生接入当前项目：新增竞品目标、快照和去重评论表，公开商品与评论采集、隔离匿名购物车库存探测、2%–5%评论率累计销量区间、跨快照库存/新增评论信号、批量 CLI、本机 FastAPI，以及 Vue 3 + TypeScript 竞品中心；现有 Streamlit 增加“竞品观察”入口并由启动器同时管理两个回环服务 | `src/takealot_ops/competitors/`、`frontend/competitor/`、`src/takealot_ops/storage/models.py`、`src/takealot_ops/dashboard/`、`src/takealot_ops/cli.py`、测试与文档 | 完整测试 `164 passed`（1条第三方 TestClient 弃用提示）；Ruff、Mypy、`takealot_ops.cli verify` 和 Vue 生产构建通过；4个真实链接全部保存，共5条快照/192条评论，库存结果为精确4、两个至少9、一个未探测 |
| 2026-07-23 | 将完整开发历史快进合并到 `main`，并把 `main` 设为项目后续开发与交付基线；原开发分支暂时保留，未删除 | `AGENT.md`、`docs/PROJECT_STATUS.md` | `main` 从初始提交快进包含开发分支全部44个提交；本次仅调整分支与交接文档，未修改运行代码 |
| 2026-07-23 | 统一异常口径：看板总览、Excel 和 HTML/PNG 总览均统计最新指标日去重异常商品；看板异常页默认仅显示最新日并提供“全部历史”切换；Excel、HTML/PNG 异常明细仅保留最新指标日，同时说明同一商品可有多条异常类型记录 | `src/takealot_ops/metrics/service.py`、`src/takealot_ops/dashboard/app.py`、`src/takealot_ops/exports/excel.py`、`src/takealot_ops/exports/html.py`、相关测试与文档 | 重点测试 `49 passed`；完整测试 `158 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；真实数据核对为最新日289个商品/299条记录，历史累计340个商品/986条记录；已重新生成并核对2026-07-23的HTML、Excel、PNG |
| 2026-07-23 | 导出中心新增“一键导出全部报表”：按页面截止日期先检查 SQLite 完整性，再从当前本地数据生成 HTML、Excel、PNG；不调用平台接口，并对所有已生成文件提供页面直接下载 | `src/takealot_ops/dashboard/app.py`、`tests/e2e/test_dashboard_smoke.py`、`README.md`、`docs/PROJECT_STATUS.md` | 导出与页面重点测试 `28 passed`；完整测试 `155 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；浏览器确认按钮、只读说明及三个下载入口均正常显示，未手动触发真实导出 |
| 2026-07-23 | 重做经营四象限对比：销售维度由单日改为近7日下单件数，销量分界只从正销量商品计算且保持整数，坐标改为相对排名以避免极端值压缩，并增加四色区域背景和真实阈值说明；单品分析及店铺总览的下单件数图移除小数移动平均线并固定整数刻度 | `src/takealot_ops/metrics/service.py`、`src/takealot_ops/dashboard/charts.py`、`src/takealot_ops/dashboard/app.py`、`src/takealot_ops/dashboard/labels.py`、相关测试与文档 | 重点测试 `45 passed`；完整测试 `155 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；真实数据验证得到明星27、转化问题34、潜力19、待优化40、未分类277，浏览器确认页面阈值与分类数量正确 |
| 2026-07-22 | 新增看板数据新鲜度展示和“立即刷新看板数据”按钮；手动刷新复用完整 `daily-run` 且不向页面泄露子进程输出；每日 Windows 自动任务默认改为中国时间 10:10；计划任务脚本改用兼容 Windows PowerShell 5.1 的 ASCII 源码并在运行时生成中文任务名 | `src/takealot_ops/dashboard/app.py`、`src/takealot_ops/dashboard/refresh.py`、`scripts/install_scheduled_task.ps1`、相关测试与文档 | 重点测试 `32 passed`；完整测试 `154 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；系统任务“Takealot 店铺数据每日更新”安装成功，下一次运行 2026-07-23 10:10；看板重启后健康检查 200 |
| 2026-07-22 | 修复运行中的旧看板未重新载入新增模块；将页面固定控件、业务字段、状态和说明统一为中文，隐藏框架与图表英文工具栏，并把网页上传上限固定为100兆字节 | `src/takealot_ops/dashboard/app.py`、`src/takealot_ops/dashboard/labels.py`、`src/takealot_ops/dashboard/charts.py`、`src/takealot_ops/dashboard/launcher.py`、`src/takealot_ops/settings.py`、`src/takealot_ops/nft102_portal.py`、相关测试与文档 | 中文界面重点测试 `39 passed`；完整测试 `148 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；浏览器确认新模块可见，顶部、图表、表格和上传控件无可见英文 |
| 2026-07-22 | 新增 NFT102 前端续写流程：上传运营最终版、校验并原样归档、识别下一日期、一键调用既有生成脚本、下载新 Excel 与核对说明；上传基准和生成结果均不覆盖；连续续写时替换旧日期后缀，避免文件名逐日增长 | `src/takealot_ops/nft102_portal.py`、`src/takealot_ops/dashboard/app.py`、`scripts/update_nft102_daily.ps1`、`tests/unit/test_nft102_portal.py`、`tests/unit/test_nft102.py`、`README.md`、`docs/PROJECT_STATUS.md` | 重点测试 `34 passed`；完整测试 `147 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过 |
| 2026-07-22 | 统一 Excel 输出对齐规则：运营日报全部有内容单元格水平、垂直居中；NFT102 仅居中目标工作表并保持其他店铺工作表不变 | `src/takealot_ops/exports/excel.py`、`scripts/write_nft102_workbook.py`、`tests/integration/test_excel_export.py`、`tests/unit/test_nft102.py` | 完整测试 `138 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；9 个日报工作表完成样式与视觉检查，公式错误扫描为 0 |
| 2026-07-22 | 补齐 Excel 异常商品表的中文导出映射；`high_views_low_conversion`、`suspected_stockout`、`stale_offer_snapshot` 的异常类型和说明不再回退为英文 | `src/takealot_ops/exports/excel.py`、`tests/integration/test_excel_export.py` | 完整测试 `137 passed`；Ruff、Mypy、`takealot_ops.cli verify` 通过；修正版 Excel 的未翻译异常字段扫描为 0，公式错误扫描为 0 |
| 2026-07-22 | 建立项目长期维护档案及 Codex 自动入口；确立任何项目修改必须同步更新 `AGENT.md` 的规则 | `AGENT.md`、`AGENTS.md` | 文档规则检查；未运行代码测试（本次未修改运行代码） |
