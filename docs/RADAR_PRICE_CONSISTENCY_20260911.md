# 雷达自有商品价格与详情一致性

2026-09-11 12:06，已发布GREEN、主机BLUE和笔记本BLUE。

## 原因与修复

PLID102576284 在同一授权范围关联两个Seller Offer。真实MySQL当前记录：VoltTech ZA（current，Offer238002441）价格520、库存1、buyable；另一家店铺（store-05，Offer238001739）价格0、库存0、disabled_by_seller。旧列表对全部非空价格直接取最小值，所以为R0；详情默认选中VoltTech ZA而显示R520。线上旧接口复现已留档，不能把差异解释成平台突然降价。

只修改 competitors/service.py 的Seller API只读价格投影：正数且有限的价格才是有效报价，零、负值或缺失保持价格未知。聚合小卡、逐店对比报价、自有报价和逐次历史采用同一规则；无有效起始价时不制造涨价/降价。原始数据库记录不改写，停用店铺仍保留，已知库存0正常显示；官方销量、库存观察、周期金额算法和权限过滤不变，未触发额外平台请求。

列表仍取当前授权/日期范围内的最低有效自有报价，详情展示所选具体店铺/Offer的价格。多家店铺均有正报价时，所选店铺可以与最低价店铺不同，不能强行把所有店铺改成同一价格。

## 验证

- 新增8项回归，覆盖有效报价+零/负/缺失报价、仅无效报价、历史价格缺失、无效起点和正常多店降价；修复前复现错误。初版正常报价测试缺少另一店起点，已补齐fixture，未改变正常价格比较规则。
- 报价/存储/物化/版本指纹55项通过。Ruff全项目通过，Mypy102份源码通过，CLI verify通过；全量后端1254项通过、1项Windows符号链接权限跳过，耗时688.75秒；保留1项现有TestClient弃用警告。
- 真实数据库全店和current单店均为520、库存1；store-05单店价格缺失、库存0，原始0元记录仍在。
- GREEN本机与公网、BLUE主机/笔记本/公网五个入口均验证单卡、分页列表、竞品查询完整卡片和详情；GREEN个人监控池及停用店铺单店路径通过。逐Offer按实际时间取最新详情点与列表逐项比较，GREEN263、BLUE221条历史保持，价格一致。
- 当前28个HTML/JS/CSS资源在六入口的HTTP/MIME/SHA及health共174项通过。没有前端源码改动，因此未重新构建前端或运行前端单测；本轮未做浏览器实点。
- 验证辅助脚本第一次错误使用竞品查询响应的store_items键，接口实际返回items；修正脚本后所有请求复核通过。首次资源检查碰到BLUE主机重启尚未监听，已保留连接拒绝日志并在启动后复查对应两入口通过。这两项没有触发额外业务修改。

## 发布与连续性

- GREEN仅在持久化active_item=null时调用正式restart_erp.ps1，health通过。原scheduled-20260911-30ea3b9ad1ab、revision45、round18、targets及重载前264条results完整保留并running；任务初始258条结果也全部保留，后续推进情况见batch-final-continuity.json。
- 两BLUE原service.py SHA相同且与GREEN任务基线一致，仅安装该一个白名单文件。公共发布包SHA256：323b83c501023c4f70ce3149ae2059a5bf1c44a38e3060fc230598315b5818f0；发布后的service.py SHA256：b2f5a3a18966ea5817f33f4efd73651d2b6ad4251c7f722854ea708b901113ee。两个inspect均changed=[]。
- 主机使用现有Windows管理员发布脚本，笔记本使用既有SSH管理员通道；只重载已有BLUE web/worker，安装前两worker idle且无待交结果。回退副本分别为D:/TakealotBlue/staging/pre-common-20260911-115915-631777和pre-common-20260911-115955-751591。
- BLUE保护文件、节点身份、原断点、日志交付状态及共享任务计数前后一致；101可写、102双只读，复制IO/SQL均Yes、延迟0、错误为空。GREEN和BLUE业务数据库仍各自独立，没有同步商品数据或变更数据库结构。
- 服务重载清除内存投影缓存，源码版本指纹使旧物化结果失效，分页接口已验证重新生成后的R520；未直接修改派生数据库，也没有把旧预览当成新结果验收。

## 文件与Git边界

证据目录：logs/radar-price-20260911/。保留任务基线、失败复现、验证日志、发布包、源码SHA、接口明细、原批次和两BLUE前后审计。task-only.patch仅对比本任务开始时的文件；工作区service.py原已有197行新增/110行删除及大量共享未提交依赖，本次没有混合commit/push或丢弃他人改动。

AGENT.md最后更新、当前状态和修改日志，以及README、PROJECT_STATUS均同步。持久用户技能 C:/Users/Mayn/.codex/skills/takealot-erp-verified-release/SKILL.md 已加入价格一致性核验规则，保留原始零价、真实库存0、授权范围、具体Offer身份及缓存更新边界。
