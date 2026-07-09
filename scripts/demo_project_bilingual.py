"""English demo: run a project task using the shareable exported pack.

Simulates a receipt-agent consuming the shared pack to produce guidance.
Run the importer and export demo first (or use demo_all_bilingual.py).
"""

from __future__ import annotations

from _common import bootstrap

bootstrap()

from app.api.projects import run_project  # noqa: E402
from app.core import pack_io  # noqa: E402
from app.models.project_schema import ProjectRunRequest  # noqa: E402

EXPORT_NAME = "jp-accounting-shareable.smp.json"
TASK = (
    "Create guidance for a receipt OCR agent that needs to convert Japanese "
    "receipts into Money Forward Cloud journal CSV entries."
)


def run_demo_project() -> None:
    export_path = pack_io.ensure_safe_path(
        f"agents/tax-agent/exports/{EXPORT_NAME}"
    )
    if not export_path.exists():
        raise SystemExit(
            "Shareable export not found. Run scripts/demo_export_bilingual.py first."
        )

    resp = run_project(
        ProjectRunRequest(
            project_id="receipt-ocr-guidance",
            agent_id="receipt-agent",
            task=TASK,
            pack_paths=[pack_io.relpath_from_root(export_path)],
            top_k=5,
        )
    )

    print("TASK:", TASK)
    print()
    print("PROJECT OUTPUT (English):")
    print(resp.output)
    print()
    print("Used memory IDs:", resp.used_memory_ids)
    print("Suggested new memories:")
    for s in resp.suggested_new_memories:
        print(f"  - [{s.suggested_classification.value}] {s.text}")
    print("Qwen fallback used:", resp.fallback_used)


if __name__ == "__main__":
    run_demo_project()
