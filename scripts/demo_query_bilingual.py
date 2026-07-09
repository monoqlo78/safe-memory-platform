"""English demo: query the bilingual Japanese accounting pack.

Run the importer first:
    python scripts/import_accounting_xlsx.py --input "demoknowlege/results.xlsx"
Then:
    python scripts/demo_query_bilingual.py
"""

from __future__ import annotations

from _common import bootstrap

bootstrap()

from app.api.packs import query_pack  # noqa: E402
from app.core import pack_io  # noqa: E402
from app.models.pack_schema import QueryRequest  # noqa: E402

AGENT_ID = "tax-agent"
PACK_ID = "jp-accounting-bilingual"
QUESTION = (
    "How should tax categories be represented in an accounting journal CSV "
    "for Japanese cloud accounting?"
)


def run_query() -> None:
    path = pack_io.find_pack_by_id(AGENT_ID, PACK_ID)
    if path is None:
        raise SystemExit(
            "Pack not found. Run scripts/import_accounting_xlsx.py first."
        )

    resp = query_pack(
        QueryRequest(
            agent_id=AGENT_ID,
            pack_path=pack_io.relpath_from_root(path),
            query=QUESTION,
            top_k=5,
        )
    )

    pack = pack_io.load_pack(path)
    by_id = {e.id: e for e in pack.entries}
    canonical_used = any(
        by_id.get(mid) and by_id[mid].canonical_text
        for mid in resp.used_memory_ids
    )

    print("Q:", QUESTION)
    print()
    print("ANSWER (English-first):")
    print(resp.answer)
    print()
    print("Used memory IDs:", resp.used_memory_ids)
    print("Classifications:", [c.value for c in resp.classifications])
    print("Canonical (English) text used for retrieval:", canonical_used)
    print("Qwen fallback used:", resp.fallback_used)


if __name__ == "__main__":
    run_query()
