from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from takealot_ops.exports.html import export_html
from takealot_ops.metrics.service import DashboardDataset


class _ResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag in {"script", "img", "iframe", "source"} and values.get("src"):
            self.resources.append(values["src"] or "")
        if tag == "link" and values.get("href"):
            self.resources.append(values["href"] or "")


def test_html_is_single_file_with_inline_plotly_and_no_http_resources(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    destination = tmp_path / "report.html"

    result = export_html(dashboard_dataset, destination)
    document = result.read_text(encoding="utf-8")
    parser = _ResourceParser()
    parser.feed(document)

    assert result == destination
    assert "plotly.js" in document.lower()
    assert "dashboard-data" in document
    assert re.search(r"data-report-ready=[\"']false[\"']", document)
    assert re.search(
        r"setAttribute\([\"']data-report-ready[\"'],\s*[\"']true[\"']\)", document
    )
    assert not [url for url in parser.resources if url.lower().startswith(("http://", "https://"))]
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", document, re.IGNORECASE)


def test_html_uses_approved_traffic_labels(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    document = export_html(dashboard_dataset, tmp_path / "traffic.html").read_text(
        encoding="utf-8"
    )

    for label in ("近30天浏览量", "近30天日均浏览量", "30天浏览量窗口净变化"):
        assert label in document
    visible_document = re.sub(r"<script\b.*?</script>", "", document, flags=re.DOTALL)
    for banned in ("精确每日流量", "昨日流量", "日访问量", "访客数", "UV"):
        assert banned not in visible_document


def test_html_handles_empty_frames_without_inventing_zeroes(
    tmp_path: Path, empty_dashboard_dataset: DashboardDataset
) -> None:
    document = export_html(
        empty_dashboard_dataset, tmp_path / "empty.html"
    ).read_text(encoding="utf-8")

    assert "暂无数据" in document
    assert '"store_daily":[]' in document


def test_html_rejects_actual_external_resource_attributes(
    tmp_path: Path, dashboard_dataset: DashboardDataset, monkeypatch
) -> None:
    monkeypatch.setattr(
        "takealot_ops.exports.html.pio.to_html",
        lambda *_args, **_kwargs: '<script src="https://cdn.invalid/plotly.js"></script>',
    )

    with pytest.raises(ValueError, match="external resource"):
        export_html(dashboard_dataset, tmp_path / "unsafe.html")


def test_html_ready_waits_for_both_plotly_newplot_promises(
    tmp_path: Path, dashboard_dataset: DashboardDataset
) -> None:
    html_path = export_html(dashboard_dataset, tmp_path / "delayed.html")
    document = html_path.read_text(encoding="utf-8")
    instrument = """
<script>
(() => {
  const originalNewPlot = Plotly.newPlot.bind(Plotly);
  window.__releasePlotCompletion = {};
  Plotly.newPlot = (...args) => {
    const target = args[0];
    const plotId = typeof target === "string" ? target : target.id;
    return originalNewPlot(...args).then((result) => new Promise((resolve) => {
      window.__releasePlotCompletion[plotId] = () => resolve(result);
    }));
  };
})();
</script>
"""
    anchor = '<div id="store-trend"'
    assert document.count(anchor) == 1
    html_path.write_text(document.replace(anchor, instrument + anchor, 1), encoding="utf-8")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            page.wait_for_function(
                "Object.keys(window.__releasePlotCompletion || {}).length === 2"
            )

            assert page.locator("#report-root").get_attribute("data-report-ready") == "false"

            page.evaluate("window.__releasePlotCompletion['store-trend']()")
            page.wait_for_function(
                "window.__takealotCompletedPlots?.['store-trend'] === true"
            )
            assert page.locator("#report-root").get_attribute("data-report-ready") == "false"
            assert page.evaluate(
                "Boolean(window.__takealotCompletedPlots?.['product-trend'])"
            ) is False

            page.evaluate("window.__releasePlotCompletion['product-trend']()")
            page.wait_for_selector('[data-report-ready="true"]')
        finally:
            browser.close()
