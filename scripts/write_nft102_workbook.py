"""Patch only the NFT102 worksheet XML while preserving every other XLSX part."""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
Q = f"{{{MAIN_NS}}}"
CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _column_number(reference: str) -> int:
    match = CELL_REF.fullmatch(reference)
    if match is None:
        raise ValueError(f"无效单元格地址：{reference}")
    number = 0
    for char in match.group(1):
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _cell(row: ET.Element, column_letter: str) -> ET.Element | None:
    target = int(row.attrib["r"])
    return next(
        (
            item
            for item in row.findall(f"{Q}c")
            if item.attrib.get("r") == f"{column_letter}{target}"
        ),
        None,
    )


def _ensure_cell(row: ET.Element, column_letter: str, style_cell: ET.Element | None) -> ET.Element:
    existing = _cell(row, column_letter)
    if existing is not None:
        return existing
    reference = f"{column_letter}{row.attrib['r']}"
    attributes = {"r": reference}
    if style_cell is not None and "s" in style_cell.attrib:
        attributes["s"] = style_cell.attrib["s"]
    created = ET.Element(f"{Q}c", attributes)
    target_column = _column_number(reference)
    for index, candidate in enumerate(row.findall(f"{Q}c")):
        if _column_number(candidate.attrib["r"]) > target_column:
            row.insert(index, created)
            break
    else:
        row.append(created)
    return created


def _clear(cell: ET.Element) -> None:
    cell.attrib.pop("t", None)
    for tag in ("f", "v", "is"):
        child = cell.find(f"{Q}{tag}")
        if child is not None:
            cell.remove(child)


def _set_number(cell: ET.Element, value: int | float) -> None:
    _clear(cell)
    node = ET.SubElement(cell, f"{Q}v")
    node.text = str(value)


def _set_formula(cell: ET.Element, formula: str, cached_value: int) -> None:
    _clear(cell)
    formula_node = ET.SubElement(cell, f"{Q}f")
    formula_node.text = formula
    value_node = ET.SubElement(cell, f"{Q}v")
    value_node.text = str(cached_value)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(f"{Q}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{Q}t")))
    return values


def _cell_text(cell: ET.Element | None, shared_strings: list[str]) -> str | None:
    if cell is None:
        return None
    if cell.attrib.get("t") == "inlineStr":
        inline = cell.find(f"{Q}is")
        if inline is None:
            return None
        return "".join(node.text or "" for node in inline.iter(f"{Q}t"))
    value = cell.find(f"{Q}v")
    if value is None or value.text is None:
        return None
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)]
    return value.text


def _sheet_part(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = next(
        (
            item
            for item in workbook.findall(f"{Q}sheets/{Q}sheet")
            if item.attrib.get("name") == sheet_name
        ),
        None,
    )
    if sheet is None:
        raise ValueError(f"工作簿中不存在 {sheet_name} 工作表")
    relationship_id = sheet.attrib[f"{{{DOC_REL_NS}}}id"]
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationship = next(
        item
        for item in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        if item.attrib.get("Id") == relationship_id
    )
    target = relationship.attrib["Target"].lstrip("/")
    if target.startswith("xl/"):
        return target
    return str(PurePosixPath("xl") / target)


def _excel_date(value: str) -> date:
    return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()


def _find_or_append_block(
    root: ET.Element, shared_strings: list[str], report_date: date
) -> tuple[int, dict[int, ET.Element]]:
    sheet_data = root.find(f"{Q}sheetData")
    if sheet_data is None:
        raise ValueError("NFT102 工作表缺少 sheetData")
    rows = {int(row.attrib["r"]): row for row in sheet_data.findall(f"{Q}row")}
    starts: list[int] = []
    for row_number, row in rows.items():
        if _cell_text(_cell(row, "A"), shared_strings) != "访客总数":
            continue
        date_text = _cell_text(_cell(row, "B"), shared_strings)
        if date_text is None:
            continue
        starts.append(row_number)
        if _excel_date(date_text) == report_date:
            return row_number, rows

    if not starts:
        raise ValueError("NFT102 中未找到可复制的4行日报区块")
    source_start = max(starts)
    target_start = source_start + 4

    # Some WPS files keep a styled blank cell below the visible data. Move such
    # trailing rows down instead of overwriting or deleting them.
    for old_number in sorted((number for number in rows if number >= target_start), reverse=True):
        trailing = rows.pop(old_number)
        new_number = old_number + 4
        trailing.attrib["r"] = str(new_number)
        for cell in trailing.findall(f"{Q}c"):
            match = CELL_REF.fullmatch(cell.attrib["r"])
            if match is not None:
                cell.attrib["r"] = f"{match.group(1)}{new_number}"
        rows[new_number] = trailing

    for offset in range(4):
        source = rows[source_start + offset]
        cloned = copy.deepcopy(source)
        new_row_number = target_start + offset
        cloned.attrib["r"] = str(new_row_number)
        for cell in cloned.findall(f"{Q}c"):
            match = CELL_REF.fullmatch(cell.attrib["r"])
            if match is None:
                continue
            cell.attrib["r"] = f"{match.group(1)}{new_row_number}"
        sheet_data.append(cloned)
        rows[new_row_number] = cloned

    ordered_rows = sorted(sheet_data.findall(f"{Q}row"), key=lambda item: int(item.attrib["r"]))
    for row in sheet_data.findall(f"{Q}row"):
        sheet_data.remove(row)
    sheet_data.extend(ordered_rows)

    dimension = root.find(f"{Q}dimension")
    if dimension is not None:
        start_ref, _, end_ref = dimension.attrib.get("ref", "A1").partition(":")
        end_match = CELL_REF.fullmatch(end_ref or start_ref)
        if end_match is not None:
            dimension.attrib["ref"] = f"{start_ref}:{end_match.group(1)}{max(rows)}"
    return target_start, rows


def patch_workbook(source: Path, output: Path, payload: dict[str, Any]) -> None:
    """Copy an XLSX and replace only its NFT102 worksheet XML part."""
    report_date = date.fromisoformat(payload["report_date"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as source_zip:
        worksheet_part = _sheet_part(source_zip, payload["sheet_name"])
        shared_strings = _shared_strings(source_zip)
        worksheet_root = ET.fromstring(source_zip.read(worksheet_part))
        target_start, rows = _find_or_append_block(
            worksheet_root, shared_strings, report_date
        )

        traffic_row = rows[target_start]
        daily_traffic_row = rows[target_start + 1]
        order_row = rows[target_start + 2]
        stock_row = rows[target_start + 3]
        prior_rows = {
            0: rows.get(target_start - 4),
            1: rows.get(target_start - 3),
            2: rows.get(target_start - 2),
            3: rows.get(target_start - 1),
        }

        date_cell = _ensure_cell(traffic_row, "B", _cell(prior_rows[0], "B") if prior_rows[0] else None)
        _set_number(date_cell, (report_date - date(1899, 12, 30)).days)
        _clear(_ensure_cell(daily_traffic_row, "B", _cell(prior_rows[1], "B") if prior_rows[1] else None))
        _clear(_ensure_cell(stock_row, "B", _cell(prior_rows[3], "B") if prior_rows[3] else None))

        for item in payload["columns"]:
            letter = item["column_letter"]
            traffic_cell = _ensure_cell(
                traffic_row, letter, _cell(prior_rows[0], letter) if prior_rows[0] else None
            )
            daily_cell = _ensure_cell(
                daily_traffic_row, letter, _cell(prior_rows[1], letter) if prior_rows[1] else None
            )
            order_cell = _ensure_cell(
                order_row, letter, _cell(prior_rows[2], letter) if prior_rows[2] else None
            )
            stock_cell = _ensure_cell(
                stock_row, letter, _cell(prior_rows[3], letter) if prior_rows[3] else None
            )
            if item["traffic_value"] is None:
                _clear(traffic_cell)
            else:
                _set_number(traffic_cell, int(item["traffic_value"]))
            _clear(daily_cell)
            if item["order_value"] is None:
                _clear(order_cell)
            else:
                _set_number(order_cell, int(item["order_value"]))
            if item["platform_stock_value"] is None:
                _clear(stock_cell)
            else:
                _set_number(stock_cell, int(item["platform_stock_value"]))

        total = int(payload["summary"]["ordered_units_mapped"])
        order_total_cell = _ensure_cell(
            order_row, "B", _cell(prior_rows[2], "B") if prior_rows[2] else None
        )
        last_column = payload["max_column_letter"]
        _set_formula(order_total_cell, f"SUM(C{target_start + 2}:{last_column}{target_start + 2})", total)
        updated_xml = ET.tostring(worksheet_root, encoding="utf-8", xml_declaration=True)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp_file:
            temp_path = Path(temp_file.name)
        try:
            with zipfile.ZipFile(temp_path, "w") as output_zip:
                for entry in source_zip.infolist():
                    content = updated_xml if entry.filename == worksheet_part else source_zip.read(entry.filename)
                    output_zip.writestr(entry, content)
            shutil.move(temp_path, output)
        finally:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    patch_workbook(args.source, args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
