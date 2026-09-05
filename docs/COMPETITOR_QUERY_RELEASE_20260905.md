# 2026-09-05 竞品查询交付记录

源码从干净 main `2b6a281` 在隔离分支 `codex/competitor-query-local` 独立实现。共享目录的旧查询草稿未用于匹配算法实现，也未被覆盖。普通点击在账号可见的系统已有商品中匹配，不增加平台商品查询、采集或数据库写入；图片沿用现有同源缩略图接口。

## 功能和验证

- 个人关注、自有商品、真正竞品、类目目录的商品卡均常驻可点击“竞品查询”；个人卡无快照也可打开证据不足状态。
- 两类结果同时显示，各自独立分页；排除自身并按PLID去重。外层真正竞品和查询结果共用完整卡片组件，单列布局保留原指标、类目层级、个人池标记及详情入口。
- 匹配使用持久化类目、受控商品主体、型号/数字规格和工作方式。品牌、卖家、库存和销售表现不决定资格；评论和销量只用于相同语义分后的排序。缺少证据时保守留空，规则分不是概率。
- 前端全部测试 **311/311**，其中匹配专项 **22项**。Vue TypeScript、干净构建、保留已部署其他功能的叠加构建均通过。Windows测试读取统一CRLF/LF，未修改App.vue业务逻辑。
- 最终发布包通过Chrome真实渲染验收：四类入口；帐篷PLID98579515查询得到几乎同款2条、相同需求37条；结果单列完整卡片；结果PLID96405139打开完整卖家对比/趋势详情；Escape关闭详情后返回原结果及焦点；关闭查询恢复入口焦点；类目查询关闭后保留类目目录；390×844无横向溢出。浏览器警告/错误为0。
- 查询时观察到的请求为同源GET（原有状态轮询和缩略图），未出现采集或写入请求。此网络记录覆盖浏览器请求，不证明缩略图服务端缓存命中情况。

## 发布构成

当前已部署前端含其他任务尚未提交的功能，不能用干净main整包覆盖。先复制现有前端源码及运行依赖重建，得到与线上**26/26文件SHA-256一致**的基线，再只替换本任务的竞品页面、匹配器、共用卡片及新增CSS。归一化构建哈希引用后，只有竞品页面JS和全局CSS内容变化，其余模块内容一致。

发布包和可复核证据保存在本机 `D:\南非店铺数据抓取\outputs\competitor-query-20260905`：`previous-manifest.json`、`release-manifest.json`、`baseline-runtime`、`release-frontend/dist`、`frontend-tests-final.log`及构建日志。Git只提交干净基线上的本任务源码、测试和文档，不混入其他任务源码或旧静态资源删除。

| 文件 | SHA-256 |
| --- | --- |
| index.html | 6a2cf3c21fd9ef8d6165d21a63bb59bda83c78314615cd3c219d741405898347 |
| assets/index-2OCXkWeZ.js | c567f80ba63f8b411a6e225049ce26efd17e80983e2f7ebcf8311081e68cdfe7 |
| assets/CompetitorsPage-BCOc_tLF.js | a5cdf099392d939fc9fcc291a497a49295e4967eb32558fe9d69e3447a5a5b3f |
| assets/index-Dwwzc8jw-css-mime-v2.css | 371b7fc8d68eacad3aa835de3c1d021a550aeb9cc915876bbf0906c436403695 |

## 上线核验

发布前绿版PID15316、蓝版PID15996。活动采集批次`scheduled-20260904-e5deb64cc879`处于running，revision38、round13、已尝试1277/2467。绿蓝公网HTTPS健康接口均HTTP200。采用保留旧哈希资源、备份并原子替换index的静态发布，不重启服务。

2026-09-05北京时间19:09:47蓝版、19:12:04绿版已原子切换到同一发布包。包文件`release-dec258b.zip`的SHA-256为`9bf76f50f344de01615dae2e259babe7ea3211182cb9a08bd4397675879b2857`。源码提交`dec258b`已推送到`origin/codex/competitor-query-local`。

| 入口 | 健康检查及26个发布文件 | CSS类型 |
| --- | --- | --- |
| 本机绿版 http://127.0.0.1:8501 | 27/27通过 | text/css |
| LAN绿版 http://192.168.110.180:8501 | 27/27通过 | text/css |
| 公网绿版 https://119.91.117.232 | 27/27通过 | text/css |
| LAN蓝版 http://192.168.110.13:8502 | 27/27通过 | text/css |
| 公网蓝版 https://119.91.117.232:8443 | 27/27通过 | text/css |

全部请求使用正常TLS验证，未忽略证书错误。135/135项通过，无文件哈希不一致；蓝版健康仍标记`read-only-test / blue-laptop`。完整逐文件结果在证据目录的`green-http-verification.json`与`blue-http-verification.json`。

绿版正式页面已使用现有登录会话再次验收：引用`index-2OCXkWeZ.js`；帐篷查询仍为2/37条；各卡片宽度与scrollWidth均1419px；鼠标和Enter均打开完整卖家连续对比、报价及价格/库存趋势详情；query层z-index95且详情打开时inert，详情层z-index100；关闭按钮和Escape均返回原结果并恢复焦点。控制台警告/错误为0。

蓝版当前无有效登录态；新公司标题、登录页及全部文件已验证，但**未完成蓝版登录后的业务交互验收**。未索取、重置或绕过登录凭据。

两端服务PID15316/15996保持不变。19:15复核同一采集批次仍running，revision38、round13保持，已尝试从发布前1277推进至1283/2467；本任务没有暂停/恢复/重启采集。

回滚备份：绿版`outputs/competitor-query-20260905/green-index-before-dec258b.html`；蓝版`D:\TakealotHA\blue-green\release-backups\blue-index-before-competitor-query-dec258b-20260905.html`。旧哈希资源保留，必要时仅原子恢复对应旧首页；本次未执行回滚。
