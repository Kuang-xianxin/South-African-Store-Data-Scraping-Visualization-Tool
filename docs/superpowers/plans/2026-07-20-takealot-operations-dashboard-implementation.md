# Takealot Operations Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-local, read-only Takealot operations system that collects Offer and Sales data, stores auditable history, presents an interactive dashboard, and exports shareable offline HTML, Excel, and PNG reports.

**Architecture:** A synchronous HTTP API client feeds idempotent collectors backed by a SQLAlchemy repository. A separate metrics service produces typed dashboard datasets consumed by Streamlit and all export formats, ensuring one calculation path. SQLite is the default database, while SQLAlchemy boundaries preserve a later MySQL migration path.

**Tech Stack:** Python 3.11+, HTTPX, SQLAlchemy 2, Alembic, Pandas, Streamlit, Plotly, OpenPyXL, Playwright, PyYAML, Pytest, Ruff, Mypy.

## Global Constraints

- Run locally on Windows and store all project artifacts under `D:\南非店铺数据抓取`.
- Default database is SQLite at `data/takealot.db`; business logic must not use SQLite-specific SQL.
- Read `TAKEALOT_API_KEY` only from the environment; never log, export, or commit it.
- Call only Takealot read endpoints in version 1: `/offers`, `/sales`, and optional `/returns`.
- Use South African Standard Time (`Africa/Johannesburg`, UTC+02:00) for sales-day grouping.
- Call `page_views_30_days` “近30天浏览量”; never label it or its daily snapshot difference as exact daily traffic or visitors.
- The local dashboard binds to `127.0.0.1` by default.
- Offline HTML embeds all scripts and data required for core interaction and makes no external requests.
- Excel files contain no macros and must open without repair warnings in Microsoft Excel and WPS.
- All implementation follows test-driven development and each task receives independent spec and quality review before the next task.

## File Map

```text
pyproject.toml                         package metadata and tooling
.gitignore                             secrets, data, exports, caches
.env.example                           non-secret environment template
README.md                              operator setup and runbook
config/settings.yaml                   non-secret runtime defaults
config/anomaly_rules.yaml              anomaly thresholds
config/sale_status_rules.yaml          explicit sale-status mapping
src/takealot_ops/settings.py           validated settings
src/takealot_ops/domain.py             typed cross-module records
src/takealot_ops/api/client.py         authenticated HTTP and pagination
src/takealot_ops/api/errors.py         API error types
src/takealot_ops/storage/models.py     SQLAlchemy tables
src/takealot_ops/storage/repository.py persistence interface and implementation
src/takealot_ops/storage/migrations.py schema creation and SQLite setup
src/takealot_ops/collectors/offers.py  Offer snapshot collection
src/takealot_ops/collectors/sales.py   Sales backfill and refresh collection
src/takealot_ops/metrics/service.py    daily metrics and anomalies
src/takealot_ops/dashboard/app.py      Streamlit entry point
src/takealot_ops/exports/html.py       standalone Plotly HTML
src/takealot_ops/exports/excel.py      macro-free Excel workbook
src/takealot_ops/exports/png.py        Playwright report screenshot
src/takealot_ops/reporting.py          one-source multi-output orchestration
src/takealot_ops/cli.py                collect, dashboard, export commands
scripts/install_scheduled_task.ps1     optional Windows Task Scheduler setup
tests/fixtures/*.json                  synthetic API responses
tests/unit/                             unit tests
tests/integration/                      database and cross-module tests
tests/e2e/                              dashboard and export checks
```

---

### Task 1: Project foundation, settings, and domain types

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config/settings.yaml`
- Create: `config/anomaly_rules.yaml`
- Create: `config/sale_status_rules.yaml`
- Create: `src/takealot_ops/__init__.py`
- Create: `src/takealot_ops/settings.py`
- Create: `src/takealot_ops/domain.py`
- Create: `tests/unit/test_settings.py`
- Create: `tests/unit/test_domain.py`

**Interfaces:**
- Produces: `Settings.from_env(project_root: Path) -> Settings`
- Produces: `OfferRecord.from_api(payload: Mapping[str, Any], captured_at: datetime) -> OfferRecord`
- Produces: `SaleRecord.from_api(payload: Mapping[str, Any]) -> SaleRecord`
- Produces: `sast_date(value: datetime) -> date`

- [ ] **Step 1: Write settings and SAST failing tests**

```python
def test_settings_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    with pytest.raises(SettingsError, match="TAKEALOT_API_KEY"):
        Settings.from_env(tmp_path)


def test_sast_date_uses_south_african_day():
    value = datetime.fromisoformat("2026-07-19T23:30:00+00:00")
    assert sast_date(value).isoformat() == "2026-07-20"
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `python -m pytest tests/unit/test_settings.py tests/unit/test_domain.py -v`

Expected: collection fails because `takealot_ops.settings` and `takealot_ops.domain` do not exist.

- [ ] **Step 3: Add package metadata and minimal domain implementation**

`pyproject.toml` must define the `takealot-ops` console script as `takealot_ops.cli:main`, a `src` package layout, Python `>=3.11`, the runtime libraries in the plan header, and development dependencies `pytest`, `pytest-cov`, `ruff`, and `mypy`.

`Settings` must contain these exact fields:

```python
@dataclass(frozen=True)
class Settings:
    project_root: Path
    api_key: str
    base_url: str
    database_url: str
    request_timeout_seconds: float
    dashboard_host: str
    dashboard_port: int
```

Defaults are `https://marketplace-api.takealot.com/v1`, `sqlite:///data/takealot.db`, `30.0`, `127.0.0.1`, and `8501`. `Settings.from_env` resolves relative SQLite paths against `project_root` and raises `SettingsError` when the API key is absent or blank.

`OfferRecord` and `SaleRecord` are frozen dataclasses. `SaleRecord.order_date` is timezone-aware and `SaleRecord.sales_day` calls `sast_date`.

- [ ] **Step 4: Run unit tests, lint, and type checks**

Run:

```powershell
python -m pytest tests/unit/test_settings.py tests/unit/test_domain.py -v
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Task 1**

```powershell
git add pyproject.toml .gitignore .env.example config src/takealot_ops/__init__.py src/takealot_ops/settings.py src/takealot_ops/domain.py tests/unit/test_settings.py tests/unit/test_domain.py
git commit -m "build: add project foundation and domain types"
```

---

### Task 2: Read-only Takealot API client

**Files:**
- Create: `src/takealot_ops/api/__init__.py`
- Create: `src/takealot_ops/api/errors.py`
- Create: `src/takealot_ops/api/client.py`
- Create: `tests/unit/test_api_client.py`
- Create: `tests/fixtures/offers_page_1.json`
- Create: `tests/fixtures/offers_page_2.json`
- Create: `tests/fixtures/sales_page.json`

**Interfaces:**
- Consumes: `Settings`
- Produces: `TakealotClient.iter_items(path: str, params: Mapping[str, Any]) -> Iterator[dict[str, Any]]`
- Produces: `TakealotClient.list_offers() -> Iterator[OfferRecord]`
- Produces: `TakealotClient.list_sales(start: date, end: date) -> Iterator[SaleRecord]`
- Produces: `AuthenticationError`, `RateLimitError`, `ApiResponseError`

- [ ] **Step 1: Write failing tests for auth, pagination, read-only paths, and retries**

Tests must prove:

- `test_client_sends_api_key_header_without_putting_it_in_url`: assert the header equals the fixture key and the captured URL does not contain it.
- `test_iter_items_follows_continuation_token_until_empty`: return two fixture pages and assert all items and both request tokens.
- `test_sales_query_uses_inclusive_sast_dates_and_limit_100`: assert exact query parameters.
- `test_403_raises_authentication_error_without_retry`: assert one request and a sanitized exception.
- `test_429_uses_retry_after_then_retries`: return 429 then 200 and assert the injected sleep duration.
- `test_500_retries_three_times_then_raises`: assert four total attempts and `ApiResponseError`.
- `test_client_rejects_non_get_requests`: assert no public mutation method exists and internal request rejects methods other than GET.

Use `httpx.MockTransport`; do not call the live API.

- [ ] **Step 2: Run the API tests and verify they fail because the client is absent**

Run: `python -m pytest tests/unit/test_api_client.py -v`

Expected: import failure for `takealot_ops.api.client`.

- [ ] **Step 3: Implement the minimal synchronous client**

`TakealotClient` accepts `Settings`, optional `httpx.BaseTransport`, and optional `sleep: Callable[[float], None]`. It owns an `httpx.Client` with `X-API-Key`, timeout, and base URL. `iter_items` requests pages until `continuation_token` is absent or blank and validates that every page contains an `items` list.

Only the following methods may be public:

- `iter_items(self, path: str, params: Mapping[str, Any]) -> Iterator[dict[str, Any]]`
- `list_offers(self) -> Iterator[OfferRecord]`
- `list_sales(self, start: date, end: date) -> Iterator[SaleRecord]`
- `list_returns(self, start: date, end: date) -> Iterator[dict[str, Any]]`
- `close(self) -> None`

Retry 429 and 5xx with delays `[2.0, 5.0, 15.0]`; prefer numeric `Retry-After` for 429. Never retry 401 or 403. Redact the `X-API-Key` value from every exception message.

- [ ] **Step 4: Run focused and full static checks**

Run:

```powershell
python -m pytest tests/unit/test_api_client.py -v
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/takealot_ops/api tests/unit/test_api_client.py tests/fixtures
git commit -m "feat: add read-only Takealot API client"
```

---

### Task 3: SQLAlchemy persistence and idempotency

**Files:**
- Create: `src/takealot_ops/storage/__init__.py`
- Create: `src/takealot_ops/storage/models.py`
- Create: `src/takealot_ops/storage/migrations.py`
- Create: `src/takealot_ops/storage/repository.py`
- Create: `tests/integration/test_repository.py`

**Interfaces:**
- Consumes: `OfferRecord`, `SaleRecord`
- Produces: `create_engine_for_settings(settings: Settings) -> Engine`
- Produces: `Repository.begin_run(run_type: str) -> str`
- Produces: `Repository.upsert_offer_snapshot(record: OfferRecord, snapshot_date: date) -> None`
- Produces: `Repository.upsert_sale(record: SaleRecord, raw_payload: Mapping[str, Any]) -> None`
- Produces: `Repository.finish_run(run_id: str, status: str, counts: Mapping[str, int], error: str | None) -> None`

- [ ] **Step 1: Write failing repository tests**

Tests must prove:

- `test_offer_snapshot_is_unique_by_day_and_offer_id`: insert the same Offer twice and assert one updated row.
- `test_repeated_sale_updates_status_without_duplicate_order_item`: upsert a changed status and assert one row with the latest value.
- `test_failed_transaction_rolls_back_all_rows`: raise midway through a transaction and assert zero committed rows.
- `test_sqlite_engine_uses_wal_and_busy_timeout`: query both PRAGMA values and assert WAL plus 5000 ms.
- `test_repository_works_with_plain_sqlalchemy_selects`: read persisted rows using only portable SQLAlchemy `select` expressions.

- [ ] **Step 2: Run repository tests and confirm schema imports fail**

Run: `python -m pytest tests/integration/test_repository.py -v`

Expected: import failure for `takealot_ops.storage`.

- [ ] **Step 3: Implement tables and repository**

Create tables named exactly:

- `collection_runs`
- `offer_current`
- `offer_snapshots`
- `sale_items`
- `return_items`
- `daily_product_metrics`
- `anomaly_events`
- `data_quality_events`

Use SQLAlchemy `UniqueConstraint` for `(snapshot_date, offer_id)`, `(metric_date, offer_id)`, and `(event_date, offer_id, anomaly_type)`. Use `order_item_id` as the sales primary key. Store raw records as JSON, not stringified Python dictionaries.

SQLite setup applies `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, and a 5000 ms busy timeout through SQLAlchemy connection events. Repository methods use SQLAlchemy expressions compatible with MySQL; dialect-specific upsert code stays inside private adapter methods.

- [ ] **Step 4: Verify persistence**

Run:

```powershell
python -m pytest tests/integration/test_repository.py -v
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/takealot_ops/storage tests/integration/test_repository.py
git commit -m "feat: add idempotent operations storage"
```

---

### Task 4: Collectors, daily metrics, and anomaly rules

**Files:**
- Create: `src/takealot_ops/collectors/__init__.py`
- Create: `src/takealot_ops/collectors/offers.py`
- Create: `src/takealot_ops/collectors/sales.py`
- Create: `src/takealot_ops/metrics/__init__.py`
- Create: `src/takealot_ops/metrics/service.py`
- Create: `tests/unit/test_metrics.py`
- Create: `tests/integration/test_collectors.py`

**Interfaces:**
- Consumes: `TakealotClient`, `Repository`, anomaly and status YAML files
- Produces: `collect_offers(client, repository, captured_at) -> CollectionResult`
- Produces: `collect_sales(client, repository, start, end) -> CollectionResult`
- Produces: `MetricService.rebuild(start: date, end: date) -> int`
- Produces: `MetricService.dashboard_dataset(as_of: date) -> DashboardDataset`

- [ ] **Step 1: Write failing metric tests**

Required cases:

- `test_sales_are_grouped_by_sast_day`: use UTC boundary records and assert the SAST date.
- `test_ordered_units_include_all_statuses`: sum included, excluded, and unknown status rows.
- `test_unknown_status_is_excluded_from_effective_units_and_flagged`: assert zero effective units and one quality event.
- `test_traffic_daily_average_is_page_views_divided_by_30`: assert `1500` becomes `50.0`.
- `test_window_net_change_is_not_named_daily_traffic`: inspect exported metric metadata and approved labels.
- `test_sales_drop_rule_requires_baseline_of_two_units`: cover baselines below and above two units.
- `test_four_quadrants_use_configured_quantiles`: use deterministic values with known 25th, 50th, and 75th percentiles.

- [ ] **Step 2: Run metric and collector tests and observe missing imports**

Run: `python -m pytest tests/unit/test_metrics.py tests/integration/test_collectors.py -v`

Expected: import failures for collector and metric modules.

- [ ] **Step 3: Implement collectors and metric service**

`collect_offers` persists the complete page result atomically for a snapshot date. `collect_sales` upserts by `order_item_id`. Both create and finish `collection_runs`, publish counts, and mark failed runs with a sanitized error.

`DashboardDataset` must provide data frames named `store_daily`, `product_daily`, `offer_current`, `anomalies`, and `quality_events`. Required `product_daily` columns are:

```text
metric_date, offer_id, sku, ordered_units, effective_units, ordered_revenue,
page_views_30_days, page_views_30_day_average, page_views_window_net_change,
conversion_percentage_30_days, conversion_percentage_previous_30_days,
conversion_change_points, total_stock, offer_status
```

Anomaly rules must reproduce the exact thresholds in the PRD. Use deterministic quantile handling and cover empty or constant datasets.

- [ ] **Step 4: Run task verification**

Run:

```powershell
python -m pytest tests/unit/test_metrics.py tests/integration/test_collectors.py -v
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/takealot_ops/collectors src/takealot_ops/metrics tests/unit/test_metrics.py tests/integration/test_collectors.py
git commit -m "feat: collect and calculate operations metrics"
```

---

### Task 5: Offline HTML, Excel, and PNG exports

**Files:**
- Create: `src/takealot_ops/exports/__init__.py`
- Create: `src/takealot_ops/exports/html.py`
- Create: `src/takealot_ops/exports/excel.py`
- Create: `src/takealot_ops/exports/png.py`
- Create: `src/takealot_ops/reporting.py`
- Create: `tests/integration/test_html_export.py`
- Create: `tests/integration/test_excel_export.py`
- Create: `tests/integration/test_reporting.py`

**Interfaces:**
- Consumes: `DashboardDataset`
- Produces: `export_html(dataset, destination: Path) -> Path`
- Produces: `export_excel(dataset, destination: Path) -> Path`
- Produces: `export_png(html_path: Path, destination: Path) -> Path`
- Produces: `generate_daily_reports(dataset, export_root: Path, report_date: date) -> ReportPaths`

- [ ] **Step 1: Write failing export tests**

Tests must prove:

- `test_html_is_single_file_with_inline_plotly_and_no_http_resources`: parse the generated file and assert no external script or stylesheet URLs.
- `test_html_uses_approved_traffic_labels`: assert the approved Chinese labels and absence of banned labels.
- `test_excel_contains_all_eight_required_sheets`: compare exact workbook sheet names.
- `test_excel_has_no_vba_project_and_opens_with_openpyxl`: reopen with `keep_vba=False` and assert no VBA archive.
- `test_excel_key_totals_match_dataset`: compare workbook totals to fixture frames.
- `test_reporting_uses_date_partition_and_chinese_filenames`: compare exact paths for a fixed date.

- [ ] **Step 2: Run export tests and confirm missing modules**

Run: `python -m pytest tests/integration/test_html_export.py tests/integration/test_excel_export.py tests/integration/test_reporting.py -v`

Expected: import failures for `takealot_ops.exports` and `takealot_ops.reporting`.

- [ ] **Step 3: Implement HTML and Excel exports from the same dataset**

HTML must call Plotly with `include_plotlyjs=True` and embed serialized data in the document. Reject any generated HTML containing `src="http`, `src='http`, `href="http`, or `href='http` except plain documentation text.

Excel creates sheets exactly:

```text
运营总览, 单品分析, 异常商品, 每日汇总,
销售明细, 流量快照, 指标说明, 数据质量
```

Freeze headers, add filters, set Chinese-readable widths, apply red/green conditional formatting, and create native charts on `运营总览` and `单品分析`. Do not use macros, external links, volatile formulas, or Power Query.

- [ ] **Step 4: Implement PNG export and report orchestration**

Use Playwright Chromium to open the local HTML by `file:///` URL, set viewport to 1920×1080, wait for Plotly completion marker `data-report-ready="true"`, and save a full-page PNG. If Chromium is unavailable, raise `PngExportUnavailable` and let report orchestration complete HTML and Excel while recording the PNG failure.

- [ ] **Step 5: Verify exports**

Run:

```powershell
python -m pytest tests/integration/test_html_export.py tests/integration/test_excel_export.py tests/integration/test_reporting.py -v
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0; PNG unavailability is represented by a tested typed error, not an unhandled exception.

- [ ] **Step 6: Commit Task 5**

```powershell
git add src/takealot_ops/exports src/takealot_ops/reporting.py tests/integration/test_html_export.py tests/integration/test_excel_export.py tests/integration/test_reporting.py
git commit -m "feat: export shareable operations reports"
```

---

### Task 6: Streamlit operations dashboard

**Files:**
- Create: `src/takealot_ops/dashboard/__init__.py`
- Create: `src/takealot_ops/dashboard/app.py`
- Create: `src/takealot_ops/dashboard/charts.py`
- Create: `src/takealot_ops/dashboard/labels.py`
- Create: `tests/unit/test_dashboard_labels.py`
- Create: `tests/e2e/test_dashboard_smoke.py`

**Interfaces:**
- Consumes: `MetricService.dashboard_dataset`
- Produces: `build_sales_figure(product_daily: DataFrame) -> go.Figure`
- Produces: `build_traffic_figure(product_daily: DataFrame) -> go.Figure`
- Produces: Streamlit pages 店铺总览、单品分析、经营四象限、异常商品、数据质量、导出中心

- [ ] **Step 1: Write failing label and figure tests**

- `test_traffic_figure_contains_only_approved_metric_names`: inspect Plotly trace names.
- `test_sales_and_traffic_are_separate_figures_without_dual_axis`: assert two figures and no `yaxis2`.
- `test_dashboard_source_contains_all_six_navigation_pages`: compare navigation labels to the PRD list.
- `test_dashboard_defaults_to_localhost`: assert host `127.0.0.1` and port `8501`.

- [ ] **Step 2: Run dashboard tests and verify missing implementation**

Run: `python -m pytest tests/unit/test_dashboard_labels.py tests/e2e/test_dashboard_smoke.py -v`

Expected: import failures for dashboard modules.

- [ ] **Step 3: Implement the six-page dashboard**

Keep Streamlit rendering in `app.py` and Plotly construction in `charts.py`. `labels.py` contains a closed mapping from internal field names to approved Chinese labels. Product search matches SKU, Offer ID, TSIN, barcode, and case-insensitive title substring.

The product page renders the 30-day page-view snapshot as one figure and daily sales plus a 7-day average as a second figure. It must not configure a secondary Y axis.

- [ ] **Step 4: Run dashboard and full test suite**

Run:

```powershell
python -m pytest tests/unit/test_dashboard_labels.py tests/e2e/test_dashboard_smoke.py -v
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Task 6**

```powershell
git add src/takealot_ops/dashboard tests/unit/test_dashboard_labels.py tests/e2e/test_dashboard_smoke.py
git commit -m "feat: add local operations dashboard"
```

---

### Task 7: CLI, scheduling script, backups, and operator documentation

**Files:**
- Create: `src/takealot_ops/cli.py`
- Create: `src/takealot_ops/scheduler.py`
- Create: `src/takealot_ops/quality.py`
- Create: `scripts/install_scheduled_task.ps1`
- Modify: `README.md`
- Create: `tests/unit/test_cli.py`
- Create: `tests/unit/test_scheduler.py`
- Create: `tests/integration/test_daily_run.py`

**Interfaces:**
- Consumes: all previous modules
- Produces commands: `takealot-ops collect`, `takealot-ops export`, `takealot-ops daily-run`, `takealot-ops dashboard`, `takealot-ops verify`
- Produces: `run_daily(settings: Settings, clock: Clock) -> DailyRunResult`
- Produces: `backup_database(settings: Settings, keep: int = 8) -> Path`

- [ ] **Step 1: Write failing CLI and daily-run tests**

Tests must prove:

- `test_help_lists_all_five_commands`: capture parser help and assert exact command names.
- `test_daily_run_refreshes_seven_sast_days`: inject a clock and assert inclusive start and end dates.
- `test_daily_run_does_not_publish_reports_after_incomplete_pagination`: force page two failure and assert no report paths.
- `test_database_backup_keeps_only_eight_newest_files`: create nine dated backups and assert the oldest is removed.
- `test_verify_reports_unknown_sales_statuses`: seed an unknown status and assert non-zero quality result.
- `test_scheduler_script_binds_dashboard_to_127_0_0_1`: inspect generated task arguments.

- [ ] **Step 2: Run tests and verify CLI features are missing**

Run: `python -m pytest tests/unit/test_cli.py tests/unit/test_scheduler.py tests/integration/test_daily_run.py -v`

Expected: import failures or missing command failures.

- [ ] **Step 3: Implement CLI and daily orchestration**

Use `argparse` from the standard library. `daily-run` performs collection, metric rebuild, quality checks, exports, integrity check, and backup in PRD order. Return non-zero exit codes for configuration, collection, quality, or export failures and write sanitized logs under `logs/`.

The PowerShell script only creates a scheduled task after the operator explicitly runs it. It accepts `-ProjectPath` and `-DailyAt` parameters, defaults to `08:30`, and launches `python -m takealot_ops.cli daily-run` with the project working directory.

- [ ] **Step 4: Replace README with exact setup and operations instructions**

Document Python environment creation, editable install, API key environment configuration, initial verification, collection, dashboard launch, exports, scheduled-task installation, backup restoration, and MySQL migration trigger conditions. Include the metric disclaimer verbatim from Global Constraints.

- [ ] **Step 5: Verify commands and documentation**

Run:

```powershell
python -m pytest tests/unit/test_cli.py tests/unit/test_scheduler.py tests/integration/test_daily_run.py -v
python -m takealot_ops.cli --help
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

Expected: commands exit 0, and help displays all five subcommands.

- [ ] **Step 6: Commit Task 7**

```powershell
git add src/takealot_ops/cli.py src/takealot_ops/scheduler.py src/takealot_ops/quality.py scripts/install_scheduled_task.ps1 README.md tests/unit/test_cli.py tests/unit/test_scheduler.py tests/integration/test_daily_run.py
git commit -m "feat: add daily operations workflow"
```

---

### Task 8: Cross-output audit, packaging, and final validation

**Files:**
- Create: `tests/e2e/test_cross_output_consistency.py`
- Create: `tests/e2e/test_security_scan.py`
- Create: `tests/e2e/test_offline_report.py`
- Create: `docs/audit-report.md`
- Create: `docs/test-report.md`

**Interfaces:**
- Consumes: complete application and synthetic fixtures
- Produces: reproducible audit and test evidence

- [ ] **Step 1: Write failing end-to-end acceptance tests**

The fixture dataset must include 10 SKUs, two SAST boundary orders, a repeated order-item status update, an unknown status, missing traffic for one Offer, one out-of-stock Offer, and 31 daily Offer snapshots.

Tests must prove:

- `test_ten_sku_totals_match_database_html_and_excel`: compare all ten SKU totals across three representations.
- `test_traffic_snapshots_match_source_offers`: compare every snapshot value to the fixture response.
- `test_no_output_contains_api_key_or_auth_header`: scan generated bytes and logs for the fixture secret and header name.
- `test_offline_html_has_no_network_dependency`: intercept browser requests and assert only the local file is requested.
- `test_repeated_collection_is_idempotent`: run the same fixtures twice and compare row counts plus content hashes.
- `test_generated_workbook_reopens_without_repair`: reopen twice with OpenPyXL and validate worksheet dimensions and chart relationships.

- [ ] **Step 2: Run tests and confirm acceptance gaps fail**

Run: `python -m pytest tests/e2e -v`

Expected: new acceptance tests fail until fixtures and integration behavior are complete.

- [ ] **Step 3: Implement only the fixture and integration corrections required by failures**

Do not add new product scope. Fix production code only when an acceptance failure demonstrates a PRD violation, and add a focused regression test next to the affected module.

- [ ] **Step 4: Run final verification from a clean environment**

Run:

```powershell
python -m pip install -e ".[dev]"
python -m playwright install chromium
python -m pytest --cov=takealot_ops --cov-report=term-missing
python -m ruff check src tests
python -m mypy src
python -m build
```

Expected: zero test failures, zero Ruff errors, zero Mypy errors, successful wheel and source distribution. Coverage must be at least 85% overall and 90% for `api/client.py`, `storage/repository.py`, and `metrics/service.py`.

- [ ] **Step 5: Write audit and test reports from actual evidence**

`docs/audit-report.md` records review scope, findings, fixes, residual risks, API write-safety verification, secret scanning, and traffic-label verification. `docs/test-report.md` records the exact commands, timestamps, versions, test counts, coverage, and build artifacts from Step 4. Do not include credentials or raw seller data.

- [ ] **Step 6: Commit Task 8**

```powershell
git add tests/e2e docs/audit-report.md docs/test-report.md
git commit -m "test: verify dashboard release"
```

## Execution Order and Agent Gates

1. Execute Tasks 1–4 sequentially because each consumes interfaces from the previous task.
2. After Task 4, Tasks 5 and 6 may be implemented by separate agents, but each must receive an independent task review before integration.
3. Execute Task 7 after Tasks 5 and 6 are integrated.
4. Assign Task 8 to a testing agent that did not implement Tasks 1–7.
5. Generate a full-branch review package and assign a final auditor that did not implement the code.
6. Fix all Critical and Important findings, repeat affected tests, then run the entire verification block again.
7. Only after fresh verification, stage intended files, commit remaining documentation, push the feature branch, and open a draft PR when GitHub CLI or the GitHub connector is available.
