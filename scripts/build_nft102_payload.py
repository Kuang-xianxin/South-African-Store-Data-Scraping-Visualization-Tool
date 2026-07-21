"""Create the intermediate JSON consumed by the native WPS updater."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from takealot_ops.nft102 import build_update_payload
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--report-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--sales-complete", action="store_true")
    args = parser.parse_args()

    project_root = Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd())).resolve()
    settings = DashboardSettings.from_env(project_root)
    engine = create_engine_for_settings(settings)
    try:
        with Session(engine) as session:
            payload = build_update_payload(
                session,
                args.template,
                args.report_date,
                sales_data_complete=args.sales_complete,
            )
    finally:
        engine.dispose()

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = payload["summary"]
    unmapped = [
        item
        for item in payload["columns"]
        if item["sku"] is None or (item["reason"] and item["active"])
    ]
    skipped_duplicates = [
        item
        for item in payload["columns"]
        if not item["active"] and item["sku"] is not None
    ]
    lines = [
        "NFT102 一键更新核对报告",
        "",
        f"表格日期：{payload['report_date']}",
        f"订单数据日期：{payload['sales_date']}（表格日期的前一天）",
        f"商品列：{summary['product_columns']}",
        f"成功匹配：{summary['matched_active_columns']}",
        f"写入订单件数：{summary['ordered_units_mapped']}",
        f"近30天浏览量有值：{summary['traffic_values']}",
        f"平台可售库存有值：{summary['platform_stock_values']}",
        "",
        "自动填写口径：",
        "- 访客总数：Takealot page_views_30_days（近30天滚动浏览量）",
        "- 当天订单数：表格日期前一天的 Sales quantity",
        "- 平台库存数量：Takealot各地区quantity_available合计",
        "",
        "本次留空：",
        "- 当天访客数：接口不提供精确单日访客数",
        "- 表头无法识别13位SKU或无法匹配的商品列",
        "",
        "需人工补充SKU的列：",
    ]
    lines.extend(
        f"- {item['column_letter']}列：{item['header'] or '空表头'}" for item in unmapped
    )
    lines.append("")
    lines.append("因SKU重复而跳过的旧列：")
    lines.extend(
        f"- {item['column_letter']}列：{item['header']}" for item in skipped_duplicates
    )
    args.output_json.with_suffix(".txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8-sig"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
