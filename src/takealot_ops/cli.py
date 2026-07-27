"""Command-line entry point for local Takealot operations workflows."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from sqlalchemy.orm import Session

from takealot_ops.api.client import TakealotClient
from takealot_ops.collectors import collect_offers, collect_sales
from takealot_ops.competitors.service import CompetitorCollector, parse_competitor_urls
from takealot_ops.dashboard.launcher import launch_dashboard, launch_legacy_dashboard
from takealot_ops.domain import sast_date
from takealot_ops.erp.daily_report import (
    ReportCaptureResult,
    capture_daily_report,
    create_deadline_snapshot,
    daily_report_payload,
    export_operations_workbook,
    operations_business_date,
    record_daily_report_failure,
    unresolved_locations,
)
from takealot_ops.metrics.service import MetricService
from takealot_ops.quality import verify_quality
from takealot_ops.reporting import generate_daily_reports
from takealot_ops.scheduler import (
    Clock,
    DailyRunResult,
    SystemClock,
    run_daily,
    verify_database_integrity,
)
from takealot_ops.settings import DashboardSettings, Settings, SettingsError
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.mysql_migration import migrate_sqlite_to_mysql
from takealot_ops.storage.repository import Repository


EXIT_CONFIGURATION = 2
EXIT_COLLECTION = 3
EXIT_QUALITY = 4
EXIT_OPERATION = 5
_DAILY_REPORT_RETRY_PLAN = (
    ("标准接口", True, 0.0),
    ("新会话重试", True, 20.0),
    ("直连备用", False, 60.0),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="takealot-ops",
        description="Takealot 店铺只读数据采集、看板和报表工具",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="采集 Offer 和近七个 SAST 自然日销售")
    collect.add_argument("--start", type=_parse_date, help="销售开始日期 YYYY-MM-DD")
    collect.add_argument("--end", type=_parse_date, help="销售结束日期 YYYY-MM-DD")

    export = commands.add_parser("export", help="从本地数据库生成 HTML、Excel 和 PNG")
    export.add_argument("--date", type=_parse_date, help="报告截止日期 YYYY-MM-DD")

    commands.add_parser("daily-run", help="执行每日采集、校验、导出和备份")
    daily_report_run = commands.add_parser(
        "daily-report-run",
        help="执行完整采集并冻结运营日报早间或晚间版本",
    )
    daily_report_run.add_argument(
        "--slot",
        choices=("morning", "evening", "manual"),
        required=True,
    )
    daily_report_capture = commands.add_parser(
        "daily-report-capture",
        help="不访问平台，直接把当前数据库冻结为运营日报版本",
    )
    daily_report_capture.add_argument(
        "--slot",
        choices=("morning", "evening", "manual"),
        required=True,
    )
    daily_report_capture.add_argument("--date", type=_parse_date)
    commands.add_parser(
        "daily-report-deadline",
        help="记录18:30仍未合并的数据并在可导出时保存本地表格",
    )
    commands.add_parser("dashboard", help="在 127.0.0.1 启动本地看板")
    commands.add_parser(
        "dashboard-legacy",
        help="在 127.0.0.1 启动保留的 Streamlit 旧版看板",
    )

    competitors = commands.add_parser(
        "collect-competitors",
        help="采集一个或多个 Takealot 竞品链接",
    )
    competitors.add_argument("urls", nargs="+", help="Takealot 商品链接")
    competitors.add_argument(
        "--skip-stock",
        action="store_true",
        help="只采集公开商品与评论，不执行匿名购物车库存探测",
    )
    competitors.add_argument(
        "--show-browser",
        action="store_true",
        help="在库存探测时显示隔离浏览器窗口",
    )

    verify = commands.add_parser("verify", help="检查数据库完整性和数据质量")
    verify.add_argument("--date", type=_parse_date, help="检查日期 YYYY-MM-DD")
    migrate = commands.add_parser(
        "migrate-to-mysql",
        help="把旧 SQLite 全量迁移到当前配置的空 MySQL 数据库",
    )
    migrate.add_argument(
        "--sqlite-source",
        type=Path,
        default=Path("data/takealot.db"),
        help="旧 SQLite 文件路径",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd())).resolve()
    logger = _configure_logging(project_root)
    try:
        if args.command == "collect":
            exit_code = _collect_command(project_root, args.start, args.end)
        elif args.command == "export":
            exit_code = _export_command(project_root, args.date)
        elif args.command == "daily-run":
            exit_code = _daily_command(project_root)
        elif args.command == "daily-report-run":
            exit_code = _daily_report_run_command(project_root, args.slot)
        elif args.command == "daily-report-capture":
            exit_code = _daily_report_capture_command(
                project_root,
                args.slot,
                args.date,
            )
        elif args.command == "daily-report-deadline":
            exit_code = _daily_report_deadline_command(project_root)
        elif args.command == "dashboard":
            exit_code = launch_dashboard(DashboardSettings.from_env(project_root))
        elif args.command == "dashboard-legacy":
            exit_code = launch_legacy_dashboard(
                DashboardSettings.from_env(project_root)
            )
        elif args.command == "collect-competitors":
            exit_code = _collect_competitors_command(
                project_root,
                args.urls,
                skip_stock=args.skip_stock,
                show_browser=args.show_browser,
            )
        elif args.command == "verify":
            exit_code = _verify_command(project_root, args.date)
        elif args.command == "migrate-to-mysql":
            exit_code = _migrate_to_mysql_command(
                project_root,
                args.sqlite_source,
            )
        else:
            parser.error("unknown command")
        logger.info("%s finished with exit code %s", args.command, exit_code)
        return exit_code
    except (SettingsError, ValueError) as exc:
        logger.error("%s configuration failed: %s", args.command, type(exc).__name__)
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIGURATION
    except Exception as exc:
        logger.error("%s failed: %s", args.command, type(exc).__name__)
        print(f"操作失败：{type(exc).__name__}；详情见 logs/takealot-ops.log", file=sys.stderr)
        return EXIT_OPERATION


def _collect_command(project_root: Path, start: date | None, end: date | None) -> int:
    settings = Settings.from_env(project_root)
    captured_at = SystemClock().now()
    end_date = end or sast_date(captured_at)
    start_date = start or end_date - timedelta(days=6)
    if start_date > end_date:
        raise ValueError("--start must be on or before --end")

    engine = create_engine_for_settings(settings)
    client = TakealotClient(settings)
    try:
        create_schema(engine)
        with Session(engine) as session:
            repository = Repository(session)
            offers = collect_offers(client, repository, captured_at)
            if not offers.succeeded:
                print("Offer 采集失败；未发布不完整快照。", file=sys.stderr)
                return EXIT_COLLECTION
            sales = collect_sales(client, repository, start_date, end_date)
            if not sales.succeeded:
                print("Sales 采集失败；未发布不完整分页结果。", file=sys.stderr)
                return EXIT_COLLECTION
        print(
            f"采集完成：Offer {offers.counts['records']} 条，"
            f"Sales {sales.counts['records']} 条，日期 {start_date} 至 {end_date}。"
        )
        return 0
    finally:
        client.close()
        engine.dispose()


def _export_command(project_root: Path, report_date: date | None) -> int:
    settings = DashboardSettings.from_env(project_root)
    verify_database_integrity(settings)
    as_of = report_date or sast_date(SystemClock().now())
    engine = create_engine_for_settings(settings)
    try:
        with Session(engine) as session:
            service = _metric_service(Repository(session), project_root)
            dataset = service.dashboard_dataset(as_of)
        paths = generate_daily_reports(dataset, project_root / "exports", as_of)
    finally:
        engine.dispose()
    print(f"HTML：{paths.html}")
    print(f"Excel：{paths.excel}")
    if paths.png_error is None:
        print(f"PNG：{paths.png}")
    else:
        print(f"PNG 未生成：{paths.png_error}", file=sys.stderr)
    return 0


def _daily_command(project_root: Path) -> int:
    result = run_daily(Settings.from_env(project_root), SystemClock())
    if result.report_paths is not None:
        print(f"日报目录：{result.report_paths.html.parent}")
    if result.backup_path is not None:
        print(f"数据库备份：{result.backup_path}")
    if result.quality is not None and not result.quality.passed:
        print(f"数据质量警告：{result.quality.issue_count} 项。", file=sys.stderr)
    if result.error:
        print(result.error, file=sys.stderr)
    return result.exit_code


def _daily_report_run_command(project_root: Path, slot: str) -> int:
    settings = Settings.from_env(project_root)
    clock = SystemClock()
    captured_at = clock.now()
    business_date = operations_business_date(captured_at)
    result, attempts = _run_protected_daily_report_collection(
        settings,
        clock,
        business_date=business_date,
        slot=slot,
    )
    finalized_at = clock.now()
    if not _complete_collection_succeeded(result):
        final_reason = str(attempts[-1]["reason"]) if attempts else "采集流程未返回结果"
        reason = (
            f"自动保护共尝试 {len(attempts)} 次仍失败；"
            f"最终原因：{final_reason}"
        )
        _record_report_failure(
            settings,
            business_date=business_date,
            slot=slot,
            captured_at=finalized_at,
            reason=reason,
            attempts=attempts,
        )
        print(reason, file=sys.stderr)
        return result.exit_code
    try:
        capture = _capture_report_values(
            settings,
            business_date=business_date,
            slot=slot,
            captured_at=finalized_at,
            capture_details={
                "attempts": attempts,
                "recovered": len(attempts) > 1,
                "capture_method": attempts[-1]["strategy"],
                "downstream_status": result.status,
            },
        )
    except Exception as exc:
        _record_report_failure(
            settings,
            business_date=business_date,
            slot=slot,
            captured_at=finalized_at,
            reason=f"{type(exc).__name__}: 日报冻结失败",
            attempts=attempts,
        )
        raise
    print(
        f"运营日报 {slot} 版本已冻结：{capture.product_count} 个商品，"
        f"业务日 {business_date}，重新待确认 {capture.reopened_count} 个。"
    )
    return result.exit_code


def _run_protected_daily_report_collection(
    settings: Settings,
    clock: Clock,
    *,
    business_date: date,
    slot: str,
    runner: Callable[..., DailyRunResult] = run_daily,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[DailyRunResult, list[dict[str, object]]]:
    """Retry a complete capture with a fresh client and a direct-network fallback."""
    logger = logging.getLogger("takealot_ops.cli")
    attempts: list[dict[str, object]] = []
    last_result: DailyRunResult | None = None
    for attempt_number, (strategy, trust_env, delay) in enumerate(
        _DAILY_REPORT_RETRY_PLAN,
        start=1,
    ):
        if delay:
            sleeper(delay)
        started_at = clock.now()
        try:
            result = runner(
                settings,
                clock,
                report_date=business_date,
                trust_env=trust_env,
            )
        except Exception as exc:
            result = DailyRunResult(
                status="collection_failed",
                start_date=business_date,
                end_date=business_date,
                error=f"{type(exc).__name__}: 采集流程异常退出",
            )
        last_result = result
        succeeded = _complete_collection_succeeded(result)
        reason = _daily_attempt_reason(result)
        attempt = {
            "attempt": attempt_number,
            "strategy": strategy,
            "trust_env": trust_env,
            "started_at": started_at.isoformat(),
            "finished_at": clock.now().isoformat(),
            "status": "success" if succeeded else "failed",
            "workflow_status": result.status,
            "reason": reason,
            "offer_run_id": (
                result.offer_result.run_id if result.offer_result is not None else None
            ),
            "sales_run_id": (
                result.sales_result.run_id if result.sales_result is not None else None
            ),
        }
        attempts.append(attempt)
        logger.info(
            "daily-report capture date=%s slot=%s attempt=%s strategy=%s "
            "status=%s workflow_status=%s reason=%s",
            business_date,
            slot,
            attempt_number,
            strategy,
            attempt["status"],
            result.status,
            reason,
        )
        if succeeded:
            return result, attempts
    if last_result is None:
        raise RuntimeError("日报采集保护层没有执行任何尝试")
    logger.error(
        "daily-report capture exhausted date=%s slot=%s attempts=%s final_reason=%s",
        business_date,
        slot,
        len(attempts),
        attempts[-1]["reason"],
    )
    return last_result, attempts


def _complete_collection_succeeded(result: DailyRunResult) -> bool:
    return bool(
        result.offer_result is not None
        and result.offer_result.succeeded
        and result.sales_result is not None
        and result.sales_result.succeeded
    )


def _daily_attempt_reason(result: DailyRunResult) -> str:
    if result.offer_result is None:
        return result.error or f"完整采集未启动（{result.status}）"
    if not result.offer_result.succeeded:
        return f"Offers 失败：{result.offer_result.error or result.status}"
    if result.sales_result is None:
        return result.error or f"Sales 未启动（{result.status}）"
    if not result.sales_result.succeeded:
        return f"Sales 失败：{result.sales_result.error or result.status}"
    if result.status not in {"success", "quality_failed"}:
        return f"Offers/Sales 已完整获取；后处理状态 {result.status}"
    return "Offers/Sales 完整获取"


def _daily_report_capture_command(
    project_root: Path,
    slot: str,
    report_date: date | None,
) -> int:
    settings = Settings.from_env(project_root)
    captured_at = SystemClock().now()
    business_date = report_date or operations_business_date(captured_at)
    result = _capture_report_values(
        settings,
        business_date=business_date,
        slot=slot,
        captured_at=captured_at,
    )
    print(
        f"运营日报 {slot} 版本已冻结：{result.product_count} 个商品，"
        f"业务日 {business_date}。"
    )
    return 0


def _daily_report_deadline_command(project_root: Path) -> int:
    settings = Settings.from_env(project_root)
    captured_at = SystemClock().now()
    business_date = operations_business_date(captured_at)
    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        unresolved = create_deadline_snapshot(
            engine,
            business_date=business_date,
            snapped_at=captured_at,
        )
        if not daily_report_payload(engine, business_date)["items"]:
            print(f"{business_date} 尚无运营日报采集版本，本次不生成空表。")
            return 0
        if unresolved:
            print(
                f"{business_date} 仍有 {unresolved} 个商品未确认，"
                "已保存18:30待办快照。"
            )
            return 0
        if unresolved_locations(engine, business_date):
            print("历史日期仍有未合并数据，本次不导出。", file=sys.stderr)
            return 0
        destination = (
            project_root
            / "exports"
            / "operations-daily"
            / business_date.isoformat()
            / f"运营日报_{business_date.isoformat()}.xlsx"
        )
        export_operations_workbook(
            engine,
            business_date=business_date,
            destination=destination,
        )
    finally:
        engine.dispose()
    print(f"运营日报已自动导出：{destination}")
    return 0


def _capture_report_values(
    settings: Settings,
    *,
    business_date: date,
    slot: str,
    captured_at: datetime,
    capture_details: dict[str, object] | None = None,
) -> ReportCaptureResult:
    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        return capture_daily_report(
            engine,
            business_date=business_date,
            slot=slot,
            captured_at=captured_at,
            capture_details=capture_details,
        )
    finally:
        engine.dispose()


def _record_report_failure(
    settings: Settings,
    *,
    business_date: date,
    slot: str,
    captured_at: datetime,
    reason: str,
    attempts: list[dict[str, object]] | None = None,
) -> None:
    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        record_daily_report_failure(
            engine,
            business_date=business_date,
            slot=slot,
            captured_at=captured_at,
            reason=reason,
            attempts=attempts,
        )
    finally:
        engine.dispose()


def _verify_command(project_root: Path, check_date: date | None) -> int:
    settings = DashboardSettings.from_env(project_root)
    verify_database_integrity(settings)
    as_of = check_date or sast_date(SystemClock().now())
    engine = create_engine_for_settings(settings)
    try:
        with Session(engine) as session:
            quality = verify_quality(Repository(session), as_of)
    finally:
        engine.dispose()
    if quality.passed:
        print(f"数据库完整，{as_of} 未发现数据质量事件。")
        return 0
    print(
        f"数据库完整，但 {as_of} 有 {quality.issue_count} 项数据质量事件，"
        f"其中未知销售状态 {quality.unknown_sales_status_count} 项。",
        file=sys.stderr,
    )
    return EXIT_QUALITY


def _collect_competitors_command(
    project_root: Path,
    raw_urls: Sequence[str],
    *,
    skip_stock: bool,
    show_browser: bool,
) -> int:
    return asyncio.run(
        _collect_competitors_async(
            project_root,
            raw_urls,
            skip_stock=skip_stock,
            show_browser=show_browser,
        )
    )


async def _collect_competitors_async(
    project_root: Path,
    raw_urls: Sequence[str],
    *,
    skip_stock: bool,
    show_browser: bool,
) -> int:
    urls = parse_competitor_urls("\n".join(raw_urls))
    settings = DashboardSettings.from_env(project_root)
    engine = create_engine_for_settings(settings)
    failures = 0
    try:
        create_schema(engine)
        async with CompetitorCollector(
            engine=engine,
            project_root=project_root,
        ) as collector:
            for index, url in enumerate(urls, start=1):
                result = await collector.collect(
                    url,
                    with_stock_probe=not skip_stock,
                    visible_browser=show_browser,
                )
                prefix = f"[{index}/{len(urls)}] PLID{result.plid}"
                if result.succeeded:
                    print(f"{prefix}：{result.message}")
                else:
                    failures += 1
                    print(f"{prefix}：{result.message}", file=sys.stderr)
    finally:
        engine.dispose()
    return EXIT_COLLECTION if failures else 0


def _migrate_to_mysql_command(
    project_root: Path,
    sqlite_source: Path,
) -> int:
    settings = DashboardSettings.from_env(project_root)
    source = (
        sqlite_source
        if sqlite_source.is_absolute()
        else project_root / sqlite_source
    )
    report = migrate_sqlite_to_mysql(source, settings.database_url)
    print(
        f"MySQL 迁移完成：{len(report.table_counts)} 张表，"
        f"{report.total_rows} 行。"
    )
    for table_name, count in sorted(report.table_counts.items()):
        print(f"{table_name}：{count}")
    return 0


def _metric_service(repository: Repository, project_root: Path) -> MetricService:
    return MetricService(
        repository,
        anomaly_rules_path=project_root / "config" / "anomaly_rules.yaml",
        sale_status_rules_path=project_root / "config" / "sale_status_rules.yaml",
        now=lambda: datetime.now().astimezone(),
    )


def _configure_logging(project_root: Path) -> logging.Logger:
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("takealot_ops.cli")
    logger.setLevel(logging.INFO)
    target = (log_dir / "takealot-ops.log").resolve()
    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).resolve() == target
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


if __name__ == "__main__":
    raise SystemExit(main())
