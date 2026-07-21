"""Daily report export orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from takealot_ops.exports.excel import export_excel
from takealot_ops.exports.html import export_html
from takealot_ops.exports.png import PngExportUnavailable, export_png
from takealot_ops.metrics.service import DashboardDataset


@dataclass(frozen=True)
class ReportPaths:
    """Planned output paths plus an optional non-fatal PNG error."""

    html: Path
    excel: Path
    png: Path
    png_error: str | None = None


def generate_daily_reports(
    dataset: DashboardDataset,
    export_root: Path,
    report_date: date,
) -> ReportPaths:
    """Generate the three daily reports while treating PNG as best effort."""
    partition = export_root / report_date.isoformat()
    basename = f"Takealot运营日报_{report_date.isoformat()}"
    html_path = partition / f"{basename}.html"
    excel_path = partition / f"{basename}.xlsx"
    png_path = partition / f"{basename}.png"

    export_html(dataset, html_path)
    export_excel(dataset, excel_path)
    try:
        export_png(html_path, png_path)
    except PngExportUnavailable as exc:
        return ReportPaths(html_path, excel_path, png_path, str(exc))
    return ReportPaths(html_path, excel_path, png_path)
