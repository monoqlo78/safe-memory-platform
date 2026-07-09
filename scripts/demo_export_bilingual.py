"""English demo: export a safe, shareable copy of the bilingual pack.

Exports only PUBLIC and SHAREABLE entries, removes the private Japanese source
text, and redacts any sensitive content. CONFIDENTIAL and SECRET entries are
excluded.
"""

from __future__ import annotations

from _common import bootstrap

bootstrap()

from app.api.packs import export_pack  # noqa: E402
from app.core import pack_io  # noqa: E402
from app.models.pack_schema import Classification, ExportRequest  # noqa: E402

AGENT_ID = "tax-agent"
PACK_ID = "jp-accounting-bilingual"
EXPORT_NAME = "jp-accounting-shareable"


def run_export() -> str:
    path = pack_io.find_pack_by_id(AGENT_ID, PACK_ID)
    if path is None:
        raise SystemExit(
            "Pack not found. Run scripts/import_accounting_xlsx.py first."
        )

    resp = export_pack(
        ExportRequest(
            agent_id=AGENT_ID,
            pack_path=pack_io.relpath_from_root(path),
            export_name=EXPORT_NAME,
            allowed_classifications=[Classification.PUBLIC, Classification.SHAREABLE],
            remove_sources=True,
            redact_sensitive_text=True,
        )
    )

    export_pack_obj = pack_io.load_pack(resp.export_path)
    original_leaks = [e.id for e in export_pack_obj.entries if e.original_text]
    sensitive_leaks = [
        e.id
        for e in export_pack_obj.entries
        if e.classification in {Classification.CONFIDENTIAL, Classification.SECRET}
    ]

    print("Export path:", resp.export_path)
    print("Included entries:", resp.included_count)
    print("Excluded entries:", resp.excluded_count)
    print("Warnings:", resp.warnings)
    print("Original Japanese source removed from export:", not original_leaks)
    print("CONFIDENTIAL/SECRET excluded from export:", not sensitive_leaks)
    return resp.export_path


if __name__ == "__main__":
    run_export()
