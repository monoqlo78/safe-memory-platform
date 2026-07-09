"""Excel importer extracts rows and builds bilingual specs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import openpyxl
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def importer():
    return importlib.import_module("import_accounting_xlsx")


def _make_xlsx(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append(["Title", "ContentText"])
    ws.append(["源泉徴収", "給与から所得税を源泉徴収します。"])
    ws.append([None, None])  # empty row -> skipped
    ws.append(["消費税", "仕訳CSVに税区分を入力します。"])
    wb.save(path)


def test_extract_records_skips_empty_rows(importer, tmp_path):
    xlsx = tmp_path / "mini.xlsx"
    _make_xlsx(xlsx)

    ws_count, total_rows, records = importer.extract_records(
        xlsx, limit=0, max_chars=0
    )

    assert ws_count == 1
    # Two non-empty data rows (header excluded, empty row skipped).
    assert total_rows == 2
    assert len(records) == 2
    # Column names are preserved in the combined record.
    assert "Title:" in records[0]
    assert "ContentText:" in records[0]


def test_build_entries_sets_bilingual_metadata(importer, tmp_path):
    xlsx = tmp_path / "mini.xlsx"
    _make_xlsx(xlsx)
    _, _, records = importer.extract_records(xlsx, limit=0, max_chars=0)

    specs = importer.build_entries(records)

    assert len(specs) == 2
    for spec in specs:
        assert spec["source_language"] == "ja"
        assert spec["canonical_language"] == "en"
        assert spec["original_text"]
        # In fallback mode canonical text carries the untranslated marker.
        assert spec["canonical_text"].startswith("[UNTRANSLATED FALLBACK] ")
        assert spec["text"] == spec["canonical_text"]


def test_limit_caps_records(importer, tmp_path):
    xlsx = tmp_path / "mini.xlsx"
    _make_xlsx(xlsx)

    _, _, records = importer.extract_records(xlsx, limit=1, max_chars=0)
    assert len(records) == 1
