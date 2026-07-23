"""Closed Chinese display vocabulary for the operations dashboard."""

from __future__ import annotations


PAGE_NAMES = (
    "店铺总览",
    "单品分析",
    "经营四象限",
    "异常商品",
    "数据质量",
    "NFT102 日报更新",
    "导出中心",
)

TRAFFIC_METRIC_LABELS = {
    "page_views_30_days": "近30天浏览量",
    "page_views_30_day_average": "近30天日均浏览量",
    "page_views_window_net_change": "30天浏览量窗口净变化",
}

QUADRANT_LABELS = {
    "star": "明星商品",
    "conversion_issue": "转化问题商品",
    "potential": "潜力商品",
    "optimize": "待优化商品",
    "unclassified": "未分类",
}

FIELD_LABELS = {
    "metric_date": "数据日期",
    "event_date": "事件日期",
    "offer_id": "商品编号",
    "sku": "库存编码",
    "tsin_id": "平台商品编号",
    "barcode": "条码",
    "title": "商品名称",
    "ordered_units": "下单件数",
    "ordered_units_7_days": "近7日下单件数",
    "effective_units": "有效销售件数",
    "ordered_revenue": "下单金额",
    "page_views_30_days": TRAFFIC_METRIC_LABELS["page_views_30_days"],
    "page_views_30_day_average": TRAFFIC_METRIC_LABELS["page_views_30_day_average"],
    "page_views_window_net_change": TRAFFIC_METRIC_LABELS["page_views_window_net_change"],
    "conversion_percentage_30_days": "近30天转化率",
    "conversion_percentage_previous_30_days": "上一周期转化率",
    "conversion_change_points": "转化率周期变化",
    "total_stock": "平台可售库存",
    "offer_status": "商品状态",
    "status": "商品状态",
    "selling_price": "当前售价",
    "rrp": "建议零售价",
    "benchmark_price": "基准价",
    "discount_percentage": "折扣率",
    "quantity_returned_30_days": "近30天退货数量",
    "quadrant": "经营分类",
    "anomaly_type": "异常类型",
    "severity": "严重程度",
    "explanation": "说明",
    "event_type": "质量事件",
    "details": "详情",
    "created_at": "记录时间",
}

OFFER_STATUS_LABELS = {
    "buyable": "可购买",
    "not_buyable": "不可购买",
    "disabled_by_seller": "卖家已停用",
    "disabled_by_takealot": "平台已停用",
}

SEVERITY_LABELS = {
    "warning": "提醒",
    "critical": "严重",
    "info": "提示",
}

SALE_STATUS_LABELS = {
    "Shipped to Customer": "已发货给顾客",
    "Preparing for Customer": "正在为顾客备货",
    "Inter DC Transfer": "配送中心间调拨",
    "Returned": "已退货",
    "<missing>": "未提供状态",
}

ANOMALY_LABELS = {
    "sales_drop": "销量突降",
    "sales_spike": "销量突增",
    "high_views_low_conversion": "高浏览低转化",
    "low_views_high_conversion": "低浏览高转化",
    "suspected_stockout": "疑似断货",
    "non_buyable": "不可购买",
    "stale_offer_snapshot": "数据停止更新",
    "unknown_sale_status": "未知销售状态",
}

ANOMALY_EXPLANATIONS = {
    "sales_drop": "当日下单件数低于历史基准。",
    "sales_spike": "当日下单件数高于历史基准。",
    "high_views_low_conversion": "近30天浏览量较高但转化率较低，建议检查商品页、价格和转化环节。",
    "low_views_high_conversion": "浏览量较低但转化率较高，建议检查曝光机会。",
    "suspected_stockout": "显示库存为零，但商品仍可购买或最近7天有销量。",
    "non_buyable": "商品当前不是可购买状态。",
    "stale_offer_snapshot": "最新商品数据早于时效阈值，建议检查数据采集。",
    "unknown_sale_status": "销售状态尚未配置，有效件数暂不计算。",
}

EVENT_LABELS = {
    "unknown_sale_status": "销售状态未配置",
    "missing_sku": "库存编码缺失",
}
