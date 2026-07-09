"""Import a Japanese accounting Excel file into a bilingual Safe Memory Pack.

Usage:
    python scripts/import_accounting_xlsx.py --input "demoknowlege/results.xlsx"

Reads every worksheet, extracts non-empty rows, translates/normalizes each row
into canonical English via Qwen (with a safe fallback), and builds a Safe
Memory Pack. The original Japanese text is preserved as provenance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from _common import bootstrap

PROJECT_ROOT = bootstrap()

# Imports that rely on SAFE_MEMORY_ROOT / sys.path must come after bootstrap().
import openpyxl  # noqa: E402

from app.api.packs import build_pack_from_entries  # noqa: E402
from app.core import pack_io  # noqa: E402
from app.core.translation import normalize_accounting_batch  # noqa: E402
from app.models.pack_schema import Classification  # noqa: E402

AGENT_ID = "tax-agent"
PACK_ID = "jp-accounting-bilingual"
PACK_TITLE = "Japanese Accounting Knowledge Pack for International Demo"


def _row_to_original_text(headers: Optional[List[str]], row) -> str:
    """Combine non-empty cell values of a row into one labeled record."""
    parts: List[str] = []
    for idx, cell in enumerate(row):
        if cell is None:
            continue
        value = str(cell).strip()
        if not value:
            continue
        if headers and idx < len(headers) and headers[idx]:
            parts.append(f"{headers[idx]}: {value}")
        else:
            parts.append(value)
    return "\n".join(parts)


def extract_records(
    xlsx_path: Path,
    limit: int,
    max_chars: int,
) -> tuple[int, int, List[str]]:
    """Return (worksheet_count, total_non_empty_rows, records)."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    records: List[str] = []
    total_rows = 0

    for ws in wb.worksheets:
        headers: Optional[List[str]] = None
        for r_index, row in enumerate(ws.iter_rows(values_only=True)):
            non_empty = [c for c in row if c is not None and str(c).strip()]
            if not non_empty:
                continue
            if r_index == 0 and headers is None:
                # Treat the first non-empty row as a header row.
                headers = [str(c).strip() if c is not None else "" for c in row]
                continue
            total_rows += 1
            original = _row_to_original_text(headers, row)
            if not original:
                continue
            if max_chars > 0 and len(original) > max_chars:
                original = original[:max_chars]
            records.append(original)
            if limit > 0 and len(records) >= limit:
                wb.close()
                return len(wb.worksheets), total_rows, records

    worksheet_count = len(wb.worksheets)
    wb.close()
    return worksheet_count, total_rows, records


def build_entries(records: List[str]) -> List[Dict[str, object]]:
    """Translate all records in batched Qwen calls and build bilingual specs."""
    from app.config import settings

    total = len(records)
    print(f"  translating {total} rows in batches of {settings.translation_batch_size}...")
    canonicals = normalize_accounting_batch(
        records, batch_size=settings.translation_batch_size
    )

    specs: List[Dict[str, object]] = []
    for original, canonical in zip(records, canonicals):
        note = (
            "Auto-translated from Japanese via Qwen."
            if not canonical.startswith("[UNTRANSLATED FALLBACK]")
            else "Qwen unavailable; canonical text is a labeled fallback."
        )
        specs.append(
            {
                "text": canonical,
                "original_text": original,
                "canonical_text": canonical,
                "source_language": "ja",
                "canonical_language": "en",
                "translation_note": note,
                "source": "import_accounting_xlsx",
            }
        )
    print(f"  translated {total}/{total}")
    return specs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a Japanese accounting Excel file into a bilingual pack."
    )
    parser.add_argument("--input", required=True, help="Path to the .xlsx file.")
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max records to import (0 = all). Default 25 keeps the demo fast.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1200,
        help="Max characters per source record before translation.",
    )
    parser.add_argument(
        "--default-classification",
        default="PUBLIC",
        help="Fallback classification for public NTA data (default PUBLIC).",
    )
    args = parser.parse_args()

    xlsx_path = Path(args.input)
    if not xlsx_path.is_absolute():
        xlsx_path = (PROJECT_ROOT / xlsx_path).resolve()
    if not xlsx_path.exists():
        raise SystemExit(f"Input Excel file not found: {xlsx_path}")

    try:
        default_class = Classification(args.default_classification.upper())
    except ValueError:
        default_class = Classification.PUBLIC

    print(f"Source Excel: {xlsx_path}")
    worksheet_count, total_rows, records = extract_records(
        xlsx_path, args.limit, args.max_chars
    )
    print(f"Worksheets: {worksheet_count}")
    print(f"Non-empty data rows scanned: {total_rows}")
    print(f"Records selected for import: {len(records)}")

    if not records:
        raise SystemExit("No records extracted from the Excel file.")

    print("Translating/normalizing to canonical English...")
    specs = build_entries(records)

    pack, saved_path, audit_path = build_pack_from_entries(
        agent_id=AGENT_ID,
        pack_id=PACK_ID,
        title=PACK_TITLE,
        entries=specs,
        default_classification=default_class,
        method="import",
    )

    counts = Counter(e.classification.value for e in pack.entries)

    print()
    print("=== Import complete ===")
    print(f"Created pack path: {pack_io.relpath_from_root(saved_path)}")
    print(f"Entry count: {len(pack.entries)}")
    print(f"Classification counts: {dict(counts)}")
    print(f"Audit report path: {pack_io.relpath_from_root(audit_path)}")


if __name__ == "__main__":
    main()
