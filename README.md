# Takealot 店铺运营数据工具

这是一个在 Windows 本机运行的小型运营 ERP：通过只读 Seller API 采集自有店铺 Offer 与 Sales 数据，并通过公开商品接口和隔离匿名购物车观察竞品。统一的 Vue 3 + TypeScript 前端包含经营总览、商品中心、经营四象限、风险与质量、竞品雷达、报表与 NFT102 工作台；自有店铺和竞品历史统一保存在 SQLite。

## 1. 安装

在 PowerShell 中进入项目目录：

```powershell
Set-Location -LiteralPath 'D:\南非店铺数据抓取'
uv venv --python 3.11 .venv
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

检查命令是否已安装：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli --help
```

## 2. 配置 API Key

复制模板，生成项目根目录下的 `.env` 文件：

```powershell
Copy-Item .env.example .env
notepad .env
```

只修改这一行：

```env
TAKEALOT_API_KEY=在这里粘贴真实Key
```

程序会自动读取 `D:\南非店铺数据抓取\.env`。已经存在的系统环境变量优先于 `.env`。不要把真实 Key 写进 `.env.example`，也不要把 `.env` 发给他人或提交到 GitHub。

## 3. 首次采集与检查

采集当前 Offer 和最近七个南非标准时间（SAST）自然日的 Sales：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli collect
```

也可以指定销售日期范围：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli collect --start 2026-07-01 --end 2026-07-21
```

检查数据库完整性和当天数据质量：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli verify
```

如果出现未知销售状态，命令会返回非零状态。先在 `config/sale_status_rules.yaml` 中确认该状态应计入还是排除，再重新计算。

## 4. 本地看板

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli dashboard
```

浏览器打开 `http://127.0.0.1:8501`。这是正式的统一 Vue ERP 地址，不再需要相邻端口。ERP 默认只监听本机回环地址，不允许绑定局域网地址。浏览、筛选和切换页面时只读取 SQLite，不调用 API，因此查看已有历史数据不需要 API Key。

侧边栏会显示“最近采集”和“最新指标”，用于判断当前看到的数据是否已经更新。“最近采集”以及竞品历史快照的时间固定按北京时间（Asia/Shanghai）显示，不跟随浏览器或南非平台时区；“最新指标”仍是既定的 SAST 业务日期。需要立即更新时，可点击“刷新全部数据”；该按钮会调用项目根目录 `.env` 中的 API Key，依次完成只读采集、指标重建、日报导出、完整性检查和备份，通常需要 1 至 3 分钟。刷新期间不要重复点击或关闭页面；失败时可查看 `logs\takealot-ops.log`，页面不会显示或记录 API Key。

“经营四象限”使用商品最新的近30天浏览量和截至最新指标日的近7个自然日下单件数。图上横纵坐标显示商品在店铺内的相对位置，避免少数极端商品把其余点挤在一起；四类散点使用固定高对比色，其中“待优化”为亮金色，不会继承按钮状态变成近黑色。鼠标移到商品点上会立即出现 ERP 样式的信息卡，显示商品名称、平台 SKU、真实浏览量和真实整数件数，点击小点会直接复制平台 SKU 并显示结果反馈。销量为0始终放在低销量侧，缺失数据保持未分类。单品分析和店铺总览的每日下单件数图只显示真实整数件数，不再绘制可能产生小数的移动平均线。

“报表工作台”提供“生成全部报表”按钮，按顶部选择的截止日期从现有本地数据生成离线网页、电子表格和图片。生成过程不重新采集、不调用平台接口；完成后可在页面分别下载三个文件。若所选日期已经存在日报，页面会直接显示现有文件的下载按钮，再次导出会按当前本地指标重新生成同名日报。

异常口径默认以“最新指标日”为准：看板总览、Excel 总览和 HTML 总览都统计该日出现异常的去重商品数；异常明细保留同一商品在当天触发的每一种异常，因此明细记录条数可能大于异常商品数。看板“异常商品”页默认仅显示最新指标日，可切换“全部历史”追溯旧记录；Excel、HTML 和 PNG 日报默认只输出最新指标日异常，不混入历史日期。

### 4.1 竞品观察

“竞品雷达”是统一 Vue ERP 的一个正式模块，与经营总览等页面共用 `8501` 地址和同一份 SQLite，不再通过 Streamlit 嵌入或单独启动第二个端口。

竞品中心支持一次粘贴多条 Takealot 商品链接，每行一条，按 PLID 自动去重。当前最小闭环会：

- 读取公开商品标题、当前卖家、价格、评分和全部公开评论；
- 按 4–5 星、3 星、1–2 星固定口径汇总好评、中评和差评；
- 可选使用隔离浏览器加入购物车、测试当前卖家/SKU 的可售上限，并在结束后清空测试购物车；
- 保存每次历史快照和去重评论；
- 按 2%–5% 假设评论率给出商品维度累计销量区间；
- 从第二次可比快照开始，展示库存净流出、新增评论和观察期销量信号。

库存不是 Takealot 全平台物理总库存，而是采集时当前匿名会话、当前卖家和当前 SKU 的平台仓购物车可售上限。快捷数量菜单接受9件后，程序会切换到自定义数量输入：优先解析 `current stock = N` 明确警告；没有明确上限时，通过超量测试和二分校验尽量取得精确值，只有999件仍被接受或页面拒绝继续验证时才显示保守下限。公开接口标记为 `is_leadtime` 的供应商调货/长时效到货商品不计入有效平台仓库存，统一显示“没货”，也不参与库存净流出销量推断。库存变化和评论法都不是官方销量；补货、购物车占用、取消、评论延迟以及跨卖家/变体评论都可能影响判断。

大量链接首次建档时，可在页面关闭库存探测，先快速采集公开商品与评论；后续再分时补充库存。单个链接失败不会中断整批。

当前交付是最小模块。原独立工具中的关键词/类目排名、卖家上新、Buy Box 历史占有率和六工作表 Excel 尚未迁入；这些信号不会在当前页面中伪装成已支持。

也可从命令行采集：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli collect-competitors `
  "https://www.takealot.com/example/PLID12345678"

# 只采集公开商品与评论
.\.venv\Scripts\python.exe -m takealot_ops.cli collect-competitors `
  --skip-stock "https://www.takealot.com/example/PLID12345678"
```

统一 ERP 的 Vue 源码暂位于兼容目录 `frontend\competitor\`。修改前端后执行：

```powershell
Set-Location .\frontend\competitor
npm.cmd install
npm.cmd run build
```

如需临时核对迁移前的旧界面，可在停止正式 ERP 后运行：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli dashboard-legacy
```

该命令只用于兼容回退，新功能以统一 Vue ERP 为准。

## 5. 生成分享报表

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli export
```

指定报告日期：

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli export --date 2026-07-21
```

文件保存到 `exports\YYYY-MM-DD\`：

- 独立 HTML：可直接发送给运营人员，用浏览器打开，不依赖外网脚本。
- Excel：包含运营总览、单品分析、异常商品、每日汇总、销售明细、流量快照、指标说明和数据质量。
- PNG：适合发送到聊天群或日报。

## 6. 每日完整运行

```powershell
.\.venv\Scripts\python.exe -m takealot_ops.cli daily-run
```

执行顺序为：完整分页采集 → 七个 SAST 自然日指标重建 → 数据质量检查 → 报表导出 → SQLite 完整性检查 → 数据库备份。任何分页失败都不会发布不完整快照或报表。日志写入 `logs\takealot-ops.log`，不会记录 API Key。

## 7. 安装 Windows 每日计划任务

安装脚本本身不会自动运行；只有运营人员明确执行后才会创建计划任务。默认每天中国时间 `10:10`，避开平台 10 点切日窗口。自动任务与页面手动刷新执行的是同一套完整流程：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_scheduled_task.ps1 `
  -ProjectPath 'D:\南非店铺数据抓取' `
  -DailyAt '10:10'
```

计划任务使用项目自己的 `.venv`，工作目录固定为项目根目录，并强制设置 `TAKEALOT_DASHBOARD_HOST=127.0.0.1`。

## 8. 备份与恢复

每次 `daily-run` 会在 `backups\` 中生成一致性 SQLite 备份，只保留最新 8 份。

恢复步骤：

1. 停止正在运行的看板和每日任务。
2. 将当前 `data\takealot.db` 复制到安全位置留档。
3. 把选定的 `backups\takealot-*.db` 复制为 `data\takealot.db`。
4. 运行 `.\.venv\Scripts\python.exe -m takealot_ops.cli verify`。
5. 验证通过后再启动看板。

## 9. 流量指标口径

`page_views_30_days` 只能称为“近30天浏览量”；不得将其或每日快照差值标注为精确日流量或访客数。

“近30天日均浏览量”只是滚动窗口值除以 30；“30天浏览量窗口净变化”只是相邻快照的窗口差值。缺失值保持为空，不补零。

## 10. 什么时候考虑 MySQL

当前单机、单店、单运营人员场景中，自有店铺数据和竞品历史统一使用 `data\takealot.db`，不需要安装 MySQL。出现以下任一情况再启动迁移评估：

- 多人或多个任务需要同时写入数据库；
- 看板需要部署到另一台服务器；
- 管理多个店铺并需要统一权限控制；
- SQLite 文件和备份窗口已经明显影响每日运行；
- 需要数据库级高可用、集中备份或审计。

业务代码通过 SQLAlchemy 隔离数据库访问，但当前看板和自动备份明确只支持同步 SQLite；切换 MySQL 前必须补充迁移脚本、方言测试、只读看板事务和新的备份方案。

## 11. 一键更新 NFT102 访客表

推荐从统一 ERP 的“`报表工作台` → `NFT102 续写`”页面操作：

1. 运营同事完成当天备注并保存 Excel；
2. 在页面上传这份最终版 `.xlsx`；
3. 页面自动识别表内最新日报日期，并默认选择连续下一天；
4. 点击“保存基准并生成下一日表格”；
5. 生成完成后直接下载新表格和运营核对说明。

上传文件会按内容哈希原样存档到 `data\nft102-baselines\`，不会被覆盖。新 Excel 和核对报告仍输出到 `outputs\nft102-daily\YYYY-MM-DD\`。程序只在点击生成按钮后调用只读 Takealot API；运营填写的内容、批注、图片和其他工作表继续沿用上传版本。

连续上传前一天生成的文件时，输出文件名会替换旧的 `_NFT102_日期` 后缀，不会每天叠加日期导致文件名越来越长。

中国时间每天 10:00 左右是平台销量切日窗口，建议 10:05 后执行。若需要保留原命令行方式，仍可双击项目根目录的 `更新NFT102日报.bat`；该方式会自动寻找本机最新模板，而网页方式始终以本次明确上传的运营最终版为准。

看板固定使用中文界面：导航、按钮、上传提示、字段名、商品状态、异常说明以及图表和表格工具控件均不显示英文。平台返回的商品原始名称、店铺代码、商品编码和南非货币符号属于真实业务数据，保持平台原值，不做猜测性翻译。网页上传上限为100兆字节。

填写口径：

- 表格日期使用当天日期；“当天订单数”使用前一天完整的 Sales quantity。
- “访客总数”填写 `page_views_30_days`，含义是近30天滚动浏览量。
- 接口不提供精确“当天访客数”，因此该行留空。
- “平台库存数量”填写 Takealot 各地区 `quantity_available` 合计；收货中和在途库存不计入可售库存。
- 仅按表头中的完整 13 位 SKU 自动匹配；无法识别、无法匹配和重复旧列会留空并写入核对报告。

每次会同时生成 `.核对报告.txt`（供运营查看）和 `.核对报告.json`（供程序审计）。

## 常用命令

```powershell
# 完整测试
.\.venv\Scripts\python.exe -m pytest -q

# 代码检查
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
```

项目状态和后续工作见 `docs\PROJECT_STATUS.md`。
