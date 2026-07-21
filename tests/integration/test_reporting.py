from __future__ import annotations

from datetime import date
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from takealot_ops.exports import png as png_export
from takealot_ops.exports.png import PngExportUnavailable, export_png
from takealot_ops.metrics.service import DashboardDataset
from takealot_ops.reporting import generate_daily_reports


def test_reporting_uses_date_partition_and_chinese_filenames(
    tmp_path: Path, dashboard_dataset: DashboardDataset, monkeypatch
) -> None:
    seen_datasets: list[int] = []

    def fake_html(dataset: DashboardDataset, destination: Path) -> Path:
        seen_datasets.append(id(dataset))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("<main data-report-ready=\"true\"></main>", encoding="utf-8")
        return destination

    def fake_excel(dataset: DashboardDataset, destination: Path) -> Path:
        seen_datasets.append(id(dataset))
        destination.write_bytes(b"xlsx")
        return destination

    def fake_png(html_path: Path, destination: Path) -> Path:
        assert html_path.exists()
        destination.write_bytes(b"png")
        return destination

    monkeypatch.setattr("takealot_ops.reporting.export_html", fake_html)
    monkeypatch.setattr("takealot_ops.reporting.export_excel", fake_excel)
    monkeypatch.setattr("takealot_ops.reporting.export_png", fake_png)

    paths = generate_daily_reports(dashboard_dataset, tmp_path, date(2026, 7, 20))
    partition = tmp_path / "2026-07-20"

    assert paths.html == partition / "Takealot运营日报_2026-07-20.html"
    assert paths.excel == partition / "Takealot运营日报_2026-07-20.xlsx"
    assert paths.png == partition / "Takealot运营日报_2026-07-20.png"
    assert paths.png_error is None
    assert seen_datasets == [id(dashboard_dataset), id(dashboard_dataset)]


def test_reporting_keeps_html_and_excel_when_png_is_unavailable(
    tmp_path: Path, dashboard_dataset: DashboardDataset, monkeypatch
) -> None:
    monkeypatch.setattr(
        "takealot_ops.reporting.export_png",
        lambda *_: (_ for _ in ()).throw(PngExportUnavailable("Chromium unavailable")),
    )

    paths = generate_daily_reports(dashboard_dataset, tmp_path, date(2026, 7, 20))

    assert paths.html.exists()
    assert paths.excel.exists()
    assert paths.png.name == "Takealot运营日报_2026-07-20.png"
    assert not paths.png.exists()
    assert paths.png_error == "Chromium unavailable"


def test_export_png_raises_typed_error_when_browser_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    html_path = tmp_path / "report.html"
    html_path.write_text("<main data-report-ready=\"true\"></main>", encoding="utf-8")
    monkeypatch.setattr(
        png_export,
        "_render_png",
        lambda *_: (_ for _ in ()).throw(PlaywrightError("browser missing")),
    )

    try:
        export_png(html_path, tmp_path / "report.png")
    except PngExportUnavailable as exc:
        assert "browser missing" in str(exc)
    else:
        raise AssertionError("expected PngExportUnavailable")
