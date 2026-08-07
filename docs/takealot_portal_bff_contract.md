# Takealot Seller Portal BFF 契约记录

核对日期：2026-08-05

实现更新：2026-08-06
基础地址：`https://seller-api.takealot.com`

## 证据边界

本记录来自当日公开可下载的Seller Portal环境文件与前端构建产物：

- `https://sellers.takealot.com/env.js`
- `https://sellers.takealot.com/assets/main-DhemoBky.js`

这些是Portal内部BFF契约，不是公开Marketplace API承诺，可能随前端发布变化。代码必须固定主机和路径白名单、默认关闭，并在升级后重新核验。公开`marketplace-api.takealot.com/v1`客户端继续严格只读GET。

## 已观察流程

| 阶段 | 方法与路径 | 关键载荷/结论 |
|---|---|---|
| 登录 | `POST /v1/login` | `{email,password}`；可能返回`requires_2fa/session_id`或临时`api_key` |
| OTP | `POST /v1/otp/verify_login` | `{otp,session_id,device_name,remember_device}`；ERP强制`remember_device=false` |
| 当前身份 | `GET /v2/whoami` | 登录后验证Bearer有效性 |
| 仓库 | `GET /v2/shipment/facilities` | 返回facility、region及内部ID |
| 分仓预审 | `POST /v2/shipment/shipments_review` | `{data:{shipment_items:[{offer_id,region,quantity}]}}` |
| 预审轮询/结果 | `GET /v2/task/{id}/status`、`GET /v2/task/{id}/shipment/download` | 结果按facility分组；可能含`UNALLOCATED` |
| 创建平台草稿 | `POST /v1/task/shipment` | `task_type_id=22`，载荷含`shipment_summaries/replenishment_list` |
| Shipment任务结果 | `GET /v1/shipment/task/{id}/status`、`GET /v1/shipment/task/{id}/result` | 返回`shipment_summaries/products_in_draft` |
| PO预览 | `GET /v1/shipment/{id}/confirm/preview` | 写入前人工核对 |
| 确认PO | `POST /v1/task/shipment` | `task_type_id=21`、`request_params.shipment_id` |
| Tracking | `PUT /v1/shipment/{id}/tracking_info` | `{tracking_info}` |
| 归档 | `PUT /v1/shipment/archived?shipment_ids={id}&status=true` | Portal使用重复参数序列化，单个Shipment为上述形式 |

## 尚未完成的契约确认

当前构建产物把修改发货数量与确认已发货写成`vi/shipment/...`（字母`i`），而其他同组端点均为`/v1/...`。ERP实现保留推定的`PUT /v1/shipment/{id}/shipped?status=true`，但由独立`TAKEALOT_PORTAL_SHIPPED_WRITE_ENABLED=false`默认关闭。只有在用户授权的人工测试中确认真实网络请求后才能打开；不得以猜测结果直接用于正式操作。

## ERP安全实现

- 每店铺邮箱和密码只允许服务器管理员从交互式CLI写入运行ERP的Windows账号凭据管理器；浏览器/API不提供密码字段，密码不进入`.env`、MySQL、日志、审计或页面响应。
- 临时OTP和Bearer只在当前进程按店铺隔离，退出或重启清空；OTP固定`remember_device=false`。
- 创建按钮先以`GET /v2/whoami`校验已有会话；无有效会话才使用服务器凭据调用`POST /v1/login`。2FA只由该平台响应的`requires_2fa/requires2FA`触发，ERP不按时间自行强制验证码。
- 用户只调用“直接创建”以及“OTP验证并续接”两个loopback写入口；旧浏览器密码登录、本地冻结、单独预审和单独创建路由均不存在，并继续校验ERP登录、`logistics.manage`权限与CSRF。
- 服务端预审结果必须与已记录创建请求的逐Offer、逐区域数量完全一致；`UNALLOCATED`、未知/禁用facility、区域变化或数量变化全部拒绝。
- 服务内部仍保存规范JSON的SHA-256和5分钟一次性令牌，把预审与Task 22绑定在同一个幂等请求中；这些不是页面上的第二次确认步骤。
- 每次POST/PUT最多发送一次，不自动重试。超时或任务结果不明进入`*_unknown`，要求先到Seller Portal人工核对。
- 平台任务ID、Shipment ID、状态与操作者写审计；秘密不进入审计。
- 本模块不实现容量或replenishment block绕过，也不调用星凯接口。
