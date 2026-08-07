# 星凯 ERP 超级补货 — 底层接口调用分析

## 背景

Takealot Marketplace API (`marketplace-api.takealot.com/v1`) 仅提供只读端点，不含入库单创建能力。
星凯 ERP 的超级补货（`https://www.xingkaierp.com/oms/OmsNfRestock`）通过自有后端代理层对接 Takealot 卖家后台，
将补货操作封装为内部 API 供前端调用。

前端页面显示的"可约/不可约"状态来自 Takealot 卖家后台的 UI 层校验，星凯的代理层不依赖该 UI 状态，
直接与 Takealot 后端交互，因此不受前端展示限制。

---

## 流程概览

```
1. GET  /jeecgboot/oms/omsNfGoodsInfNew/getShopList          → 获取店铺列表
2. POST /jeecgboot/oms/omsNfGoodsInfNew/selectReplenishmentList → 查询补货清单（含库存信息）
3. POST /jeecgboot/oms/omsNfGoodsInfNew/saveReplenishmentList   → 提交补货单 ★核心
   ├── 返回 NEED_2FA → 需要 OTP 验证
   │   4. POST /jeecgboot/oms/omsNfGoodsInfNew/verifyLoginOtp → 提交 OTP
   │   5. 重新调 saveReplenishmentList（session 已通过）
   └── 返回 code:200 → 成功
```

---

## 认证头

| Header | 值 | 说明 |
|---|---|---|
| `Authorization` | JWT token | 登录后获取 |
| `X-Access-Token` | 同上 JWT | 冗余头 |
| `X-Tenant-Id` | `1344` | 固定值 |
| `X-Version` | `v3` | 固定值 |
| `X-TIMESTAMP` | 毫秒时间戳 | 每次请求生成 |
| `X-Sign` | MD5 签名 | 见下方算法 |
| `Content-Type` | `application/json;charset=UTF-8` | |

---

## X-Sign 签名算法

```python
import hashlib
import json
import re
import urllib.parse

SIGNATURE_SECRET = "dd05f1c54d63749eda95f9fa6d49v442a"


def parse_query_string(url):
    """提取 URL 中的 query 参数，以及路径末尾逗号分隔的路径变量。"""
    result = {}
    last_seg = url[url.rfind("/") + 1:]
    if "," in last_seg:
        if "?" in last_seg:
            last_seg = last_seg[:last_seg.index("?")]
        result["x-path-variable"] = urllib.parse.unquote(last_seg)
    m = re.match(r"^[^?]+\?([\w\W]+)$", url)
    if m:
        for match in re.finditer(r"([^&=]+)=([\w\W]*?)(&|$|#)", m.group(1)):
            k, v = match.group(1), match.group(2)
            if isinstance(v, (int, float)):
                v = str(v)
            result[k] = v
    return result


def merge_object(target, source):
    """合并 source 到 target。bool→"true"/"false"，number→字符串，数组/item 逐元素复制。"""
    if not source:
        return target
    for k, v in source.items():
        if isinstance(v, bool):
            target[k] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            target[k] = str(v)
        else:
            target[k] = v
    return target


def compute_sign(url: str, params: dict | None, data) -> str:
    """
    计算 X-Sign。
    url  : 相对路径，如 "/jeecgboot/oms/omsNfGoodsInfNew/saveReplenishmentList"
    params: 请求的 query params（通常为 None）
    data  : 请求体（list 或 dict）
    """
    obj = parse_query_string(url)
    if params:
        obj = merge_object(obj, params)
    if data is not None:
        # data 是数组时，变成 {"0": {第1行}, "1": {第2行}, ...}
        if isinstance(data, list):
            data = {str(i): item for i, item in enumerate(data)}
        obj = merge_object(obj, data)

    # 按 key 排序，删除 _t（如果有）
    sorted_obj = {k: obj[k] for k in sorted(obj.keys())}
    sorted_obj.pop("_t", None)

    raw = json.dumps(sorted_obj, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5((raw + SIGNATURE_SECRET).encode()).hexdigest().upper()
```

---

## selectReplenishmentList — 查询补货清单

```
POST /jeecgboot/oms/omsNfGoodsInfNew/selectReplenishmentList
```

**请求体（已知参数）**：
```json
{
  "shopName": "VoltTech ZA",
  "pageNo": 1,
  "pageSize": 500
}
```

**返回关键字段**（每行）：
```json
{
  "offerId": 235133257,
  "shopName": "VoltTech ZA",
  "sku": "9902351332575",
  "tsinId": "103996414",
  "imageUrl": "http://takealot.s3.amazonaws.com/...",
  "productLabelNumber": "9902351332575",
  "stockAtTakealotTotal": 5,
  "cptQuantity": 0,
  "jhbQuantity": 5,
  "dbnQuantity": 0,
  "totalStockCover": 0,
  "cpt": 0,
  "jhb": 0,
  "dbn": 0
}
```

---

## saveReplenishmentList — 提交补货单 ★核心★

```
POST /jeecgboot/oms/omsNfGoodsInfNew/saveReplenishmentList
timeout: 60s
```

**请求体**（数组，每行是要补货的商品）：
```json
[{
  "id": null,
  "createTime": null,
  "offerId": 235133257,
  "shopName": "VoltTech ZA",
  "imageUrl": "http://takealot.s3.amazonaws.com/...",
  "sku": "9902351332575",
  "tsinId": "103996414",
  "barcode": null,
  "productLabelNumber": "9902351332575",
  "stockAtTakealotTotal": 5,

  "cptQuantity": 0,
  "jhbQuantity": 5,
  "dbnQuantity": 0,

  "cpt": 0,
  "jhb": 0,
  "dbn": 0,

  "cptStock": 0,
  "jhbStock": 0,
  "dbnStock": 50,

  "cptReplenish": null,
  "jhbReplenish": null,
  "dbnReplenish": 50,

  "... 其他字段保持原样或 null ..."
}]
```

⚠️ 关键字段：
- **`cptStock`**：CPT 仓（开普敦）补货数量 → 对应表格中的 cptReplenish
- **`jhbStock`**：JHB 仓（约翰内斯堡）补货数量 → 对应表格中的 jhbReplenish
- **`dbnStock`**：DBN 仓（德班）补货数量 → 对应表格中的 dbnReplenish
- 只有 `cptStock > 0 || jhbStock > 0 || dbnStock > 0` 的行才会被提交

📋 数据来源：从 `selectReplenishmentList` 返回的行中直接拿，加上你要补货的数量填到 `xxxStock` 字段即可。

**返回**：
```json
// 成功
{ "code": 200, "message": "..." }

// 需要 OTP
{
  "message": "NEED_2FA",
  "result": {
    "email": "xxx@xxx",
    "sessionId": "abc123...",
    "shopName": "VoltTech ZA",
    "needTwoFactor": true
  }
}
```

---

## verifyLoginOtp — OTP 验证

```
POST /jeecgboot/oms/omsNfGoodsInfNew/verifyLoginOtp
```

**请求体**：
```json
{
  "shopName": "VoltTech ZA",
  "sessionId": "从 NEED_2FA 响应中获取",
  "otp": "用户输入的验证码"
}
```

**返回**：
```json
// 成功
{ "code": 200, "message": "验证成功" }
```

⚠️ OTP 通过后，需要**重新调 saveReplenishmentList**（用同样的数据），此时 session 已被验证通过，会直接成功。

---

## getShopList — 获取店铺列表

```
GET /jeecgboot/oms/omsNfGoodsInfNew/getShopList
```

返回店铺名列表，用于填充 `shopName` 参数。

---

## 完整调用示例（Python 伪代码）

```python
# 1. 获取店铺
shops = get("/oms/omsNfGoodsInfNew/getShopList")

# 2. 查询补货清单
items = post("/oms/omsNfGoodsInfNew/selectReplenishmentList",
             {"shopName": "VoltTech ZA", "pageNo": 1, "pageSize": 500})

# 3. 对要补货的商品，设置数量
payload = []
for item in items:
    if item["sku"] == "9902351332575":
        item["dbnStock"] = 50        # DBN 仓补 50 个
        item["dbnReplenish"] = 50
        payload.append(item)

# 4. 提交
resp = post("/oms/omsNfGoodsInfNew/saveReplenishmentList", payload)

# 5. 如果需要 OTP
if resp.get("message") == "NEED_2FA":
    session = resp["result"]["sessionId"]
    shop = resp["result"]["shopName"]
    # 等用户输入验证码...
    otp_code = input("验证码: ")
    post("/oms/omsNfGoodsInfNew/verifyLoginOtp",
         {"shopName": shop, "sessionId": session, "otp": otp_code})
    # 重新提交
    post("/oms/omsNfGoodsInfNew/saveReplenishmentList", payload)
```

---

## JWT Token 获取

从浏览器的请求中复制 Authorization 头的值。token 有时效性，过期需重新从浏览器获取。
在 Network 面板找任意一个到 `xingkaierp.com` 的请求，复制 `Authorization` 或 `X-Access-Token` 的值。

---

## 项目现有代码

- 项目根目录：`D:\南非店铺数据抓取`
- 已有 API client：`src/takealot_ops/api/client.py`（只读 Takealot Marketplace API）
- 建议新建：`src/takealot_ops/erp/xingkai_restock.py`（星凯补货 API）
- 环境变量：.env 中可加 `XINGKAI_JWT_TOKEN` 来存 token
