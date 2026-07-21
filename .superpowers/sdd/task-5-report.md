# Task 5 report: offline HTML, Excel, PNG, and report orchestration

## Implementation

- Added a single-file Chinese HTML operations report with inline Plotly, embedded serialization of all five `DashboardDataset` frames, KPI hierarchy, two trend charts, product search/filter controls, anomaly and quality tables, approved traffic labels, and missing-value preservation.
- The HTML export parses real `src`/`href` attributes and rejects HTTP(S) resources before applying a script-only Unicode escape to the URL-like literals inside Plotly's inline bundle. A final raw resource-token guard remains in place. No API keys, environment reads, write controls, refresh controls, external styles, fonts, scripts, or images are emitted.
- Added a macro-free OpenPyXL workbook with sheets in the exact order: `运营总览`, `单品分析`, `异常商品`, `每日汇总`, `销售明细`, `流量快照`, `指标说明`, `数据质量`.
- The workbook uses typed dates/numbers/currency, visible KPI totals, frozen headers, filters, readable Chinese widths, restrained fills/borders, red/green/amber conditional formatting, and formula-backed ISO date helper ranges for two native line charts. Dataset-originated strings beginning with `=`, `+`, `-`, or `@` are written as literal cells to prevent formula injection.
- `销售明细` is explicitly titled `商品每日销售明细/汇总` and explains that the source has no order-line frame; it exports only product/date rows from `product_daily`.
- Added Playwright PNG capture using a local `file:///` URL, 1920x1080 viewport, `[data-report-ready="true"]`, and full-page screenshot. Browser/runtime failures are translated to `PngExportUnavailable`.
- Added `ReportPaths` and `generate_daily_reports`; HTML and Excel failures remain fatal, while PNG unavailability is recorded without discarding any planned path. HTML and Excel receive the same dataset object.
- Narrowed the repository-root export ignore rule from `exports/` to `/exports/` so the source package `src/takealot_ops/exports` can be tracked while generated root reports remain ignored.

## RED/GREEN evidence

Required initial RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_html_export.py tests/integration/test_excel_export.py tests/integration/test_reporting.py -v
```

Observed three collection errors, all expected:

```text
ModuleNotFoundError: No module named 'takealot_ops.exports'
ModuleNotFoundError: No module named 'takealot_ops.exports'
ModuleNotFoundError: No module named 'takealot_ops.exports'
```

Initial implementation GREEN:

```text
11 passed in 3.41s
```

Review-driven security RED:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_html_export.py::test_html_rejects_actual_external_resource_attributes tests/integration/test_excel_export.py::test_excel_writes_untrusted_text_as_literal_cells -v
```

Observed five expected failures: a real external script resource was silently rewritten rather than rejected; Excel stored the `=` case as a formula and did not literalize `+`, `-`, or `@` cases.

Security GREEN after the minimal fixes:

```text
5 passed in 0.97s
```

## Final automated verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_html_export.py tests/integration/test_excel_export.py tests/integration/test_reporting.py -v
# 16 passed in 4.16s

.\.venv\Scripts\python.exe -m pytest -q
# 76 passed in 5.74s

.\.venv\Scripts\python.exe -m ruff check src tests
# All checks passed!

.\.venv\Scripts\python.exe -m mypy src
# Success: no issues found in 20 source files

git diff --check
# exit 0; only the existing Windows LF-to-CRLF advisory for .gitignore was printed
```

## Security and package checks

- Parsed the exact generated HTML and found zero external `script`, `link`, `img`, `iframe`, or `source` URLs and zero raw HTTP(S) `src`/`href` tokens.
- Loaded the HTML with Playwright Chromium, waited for the ready selector, and observed zero HTTP(S) network requests.
- Searched exporter/reporting source for environment access, authorization handling, and API-key references; no matches.
- Inspected the generated XLSX ZIP package and found no `vbaProject`, `externalLinks`, `connections.xml`, `queryTables`, or macro-sheet parts.
- Ran the real orchestrator for 2026-07-20 and confirmed all exact Chinese HTML/XLSX/PNG filenames exist under the date partition with `png_error=None`.

## Spreadsheet visual QA

Scratch-only setup (all paths ignored by Git):

- Generated `.superpowers/sdd/task-5-qa/qa-report.xlsx` from the representative five-frame fixture.
- Created `.superpowers/sdd/task-5-qa/qa.mjs` and a Windows `node_modules` junction inside that task-specific writable directory to the loader-provided runtime path.
- Used only `@oai/artifact-tool` to import the OpenPyXL workbook, inspect values/formulas/drawings, scan formula errors, and render all eight worksheets.

Inspected ranges and results:

- `运营总览!A1:N22`: KPI values 10 ordered units, 6 effective units, R 1,899.92 revenue, and 1 anomaly; visible `TEXT` helper formulas in E9:E10; one native chart.
- `单品分析!A1:X20`: all product/date rows and missing fields preserved; visible `TEXT` helper formulas in P6:P7; one native chart.
- Formula-error scan: zero matches for `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, and `#N/A`.
- Rendered and viewed every sheet: `运营总览`, `单品分析`, `异常商品`, `每日汇总`, `销售明细`, `流量快照`, `指标说明`, `数据质量`.

Focused repairs and recheck:

- Both chart date axes initially rendered Excel serials. Added formula-backed ISO label ranges with a blank gutter before each chart, regenerated, and confirmed both axes display `2026-07-19` and `2026-07-20` without overlap.
- `指标说明` initially clipped long field names. Widened columns A:C and confirmed all names and definitions render fully.
- The other five table sheets had readable headers/data, correct blanks, and no overlap or clipping in the first pass; they were unchanged by the focused repairs.
- A full-page HTML PNG was also visually checked; the hierarchy, two Plotly charts, controls, tables, and Chinese labels rendered correctly.

## Files changed

- `.gitignore`
- `src/takealot_ops/exports/__init__.py`
- `src/takealot_ops/exports/html.py`
- `src/takealot_ops/exports/excel.py`
- `src/takealot_ops/exports/png.py`
- `src/takealot_ops/reporting.py`
- `tests/integration/conftest.py`
- `tests/integration/test_html_export.py`
- `tests/integration/test_excel_export.py`
- `tests/integration/test_reporting.py`

## Self-review

- Confirmed all outputs derive only from the supplied `DashboardDataset`; no order-level rows, traffic observations, missing-value zeroes, API calls, or secrets are invented.
- Confirmed the approved traffic labels are present and banned traffic/visitor claims are absent from visible report content.
- Confirmed chart source ranges are visible and auditable, use typed numeric/date inputs, and have no overlap with data or controls.
- Confirmed empty frames export without exceptions and unknown KPI values remain blank/`未知` rather than zero.
- Confirmed `ReportPaths` retains the planned PNG path and error string on typed PNG failure while the HTML and workbook stay on disk.
- Independent review identified two Important issues (Excel formula injection and pre-validation HTML rewriting) plus a Ruff failure; all were reproduced/fixed and the final verification is green. No Critical issues were found.

## Concerns

- The bundled `@oai/artifact-tool` successfully imported, inspected, formula-scanned, and wrote all eight QA PNGs, but its Node process returned Windows status `0xC0000409` during teardown after every requested output had been fully written. The rendered artifacts were complete and individually viewable, so this was not an import/render availability blocker; it appears limited to runtime cleanup.
- PNG availability remains installation-dependent by design. The typed fallback is covered, and Chromium was available for the representative end-to-end run.
