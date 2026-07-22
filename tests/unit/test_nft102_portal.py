from __future__ import annotations

import subprocess
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from takealot_ops.nft102_portal import (
    generate_nft102_from_baseline,
    inspect_nft102_upload,
    persist_nft102_baseline,
)


def _workbook_bytes(*, sheet_name: str = "NFT102") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet["A1"] = "访客"
    worksheet["B1"] = "日期"
    worksheet["C1"] = "商品 A\n9902242608529"
    worksheet["A2"] = "访客总数"
    worksheet["B2"] = date(2026, 7, 21)
    worksheet["A6"] = "访客总数"
    worksheet["B6"] = "2026-07-22"
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_inspect_upload_finds_latest_date_and_next_day() -> None:
    content = _workbook_bytes()

    inspection = inspect_nft102_upload("运营最终版.xlsx", content)

    assert inspection.filename == "运营最终版.xlsx"
    assert inspection.latest_report_date == date(2026, 7, 22)
    assert inspection.suggested_report_date == date(2026, 7, 23)
    assert inspection.product_columns == 1
    assert inspection.size_bytes == len(content)
    assert len(inspection.sha256) == 64


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("wrong.xls", b"not-xlsx", "只支持上传"),
        ("broken.xlsx", b"not-xlsx", "有效的 .xlsx"),
        ("empty.xlsx", b"", "为空"),
    ],
)
def test_inspect_upload_rejects_invalid_files(
    filename: str, content: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        inspect_nft102_upload(filename, content)


def test_inspect_upload_requires_nft102_sheet() -> None:
    with pytest.raises(ValueError, match="缺少 NFT102"):
        inspect_nft102_upload("other.xlsx", _workbook_bytes(sheet_name="其他店铺"))


def test_persist_baseline_archives_exact_original_bytes(tmp_path: Path) -> None:
    content = _workbook_bytes()
    inspection = inspect_nft102_upload("运营最终版.xlsx", content)

    saved = persist_nft102_baseline(
        tmp_path,
        inspection,
        content,
        saved_at=datetime(2026, 7, 22, 18, 30),
    )

    assert saved.read_bytes() == content
    assert saved.name == "运营最终版.xlsx"
    assert saved.parent.name == f"20260722-183000-000000-{inspection.sha256[:8]}"


def test_persist_baseline_detects_content_change(tmp_path: Path) -> None:
    content = _workbook_bytes()
    inspection = inspect_nft102_upload("运营最终版.xlsx", content)

    with pytest.raises(ValueError, match="发生变化"):
        persist_nft102_baseline(tmp_path, inspection, content + b"changed")


def test_generate_uses_explicit_baseline_and_finds_created_artifacts(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "update_nft102_daily.ps1"
    script.parent.mkdir()
    script.write_text("# fake", encoding="utf-8")
    baseline = tmp_path / "data" / "nft102-baselines" / "source.xlsx"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(_workbook_bytes())
    report_date = date(2026, 7, 23)
    captured: dict[str, Any] = {}

    def fake_runner(command: list[str] | tuple[str, ...], **kwargs: Any):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        folder = tmp_path / "outputs" / "nft102-daily" / report_date.isoformat()
        folder.mkdir(parents=True)
        output = folder / "运营最终版_NFT102_2026-07-23.xlsx"
        output.write_bytes(_workbook_bytes())
        output.with_suffix(".核对报告.json").write_text("{}", encoding="utf-8")
        output.with_suffix(".核对报告.txt").write_text("核对完成", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = generate_nft102_from_baseline(
        tmp_path, baseline, report_date, runner=fake_runner
    )

    command = captured["command"]
    assert command[command.index("-TemplatePath") + 1] == str(baseline)
    assert command[command.index("-ReportDate") + 1] == "2026-07-23"
    assert captured["kwargs"]["cwd"] == tmp_path.resolve()
    assert result.baseline_path == baseline.resolve()
    assert result.workbook_path.is_file()
    assert result.audit_json_path.is_file()
    assert result.audit_text_path.read_text(encoding="utf-8") == "核对完成"


def test_generate_rejects_baseline_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-nft102.xlsx"
    outside.write_bytes(_workbook_bytes())
    try:
        with pytest.raises(ValueError, match="不在项目目录"):
            generate_nft102_from_baseline(tmp_path, outside, date(2026, 7, 23))
    finally:
        outside.unlink(missing_ok=True)
