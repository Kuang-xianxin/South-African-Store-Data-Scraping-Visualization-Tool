# 双节点独立蓝环境：当前交付边界

最新核验：2026-09-05 23:34（北京时间）。蓝101已可写、蓝102只读复制；双蓝网页、跨机会话/监控库CRUD和单店真实采集已通过，完整爬虫/导出/模型及HA仍未验收。主机加载修正配置的第二轮UAC取消，主机常驻进程尚未加载最终代理/子进程配置。新状态详见[可写蓝版状态](BLUE_WRITABLE_TEST.md)。以下20:22底座记录只作历史，不能将其旧只读角色/文件清单当成当前部署。

## 已部署

| 项目 | 主机 | 笔记本 |
|---|---|---|
| 机器 | DESKTOP-NTRMANG | LAPTOP-2T5MN8EU |
| 独立目录 | D:\TakealotBlue | D:\TakealotBlue |
| 新 MySQL | 127.0.0.1:3307 / server_id 101 | 127.0.0.1:3307 / server_id 102 |
| 数据角色 | read_only=1 / super_read_only=1 / GTID ON | read_only=1 / super_read_only=1 / GTID ON |
| 新测试网页 | http://127.0.0.1:8503 | http://192.168.110.13:8503 |
| Tailscale 网页 | http://100.70.103.11:8503 | http://100.122.102.37:8503 |
| 响应标签 | blue-stage-main | blue-stage-laptop |

腾讯公网蓝版`https://119.91.117.232:8443/`现直连笔记本新8503，保留原URL、不再经过旧蓝库。
笔记本旧地址`http://192.168.110.13:8502/`及`http://100.122.102.37:8502/`由Windows端口转发到本机8503；8502后面不再运行独立旧网页。
主机本地新蓝版仍为`http://127.0.0.1:8503/`。公网蓝版暂只连笔记本，不会自动切主机或回落到绿版。

两机均安装 `TakealotBlueMySQL` 自动启动服务和 `Takealot Blue Stage Web` SYSTEM开机任务。
网页任务异常重试最多12次、间隔1分钟，允许电池供电运行；不依赖登录或临时远程终端。
安装后数据库及网页已实际运行，但没有重启两台整机做开机演练，避免影响绿版。
主机非管理员会话可能看不到SYSTEM任务，不应仅据Get-ScheduledTask无结果判定安装失败。

主机网页仅回环监听，由仅tailnet的Tailscale Serve 8503转发，原13306保留。
笔记本网页监听8503，防火墙规则`TakealotBlueStage8503`仅允许主机/腾讯Tailscale地址和本地子网。
远程管理员无法修改QQ276的Tailscale登录配置，本次没有改变其登录状态。
新MySQL两端均只监听回环，尚未开放跨机数据库端口。

## 数据与代码

- 使用既有备份`takealot-20260905-010848-130512.sql.gz`，来自北京时间09:08:48绿版快照。
- 归档SHA-256：`44c951b6497b71dceb39fb701f9c1f87737b5fe07abdccaf904867da5175ae8d`，两端导入前均核验。
- 各63张表，包括独立`blue_environment_marker`；6个用户、50967条竞品快照、64073条变体记录。
- 只清空新蓝库中克隆的`erp_sessions`、`competitor_collection_jobs`、`competitor_worker_heartbeats`，三表当前均0行；原备份保留，绿版不受影响。
- 两端发布文件169个，规范化文件哈希清单SHA-256均为`863f01a302fd8ab6500f2d7ecb6ccb8333fe2cc788392582975184c028f72a13`。
- 代码固定在各自app目录，运行时显式优先该目录的src并校验包导入位置；每机复制自己的venv，未声称第三方依赖版本已全部锁定一致。
- 没有复制绿版.env、API密钥、本地任务断点和缓存。新MySQL使用独立密码，仅在受限ACL目录以机器DPAPI保存；秘密不进入仓库或报告。

## 验证

- 隔离防误操作、DPAPI、发布路径、过期结果拒绝、续租异常和取消清理等专项：24 passed；目标Ruff及两份新PowerShell脚本语法解析通过。
- 两端首页、健康及`/api/auth/status`成功；主JS及CSS经HTTP返回200且字节SHA-256一致，CSS为text/css。
- 两端`/api/auth/bootstrap`无凭据空对象探针均403，只读保护生效。
- 两端SQL身份/角色、种子标记、关键表计数和发布清单一致；未做全表逐行比较。
- 原主机3306/8501仍为PID24628/15316，主机绿库server_id1/3306/read_only0/super_read_only0；公网443绿版及改接新蓝版的8443均健康。
- 未做登录态浏览器视觉验收、完整回归、真实断机、自动接管或双机常驻爬虫验收。

## 安装边界

1. Windows新建root@localhost与skip-name-resolve不兼容，新蓝配置不启用该选项。
2. SSH会话退出可能结束直接创建的MySQL子进程，因此以独立Windows服务运行。
3. MySQL关闭TCP后仍可能释放数据文件；安装器须等精确蓝进程和3307监听同时消失，不能只等端口。
4. SYSTEM网页进程的停止必须限定命令行为D:\TakealotBlue\blue_web.py且非交互执行；仅Stop-ScheduledTask不代表所有子进程已退出。
5. HTTP探针须显式绕过外网代理；代理导致的Tailscale HTTP超时不能误判ERP停机。

## 尚未完成

- 两个新蓝库目前是同一时刻的独立只读快照，彼此不复制，也不继续同步绿版。
- 两端网页禁止修改和采集，登录会话暂在各自进程内存，未实现跨节点登录连续性。
- 共享任务完成提交已加最终条件校验；续租/心跳异常及外层取消会停止采集协程。但快照写入和任务完成不是同一事务，不能宣称exactly-once。
- 没有新蓝版常驻worker、正式共享调度、数据库自动选主、故障回归、业务凭据/附件同步或公网蓝版双上游。
- auto_failover=false、formal_crawler_enabled=false保持关闭。8502/8443现均进入新蓝版；旧副本已经停用，不再同步主机，不能把入口迁移当作数据刷新。

## 20:12 旧蓝版退役与入口替换

- 笔记本`MySQL80`旧服务和`Takealot Blue Read Only ERP`旧网页任务已注销；3306无监听，旧数据原路径已不存在。新`TakealotBlueMySQL`服务、`Takealot Blue Stage Web`与`Takealot Blue Arbiter Observer`任务仍运行。
- 仅将笔记本`D:\TakealotMySQLReplica`下的`data/binlog/relay/logs/my.ini`移至`D:\TakealotBlue\retired\legacy-blue-20260905-201245`。归档连同任务XML、服务参数及脱敏预检记录共241文件、1823544532字节；这是可恢复退役，不是永久擦除或已回收磁盘空间。
- 新蓝MySQL3307仍使用`D:\TakealotMySQLReplica\mysql-8.0.46-winx64\bin\mysqld.exe`，该共享程序目录必须保留，禁止删除整个旧根目录。旧秘密/种子/历史归档、用户源码仓库和`D:\TakealotOffsiteBackup\repository`亦未删除。旧HA禁用任务、腾讯SSH中继及绿版配置不在本次清理范围，没有顺手停掉或改动。
- 8502通过`0.0.0.0:8502 -> 127.0.0.1:8503`持久TCP转发兼容，继续使用原先仅允许主机/腾讯Tailscale和LocalSubnet的防火墙规则；IP Helper原已Running/Automatic，没有扩大开放范围。未重启笔记本验证持久启动。
- 退役标记`D:\TakealotHA\blue-green\legacy-blue-retired.json`状态completed。旧Python/PowerShell网页启动器和安装器均增加标记检查，拒绝再启动旧蓝版；部署前对比旧文件确认除新增保护外无内容差异。退役脚本默认仅预演、硬性限制笔记本身份，SQL逐一检查旧id2/3306/旧datadir/双只读与新id102/3307/蓝标记，路径拒绝链接；新8502兼容入口健康后才停止旧MySQL，等待其进程和端口全部退出再归档。
- 腾讯只修改`/etc/nginx/conf.d/takealot-erp-blue-public.conf`并平滑reload；新配置SHA-256 `e57fb11fa06595747569204a8d7b540c2ce9c46d8f7c860776f89a22b2b6bd4f`。回滚副本为`/var/backups/takealot-blue-entry.Oy3mQR/legacy-blue.conf`。首次即时健康检查仍收到旧标签而自动回滚；检查改为等待新标签后再次切换成功。绿版tailnet/public两份Nginx配置SHA分别仍为`b9e6e42e9a12d84a7f8bc27f6c83796fdef14401c37acb336baf3aa5dd5c9a79`/`c90e1bc03738572cee888d0de1ddd6f5b3f4b8363a7f6863cb56b9ef96d12bb1`。
- 笔记本新蓝网页仅信任腾讯peer `100.72.100.10`声明的HTTPS，以便公网会话Cookie加Secure；不信任LAN或8502转发的loopback声明。`blue_web.py`两机文件SHA为`b055749a3f4a455ba662c267d11e33c0cba1f92211d1d47a01bfe95051120c1f`；只重启笔记本新蓝网页加载该设置，主机行为未变且未重启。会话Cookie名为`takealot_blue_stage_session`，旧蓝用户切换后需重新登录。
- 23项退役/只读/新库隔离定向测试、Ruff、PowerShell/Bash语法和Nginx配置检查通过。LAN8502的65个静态文件HTTP/内容哈希及CSS MIME通过；两机新库发布清单169文件及关键计数仍一致。公网新蓝首页/健康通过，未做登录态浏览器验收或整机断电/重启演练。
- 云端日志20:01–20:02曾有绿版连接上游超时，早于20:08首次蓝版入口切换；公网健康复测正常。这是独立网络观察，本轮未修改雪莲/Clash/Tailscale，也没有把它归因为数据库或宣称已修复。

20:22补充验收：公网8443也完成65/65静态文件HTTP/SHA与CSS MIME核对，连同LAN8502共130项通过；本地主机新蓝、笔记本8502及公网8443的账号状态均为`setup_required=false/bootstrap_allowed=false`。公网检验中一次TLS连接失败、一次TCP连接超时，随后未绕过证书验证重试成功；最终8443账号状态200耗时2.83秒、绿版443健康200耗时6.70秒，不据健康恢复认定网络问题已消除。主机20:22:18仲裁报告仍同时看到两机fresh、observe、writer_permitted=false。文档及源码定向diff检查通过；共享工作区已有其它任务修改保留，未提交或推送。

恢复边界：归档仍含旧数据库，可人工还原，但必须先重新核验主从角色和复制坐标。恢复旧8502要先处理其端口别名与退役标记，恢复旧服务要将精确归档子目录放回原路径、核对服务参数及只读角色；不得自动执行升主或恢复旧任务。单独恢复腾讯旧配置而不恢复其上游不会恢复旧蓝系统。

## 已确定的云端范围

用户已明确选择：腾讯云只做仲裁与访问入口，不存第三份完整业务数据库。主机与笔记本各自存蓝库，并以单写主方式设计交接；两台爬虫最终共享任务，但不代表两份数据库可以各自独立写。
MySQL Group Replication容忍一个节点故障需要3个数据库成员组成多数派；两份MySQL加普通SSH见证不等于三成员数据库组。
仅双数据节点的其他方案需另行设计并验证可靠隔离，不能仅按“连不上主机”自动升主。
依据：[MySQL组复制容错](https://dev.mysql.com/doc/refman/8.0/en/group-replication-fault-tolerance.html)、[网络与实例要求](https://dev.mysql.com/doc/refman/8.0/en/group-replication-requirements.html)。

腾讯云本轮实测约1963MiB总内存、542MiB可用，2GiB交换空间已用约1999MiB，另有PostgreSQL、Uvicorn等业务；不能据此直接判定持续内存抖动。仅安装下述蓝版仲裁程序/账号并平滑reload SSH，没有安装第三份MySQL、传输业务备份、停止其他业务或修改Nginx。
当前Tailscale两机间走DERP(lax)，两次337/340ms；同一笔记本网页经公司LAN连接约2.5ms、总响应约66ms（单次样本）。后续同步优先验证LAN，异地再评估Tailscale；未修改雪莲/Clash。

## 12:11 蓝版仲裁观察链路

- 云端独立账号`takealot-blue-arbiter`无sudo，只允许专用密钥执行固定节点的仲裁程序；禁止TTY和转发。旧`takealot-witness`及`takealot-relay`账号/账本保持不变，既有SSH允许账号全部保留。
- 配置为`/etc/takealot-blue-arbiter.json`，账本为`/var/lib/takealot-blue-arbiter/state/ledger.json`；它们只有节点身份、任期、GTID与只读/复制状态，不含业务表数据或数据库密码。
- 配置由root持有并钉住server_id=101/102、两机独立server_uuid及相同种子SHA。账本更新使用flock、临时文件、fsync和原子替换；文件缺失/损坏不能自动重置为一次新选举。
- **实际SSH入口硬性只支持observe**。代码中的受控交接模型有双抢拒绝、过期/旧任期拒绝、旧主当前任期停写回执及精确GTID边界检查，但还没有配套节点写入守卫和独立隔离证明；修改云端mode配置也不能直接启用它。不能把单元模型误称为已运行的自动选主。
- 主机首次UAC取消后，用户明确要求再次发起确认；第二次通过，12:10:16已安装SYSTEM任务`Takealot Blue Arbiter Observer`。12:10:26及12:11:20报告持续更新，云端同时看见两机fresh、read_only=1/super_read_only=1、epoch=0、owner=null、writer_permitted=false；此前真实acquire请求返回denied。主机与笔记本均已自动上报，但开机触发仅确认已注册，未重启整机演练。
- 笔记本`Takealot Blue Arbiter Observer`已以SYSTEM开机任务运行，每5秒报状态，断连只重连该观察链路、不写数据库。使用腾讯Tailscale地址100.72.100.10，HostKeyAlias钉住此前已验证的119.91.117.232指纹。
- 两机专用私钥均在本机`D:\TakealotBlue\secrets\arbiter_ed25519`，未导出私钥或放进仓库。笔记本后台密钥ACL及所有者已调整为SYSTEM/Administrators，随后实际连接和连续心跳通过。远程终端里公网/私网出站SSH的超时没有充分根因证据，不能直接归咎于Clash或数据库。
- 状态文件`D:\TakealotBlue\state\arbiter-observer.json`记录最新时间、阶段和脱敏错误类别；应核对时间持续更新和节点fresh，不能仅按历史文件存在就认定常驻在线。主机本轮已完成相隔近一分钟的两次更新核验。
- 本轮48项蓝版隔离/仲裁/队列/worker专项、目标Ruff和PowerShell解析通过，云端Bash/Python语法通过；部署程序SHA-256为`126d28698193c2d769f9cc2872c9e8fad55306edb8d02b53d206d028617f87da`，与源码一致。未做真正断电、网络分区、云端重启或自动切库演练。

主机开机任务授权与安装已完成。下一步建立蓝库GTID复制、节点写入守卫与可验证旧主隔离；旧主失联时不能把超时视为已停写。两台网页的共享会话/统一写库入口、共享调度、爬虫独立出口和蓝版公网双上游仍待完成，正式绿版不切换。

## 审计命令

在对应机器执行：

```powershell
& 'D:\TakealotBlue\runtime\Scripts\python.exe' 'D:\TakealotBlue\blue_node.py' audit
```

prepare、initialize、restore及服务安装器默认预演，写操作需显式--apply或-Apply。
禁止改成绿版3306或绿版目录，禁止为重试导入而删除不明库或跳过身份检查。
旧候选app保留在蓝版staging下，现有用户文件没有被清理。
