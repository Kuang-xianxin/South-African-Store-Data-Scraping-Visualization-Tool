"""Render the offline HTML report to a full-page PNG."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


class PngExportUnavailable(RuntimeError):
    """Raised when the local Playwright Chromium runtime cannot render a PNG."""


def export_png(html_path: Path, destination: Path) -> Path:
    """Capture ``html_path`` at 1920x1080 after all Plotly charts are ready."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_png(html_path.resolve(), destination.resolve())
    except (PlaywrightError, OSError) as exc:
        raise PngExportUnavailable(f"PNG export unavailable: {exc}") from exc
    return destination


def _render_png(html_path: Path, destination: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(html_path.as_uri(), wait_until="load")
            page.wait_for_selector('[data-report-ready="true"]', timeout=60_000)
            page.screenshot(path=str(destination), full_page=True)
        finally:
            browser.close()
