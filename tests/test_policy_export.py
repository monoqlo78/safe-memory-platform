"""CONFIDENTIAL and SECRET entries are excluded from a safe shareable export."""

from __future__ import annotations

from app.api.packs import build_pack_from_entries, export_pack
from app.core import pack_io
from app.models.pack_schema import Classification, ExportRequest


def _mixed_pack(safe_root):
    specs = [
        {"text": "Public tax guidance.", "classification": Classification.PUBLIC},
        {"text": "Shareable summary.", "classification": Classification.SHAREABLE},
        {"text": "Internal working note.", "classification": Classification.INTERNAL},
        {"text": "Employee salary sheet.", "classification": Classification.CONFIDENTIAL},
        {"text": "Root API key value.", "classification": Classification.SECRET},
    ]
    _, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="policy-export-test",
        title="Policy Export Test",
        entries=specs,
    )
    return saved_path


def test_safe_export_excludes_confidential_and_secret(safe_root):
    saved_path = _mixed_pack(safe_root)

    resp = export_pack(
        ExportRequest(
            agent_id="tax-agent",
            pack_path=pack_io.relpath_from_root(saved_path),
            export_name="policy-shareable",
            allowed_classifications=[Classification.PUBLIC, Classification.SHAREABLE],
            remove_sources=True,
            redact_sensitive_text=True,
        )
    )

    export = pack_io.load_pack(resp.export_path)
    classes = {e.classification for e in export.entries}

    assert Classification.CONFIDENTIAL not in classes
    assert Classification.SECRET not in classes
    assert classes <= {Classification.PUBLIC, Classification.SHAREABLE}
    assert resp.included_count == 2
    assert resp.excluded_count == 3


def test_confidential_included_only_when_explicitly_allowed(safe_root):
    saved_path = _mixed_pack(safe_root)

    resp = export_pack(
        ExportRequest(
            agent_id="tax-agent",
            pack_path=pack_io.relpath_from_root(saved_path),
            export_name="policy-explicit",
            allowed_classifications=[
                Classification.PUBLIC,
                Classification.CONFIDENTIAL,
            ],
            remove_sources=True,
            redact_sensitive_text=True,
        )
    )
    export = pack_io.load_pack(resp.export_path)
    classes = {e.classification for e in export.entries}

    # CONFIDENTIAL explicitly allowed; SECRET still excluded.
    assert Classification.CONFIDENTIAL in classes
    assert Classification.SECRET not in classes
