"""End-to-end English demo for the Safe Memory Platform.

Steps:
  1. Import demoknowlege/results.xlsx and build the bilingual pack.
  2. Add illustrative CONFIDENTIAL/SECRET entries (to prove safe export).
  3. Query in English.
  4. Export a safe shareable pack (PUBLIC + SHAREABLE only, sources removed).
  5. Verify CONFIDENTIAL and SECRET entries are excluded.
  6. Simulate a receipt-agent consuming the exported pack.
  7. Run an English project task.
  8. Print a concise English narrative for a 3-minute hackathon video.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from _common import bootstrap

PROJECT_ROOT = bootstrap()

import import_accounting_xlsx as importer  # noqa: E402

from app.api.packs import (  # noqa: E402
    append_entry,
    build_pack_from_entries,
    export_pack,
    query_pack,
    verify_pack,
)
from app.api.projects import run_project  # noqa: E402
from app.core import pack_io  # noqa: E402
from app.models.pack_schema import (  # noqa: E402
    AppendRequest,
    Classification,
    ExportRequest,
    QueryRequest,
    VerifyRequest,
)
from app.models.project_schema import ProjectRunRequest  # noqa: E402

AGENT_ID = "tax-agent"
PACK_ID = "jp-accounting-bilingual"
EXPORT_NAME = "jp-accounting-shareable"
IMPORT_LIMIT = 15

QUESTION = (
    "How should tax categories be represented in an accounting journal CSV "
    "for Japanese cloud accounting?"
)
PROJECT_TASK = (
    "Create guidance for a receipt OCR agent that needs to convert Japanese "
    "receipts into Money Forward Cloud journal CSV entries."
)


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def step_import() -> str:
    section("STEP 1  Import Japanese accounting Excel -> bilingual pack")
    xlsx_path = (PROJECT_ROOT / "demoknowlege" / "results.xlsx").resolve()
    if not xlsx_path.exists():
        raise SystemExit(f"Demo Excel not found: {xlsx_path}")

    ws_count, total_rows, records = importer.extract_records(
        xlsx_path, limit=IMPORT_LIMIT, max_chars=1200
    )
    print(f"Source Excel: {xlsx_path}")
    print(f"Worksheets: {ws_count} | rows scanned: {total_rows} | imported: {len(records)}")
    print("Translating/normalizing to English (Qwen or safe fallback)...")
    specs = importer.build_entries(records)

    pack, saved_path, audit_path = build_pack_from_entries(
        agent_id=AGENT_ID,
        pack_id=PACK_ID,
        title="Japanese Accounting Knowledge Pack for International Demo",
        entries=specs,
        default_classification=Classification.PUBLIC,
        method="import",
    )
    counts = Counter(e.classification.value for e in pack.entries)
    print(f"Pack: {pack_io.relpath_from_root(saved_path)}")
    print(f"Entries: {len(pack.entries)} | classifications: {dict(counts)}")
    print(f"Audit: {pack_io.relpath_from_root(audit_path)}")
    return pack_io.relpath_from_root(saved_path)


def step_add_sensitive(pack_path: str) -> None:
    section("STEP 2  Add illustrative CONFIDENTIAL/SECRET entries")
    demo_entries = [
        ("Employee salary details for the accounting team payroll run.", "CONFIDENTIAL"),
        ("Internal API key for the accounting integration: do not share.", "SECRET"),
    ]
    for text, hint in demo_entries:
        resp = append_entry(
            AppendRequest(
                agent_id=AGENT_ID,
                pack_path=pack_path,
                text=text,
                source="demo_seed",
                suggested_classification=Classification(hint),
            )
        )
        print(f"  appended {resp.classification.value} entry {resp.entry_id[:8]}")


def step_query(pack_path: str) -> None:
    section("STEP 3  Query in English")
    resp = query_pack(
        QueryRequest(agent_id=AGENT_ID, pack_path=pack_path, query=QUESTION, top_k=5)
    )
    pack = pack_io.load_pack(pack_path)
    by_id = {e.id: e for e in pack.entries}
    canonical_used = any(
        by_id.get(mid) and by_id[mid].canonical_text for mid in resp.used_memory_ids
    )
    print("Q:", QUESTION)
    print("\nANSWER:\n" + resp.answer)
    print("\nUsed memory IDs:", resp.used_memory_ids)
    print("Classifications:", [c.value for c in resp.classifications])
    print("Canonical English text used:", canonical_used)


def step_export(pack_path: str) -> str:
    section("STEP 4  Export safe shareable pack (PUBLIC + SHAREABLE only)")
    resp = export_pack(
        ExportRequest(
            agent_id=AGENT_ID,
            pack_path=pack_path,
            export_name=EXPORT_NAME,
            allowed_classifications=[Classification.PUBLIC, Classification.SHAREABLE],
            remove_sources=True,
            redact_sensitive_text=True,
        )
    )
    print("Export path:", resp.export_path)
    print("Included:", resp.included_count, "| Excluded:", resp.excluded_count)
    print("Warnings:", resp.warnings)
    return resp.export_path


def step_verify_export(export_path: str) -> None:
    section("STEP 5  Verify CONFIDENTIAL/SECRET excluded and source removed")
    export = pack_io.load_pack(export_path)
    sensitive = [
        e for e in export.entries
        if e.classification in {Classification.CONFIDENTIAL, Classification.SECRET}
    ]
    with_source = [e for e in export.entries if e.original_text]
    verify = verify_pack(VerifyRequest(pack_path=export_path))
    print("Entries in export:", len(export.entries))
    print("CONFIDENTIAL/SECRET present:", len(sensitive), "(expected 0)")
    print("Entries still carrying Japanese source:", len(with_source), "(expected 0)")
    print("Export ledger hash chain valid:", verify.valid_hash_chain)


def step_receipt_agent(export_path: str) -> None:
    section("STEP 6-7  Receipt-agent consumes shared pack + runs English task")
    resp = run_project(
        ProjectRunRequest(
            project_id="receipt-ocr-guidance",
            agent_id="receipt-agent",
            task=PROJECT_TASK,
            pack_paths=[export_path],
            top_k=5,
        )
    )
    print("TASK:", PROJECT_TASK)
    print("\nOUTPUT:\n" + resp.output)
    print("\nUsed memory IDs:", resp.used_memory_ids)
    print("Suggested new memories:")
    for s in resp.suggested_new_memories:
        print(f"  - [{s.suggested_classification.value}] {s.text}")


def step_narrative(export_path: str) -> None:
    section("STEP 8  3-minute hackathon narrative (English)")
    print(
        "Safe Memory Platform turns raw Japanese accounting knowledge from the\n"
        "National Tax Agency into a portable, policy-aware Safe Memory Pack.\n\n"
        "1. We import a Japanese Excel knowledge base. Each row keeps its original\n"
        "   Japanese text for provenance, and gets a canonical English translation\n"
        "   powered by Qwen so international judges can read it.\n\n"
        "2. Retrieval, ranking and answering all run on the English canonical text,\n"
        "   so the demo is English-first while the Japanese original stays private.\n\n"
        "3. Every entry is classified. When we export a pack to share with another\n"
        "   agent, CONFIDENTIAL and SECRET entries are excluded, the private\n"
        "   Japanese source is stripped, and sensitive text is redacted.\n\n"
        "4. A separate receipt-agent then safely consumes ONLY the shareable pack\n"
        f"   ({export_path}) to generate guidance for converting Japanese receipts\n"
        "   into Money Forward Cloud journal CSV entries.\n\n"
        "The result: portable agent memory that is auditable, tamper-evident via a\n"
        "sha256 ledger chain, and safe to share across borders and across agents."
    )


def main() -> None:
    pack_path = step_import()
    step_add_sensitive(pack_path)
    step_query(pack_path)
    export_path = step_export(pack_path)
    step_verify_export(export_path)
    step_receipt_agent(export_path)
    step_narrative(export_path)
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
