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
from typing import Any, cast
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
Q = f"{{{MAIN_NS}}}"
CELL_REF = re.compile(r"([A-Z]+)(\d+)")
ORDER_HIGHLIGHT_RGB = "FBE5D6"
INVENTORY_ALERT_RGB = "FFFF0000"


def _style_signature(style: ET.Element) -> bytes:
    """Return an xf signature that ignores only its fill selection."""
    comparable = copy.deepcopy(style)
    comparable.attrib.pop("fillId", None)
    comparable.attrib.pop("applyFill", None)
    return cast(bytes, ET.tostring(comparable, encoding="utf-8"))


def _inventory_style_signature(style: ET.Element) -> bytes:
    """Return an xf signature that ignores the inventory alert border and fill."""
    comparable = copy.deepcopy(style)
    comparable.attrib.pop("borderId", None)
    comparable.attrib.pop("applyBorder", None)
    comparable.attrib.pop("fillId", None)
    comparable.attrib.pop("applyFill", None)
    return cast(bytes, ET.tostring(comparable, encoding="utf-8"))


def _find_fill_id(styles_root: ET.Element, rgb: str) -> int:
    fills = styles_root.find(f"{Q}fills")
    if fills is None:
        raise ValueError("工作簿样式表缺少 fills")
    target = rgb.upper().removeprefix("FF")
    for index, fill in enumerate(fills.findall(f"{Q}fill")):
        foreground = fill.find(f"{Q}patternFill/{Q}fgColor")
        candidate = foreground.attrib.get("rgb", "") if foreground is not None else ""
        if candidate.upper().removeprefix("FF") == target:
            return index
    raise ValueError(f"工作簿中未找到订单高亮色 #{rgb}")


class _OrderStyleResolver:
    """Select matching normal/highlight xf styles without changing other formatting."""

    def __init__(self, styles_root: ET.Element) -> None:
        self.styles_root = styles_root
        cell_xfs = styles_root.find(f"{Q}cellXfs")
        if cell_xfs is None:
            raise ValueError("工作簿样式表缺少 cellXfs")
        self.cell_xfs: ET.Element = cell_xfs
        self.highlight_fill_id = _find_fill_id(styles_root, ORDER_HIGHLIGHT_RGB)
        self.changed = False

    def _styles(self) -> list[ET.Element]:
        return self.cell_xfs.findall(f"{Q}xf")

    def style_for(self, current_style_id: int, highlighted: bool) -> int:
        styles = self._styles()
        if current_style_id < 0 or current_style_id >= len(styles):
            raise ValueError(f"无效的单元格样式 ID: {current_style_id}")
        current = styles[current_style_id]
        signature = _style_signature(current)
        desired_fill_id = self.highlight_fill_id if highlighted else 0

        for index, candidate in enumerate(styles):
            if (
                int(candidate.attrib.get("fillId", "0")) == desired_fill_id
                and _style_signature(candidate) == signature
            ):
                return index

        # Some custom templates may not contain both variants. Add the missing
        # xf while retaining the font, border, number format and alignment.
        created = copy.deepcopy(current)
        created.attrib["fillId"] = str(desired_fill_id)
        created.attrib["applyFill"] = "1"
        self.cell_xfs.append(created)
        self.cell_xfs.attrib["count"] = str(len(self._styles()))
        self.changed = True
        return len(self._styles()) - 1


class _InventoryAlertStyleResolver:
    """Select normal/red-fill stock styles with the original border appearance."""

    def __init__(self, styles_root: ET.Element) -> None:
        cell_xfs = styles_root.find(f"{Q}cellXfs")
        if cell_xfs is None:
            raise ValueError("工作簿样式表缺少 cellXfs")
        self.cell_xfs: ET.Element = cell_xfs
        self.changed = False
        self.alert_fill_id = self._find_or_add_alert_fill(styles_root)

    def _styles(self) -> list[ET.Element]:
        return self.cell_xfs.findall(f"{Q}xf")

    def _find_or_add_alert_fill(self, styles_root: ET.Element) -> int:
        fills = styles_root.find(f"{Q}fills")
        if fills is None:
            raise ValueError("工作簿样式表缺少 fills")
        existing = fills.findall(f"{Q}fill")
        for index, fill in enumerate(existing):
            pattern = fill.find(f"{Q}patternFill")
            foreground = pattern.find(f"{Q}fgColor") if pattern is not None else None
            if (
                pattern is not None
                and pattern.attrib.get("patternType") == "solid"
                and foreground is not None
                and foreground.attrib.get("rgb", "").upper() == INVENTORY_ALERT_RGB
            ):
                return index

        fill = ET.Element(f"{Q}fill")
        pattern = ET.SubElement(fill, f"{Q}patternFill", {"patternType": "solid"})
        ET.SubElement(pattern, f"{Q}fgColor", {"rgb": INVENTORY_ALERT_RGB})
        ET.SubElement(pattern, f"{Q}bgColor", {"indexed": "64"})
        fills.append(fill)
        fills.attrib["count"] = str(len(existing) + 1)
        self.changed = True
        return len(existing)

    def style_for(self, current_style_id: int, alerted: bool) -> int:
        styles = self._styles()
        if current_style_id < 0 or current_style_id >= len(styles):
            raise ValueError(f"无效的单元格样式 ID: {current_style_id}")
        current = styles[current_style_id]
        signature = _inventory_style_signature(current)
        # Stock cells use the workbook's default border (ID 0). Keeping that
        # border lets Excel/WPS show the same grey gridline as neighbouring cells.
        desired_border_id = 0
        desired_fill_id = self.alert_fill_id if alerted else 0
        for index, candidate in enumerate(styles):
            if (
                int(candidate.attrib.get("borderId", "0")) == desired_border_id
                and int(candidate.attrib.get("fillId", "0")) == desired_fill_id
                and _inventory_style_signature(candidate) == signature
            ):
                return index

        created = copy.deepcopy(current)
        created.attrib["borderId"] = str(desired_border_id)
        created.attrib["applyBorder"] = "1"
        created.attrib["fillId"] = str(desired_fill_id)
        created.attrib["applyFill"] = "1"
        self.cell_xfs.append(created)
        self.cell_xfs.attrib["count"] = str(len(self._styles()))
        self.changed = True
        return len(self._styles()) - 1


class _CenteredStyleResolver:
    """Create centered variants of existing cell styles without changing other formatting."""

    def __init__(self, styles_root: ET.Element) -> None:
        cell_xfs = styles_root.find(f"{Q}cellXfs")
        if cell_xfs is None:
            raise ValueError("工作簿样式表缺少 cellXfs")
        self.cell_xfs: ET.Element = cell_xfs
        self.changed = False
        self._resolved: dict[int, int] = {}

    def _styles(self) -> list[ET.Element]:
        return self.cell_xfs.findall(f"{Q}xf")

    def style_for(self, current_style_id: int) -> int:
        resolved = self._resolved.get(current_style_id)
        if resolved is not None:
            return resolved
        styles = self._styles()
        if current_style_id < 0 or current_style_id >= len(styles):
            raise ValueError(f"无效的单元格样式 ID: {current_style_id}")
        current = styles[current_style_id]
        alignment = current.find(f"{Q}alignment")
        if (
            alignment is not None
            and alignment.attrib.get("horizontal") == "center"
            and alignment.attrib.get("vertical") == "center"
        ):
            self._resolved[current_style_id] = current_style_id
            return current_style_id

        centered = copy.deepcopy(current)
        centered.attrib["applyAlignment"] = "1"
        alignment = centered.find(f"{Q}alignment")
        if alignment is None:
            alignment = ET.Element(f"{Q}alignment")
            insert_at = len(centered)
            for index, child in enumerate(centered):
                if child.tag in {f"{Q}protection", f"{Q}extLst"}:
                    insert_at = index
                    break
            centered.insert(insert_at, alignment)
        alignment.attrib["horizontal"] = "center"
        alignment.attrib["vertical"] = "center"
        self.cell_xfs.append(centered)
        self.cell_xfs.attrib["count"] = str(len(self._styles()))
        centered_style_id = len(self._styles()) - 1
        self._resolved[current_style_id] = centered_style_id
        self.changed = True
        return centered_style_id


def _apply_order_style(
    cell: ET.Element, value: int | None, resolver: _OrderStyleResolver
) -> None:
    current_style_id = int(cell.attrib.get("s", "0"))
    cell.attrib["s"] = str(
        resolver.style_for(current_style_id, highlighted=value is not None and value > 0)
    )


def _apply_inventory_alert_style(
    cell: ET.Element, alerted: bool, resolver: _InventoryAlertStyleResolver
) -> None:
    current_style_id = int(cell.attrib.get("s", "0"))
    cell.attrib["s"] = str(resolver.style_for(current_style_id, alerted))


def _center_worksheet_cells(
    worksheet_root: ET.Element, resolver: _CenteredStyleResolver
) -> None:
    sheet_data = worksheet_root.find(f"{Q}sheetData")
    if sheet_data is None:
        raise ValueError("NFT102 工作表缺少 sheetData")
    for cell in sheet_data.iter(f"{Q}c"):
        current_style_id = int(cell.attrib.get("s", "0"))
        cell.attrib["s"] = str(resolver.style_for(current_style_id))


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


def _cell_number(cell: ET.Element | None) -> float | None:
    if cell is None or cell.attrib.get("t") in {"inlineStr", "s", "str"}:
        return None
    value = cell.find(f"{Q}v")
    if value is None or value.text is None:
        return None
    try:
        return float(value.text)
    except ValueError:
        return None


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
        styles_root = ET.fromstring(source_zip.read("xl/styles.xml"))
        order_style_resolver = _OrderStyleResolver(styles_root)
        inventory_alert_style_resolver = _InventoryAlertStyleResolver(styles_root)
        centered_style_resolver = _CenteredStyleResolver(styles_root)
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
            order_value = item["order_value"]
            if order_value is None:
                _clear(order_cell)
            else:
                order_value = int(order_value)
                _set_number(order_cell, order_value)
            _apply_order_style(order_cell, order_value, order_style_resolver)
            platform_stock_value = item["platform_stock_value"]
            if platform_stock_value is None:
                _clear(stock_cell)
            else:
                platform_stock_value = int(platform_stock_value)
                _set_number(stock_cell, platform_stock_value)
            previous_stock_value = _cell_number(
                _cell(prior_rows[3], letter) if prior_rows[3] is not None else None
            )
            inventory_alerted = (
                previous_stock_value is not None
                and order_value is not None
                and platform_stock_value is not None
                and previous_stock_value - order_value != platform_stock_value
            )
            _apply_inventory_alert_style(
                stock_cell, inventory_alerted, inventory_alert_style_resolver
            )

        total = int(payload["summary"]["ordered_units_mapped"])
        order_total_cell = _ensure_cell(
            order_row, "B", _cell(prior_rows[2], "B") if prior_rows[2] else None
        )
        last_column = payload["max_column_letter"]
        _set_formula(order_total_cell, f"SUM(C{target_start + 2}:{last_column}{target_start + 2})", total)
        _center_worksheet_cells(worksheet_root, centered_style_resolver)
        updated_xml = ET.tostring(worksheet_root, encoding="utf-8", xml_declaration=True)
        updated_styles_xml = (
            ET.tostring(styles_root, encoding="utf-8", xml_declaration=True)
            if (
                order_style_resolver.changed
                or inventory_alert_style_resolver.changed
                or centered_style_resolver.changed
            )
            else None
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp_file:
            temp_path = Path(temp_file.name)
        try:
            with zipfile.ZipFile(temp_path, "w") as output_zip:
                for entry in source_zip.infolist():
                    if entry.filename == worksheet_part:
                        content = updated_xml
                    elif entry.filename == "xl/styles.xml" and updated_styles_xml is not None:
                        content = updated_styles_xml
                    else:
                        content = source_zip.read(entry.filename)
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
