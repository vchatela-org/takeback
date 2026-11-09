"""A minimal reader and writer for the Office Open XML workbooks (.xlsx) some
exports ship instead of CSV files (AliExpress's, app/importers/aliexpress.py).

Only the standard library is used: an .xlsx is a zip of XML parts, and what an
export needs from one -- each sheet's cells, as text -- is a small part of the
format. No formula is evaluated, and no number format applied: a date written
as text stays that text (AliExpress writes every date as one), and a number
cell comes back as the digits stored in it.

The workbook is untrusted input, read from an archive that has already been
extracted under app/limits.py's budget -- but an .xlsx is itself a zip, so it
gets its own: every part is read through a byte counter rather than trusting
the sizes the workbook's central directory declares.
"""
import io
import re
import zipfile
from pathlib import Path
from typing import Dict, IO, Iterator, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# What one part of a workbook may expand to. A real export's largest sheet is
# a few megabytes; this only exists so a crafted one cannot fill memory.
MAX_PART_BYTES = 256 * 1024 ** 2

# XML 1.0 forbids most control characters, even escaped: a cell holding one
# would make the written workbook unreadable.
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class WorkbookError(Exception):
    """The file is not a workbook this module can read."""


class _CappedReader(io.RawIOBase):
    """Counts what is actually decompressed, and stops past MAX_PART_BYTES."""

    def __init__(self, raw: IO[bytes], name: str):
        self._raw, self._name, self._read = raw, name, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(len(buffer))
        self._read += len(data)
        if self._read > MAX_PART_BYTES:
            raise WorkbookError(f"{self._name}: part larger than {MAX_PART_BYTES} bytes")
        buffer[:len(data)] = data
        return len(data)


def _open(zf: zipfile.ZipFile, name: str) -> IO[bytes]:
    return io.BufferedReader(_CappedReader(zf.open(name), name))


def _column_index(ref: Optional[str]) -> Optional[int]:
    """"AC12" -> 28 (0-based); None for a cell with no reference."""
    if not ref:
        return None
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1 if index else None


def _text_of(element: ET.Element) -> str:
    """Every <t> under a shared or inline string, rich-text runs included
    (a run is one <r><t> per formatting change within the same cell)."""
    return "".join(node.text or "" for node in element.iter(f"{_MAIN}t"))


def _shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings: List[str] = []
    with _open(zf, "xl/sharedStrings.xml") as handle:
        for _event, element in ET.iterparse(handle):
            if element.tag == f"{_MAIN}si":
                strings.append(_text_of(element))
                element.clear()
    return strings


def _sheet_parts(zf: zipfile.ZipFile) -> List[tuple]:
    """[(sheet name, part path)], in the workbook's own order."""
    with _open(zf, "xl/workbook.xml") as handle:
        workbook = ET.parse(handle).getroot()
    targets: Dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        with _open(zf, "xl/_rels/workbook.xml.rels") as handle:
            for rel in ET.parse(handle).getroot().iter(f"{_PKG_REL}Relationship"):
                target = rel.get("Target") or ""
                # Relative to xl/ ("worksheets/sheet1.xml"), or absolute from
                # the package root ("/xl/worksheets/sheet1.xml").
                targets[rel.get("Id") or ""] = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    parts = []
    sheets = workbook.find(f"{_MAIN}sheets")
    for position, sheet in enumerate(sheets if sheets is not None else [], start=1):
        part = targets.get(sheet.get(f"{_REL}id") or "") or f"xl/worksheets/sheet{position}.xml"
        if part in zf.namelist():
            parts.append((sheet.get("name") or f"Sheet{position}", part))
    return parts


def _rows(zf: zipfile.ZipFile, part: str, strings: List[str]) -> Iterator[List[str]]:
    """One list of cell texts per row, gaps filled with "" (a cell left empty
    is not written at all, so position comes from its reference)."""
    with _open(zf, part) as handle:
        for _event, element in ET.iterparse(handle):
            if element.tag != f"{_MAIN}row":
                continue
            cells: Dict[int, str] = {}
            for position, cell in enumerate(element.findall(f"{_MAIN}c")):
                index = _column_index(cell.get("r"))
                index = position if index is None else index
                kind = cell.get("t")
                value = cell.find(f"{_MAIN}v")
                raw = value.text if value is not None and value.text is not None else ""
                if kind == "s":
                    try:
                        text = strings[int(raw)]
                    except (ValueError, IndexError):
                        text = ""
                elif kind == "inlineStr":
                    inline = cell.find(f"{_MAIN}is")
                    text = _text_of(inline) if inline is not None else ""
                elif kind == "b":
                    text = "true" if raw == "1" else "false" if raw == "0" else raw
                else:  # n (number), str (a formula's cached text), e (error), d
                    text = raw
                cells[index] = text
            element.clear()
            yield [cells.get(i, "") for i in range(max(cells) + 1)] if cells else []


def read_sheets(path: Path) -> Dict[str, List[List[str]]]:
    """{sheet name: its rows, every cell as text}, header row included."""
    try:
        with zipfile.ZipFile(path) as zf:
            strings = _shared_strings(zf)
            return {name: list(_rows(zf, part, strings)) for name, part in _sheet_parts(zf)}
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise WorkbookError(f"{path.name}: {exc}") from exc


def records(rows: List[List[str]]) -> List[Dict[str, str]]:
    """A sheet's rows as dicts keyed by its first non-empty row. A row with
    every cell empty is skipped; a column with no name is dropped."""
    header: Optional[List[str]] = None
    out: List[Dict[str, str]] = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        if header is None:
            header = [cell.strip() for cell in row]
            continue
        out.append({name: (row[i] if i < len(row) else "") for i, name in enumerate(header) if name})
    return out


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _clean(value: str) -> str:
    return escape(_INVALID_XML_CHARS.sub("", value), {'"': "&quot;"})


def write_workbook(sheets: Dict[str, List[List[str]]]) -> bytes:
    """A workbook holding these sheets, every cell an inline string -- the
    smallest valid shape, enough for read_sheets (and a spreadsheet app) to
    open. Used by app/anonymize.py to write a scrubbed copy of an export's
    workbook back in the format the importer reads."""
    out = io.BytesIO()
    names = list(sheets)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(names) + 1)
            )
            + "</Types>"
        ))
        zf.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        ))
        zf.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{_clean(name[:31])}" sheetId="{i}" r:id="rId{i}"/>'
                for i, name in enumerate(names, start=1)
            )
            + "</sheets></workbook>"
        ))
        zf.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(names) + 1)
            )
            + "</Relationships>"
        ))
        for i, name in enumerate(names, start=1):
            body = io.StringIO()
            body.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
            for r, row in enumerate(sheets[name], start=1):
                body.write(f'<row r="{r}">')
                for c, value in enumerate(row):
                    if value:
                        body.write(f'<c r="{_column_letter(c)}{r}" t="inlineStr"><is><t xml:space="preserve">'
                                   f"{_clean(str(value))}</t></is></c>")
                body.write("</row>")
            body.write("</sheetData></worksheet>")
            zf.writestr(f"xl/worksheets/sheet{i}.xml", body.getvalue())
    return out.getvalue()
