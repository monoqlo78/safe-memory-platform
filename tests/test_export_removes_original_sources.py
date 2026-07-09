"""Export with remove_sources strips the private Japanese source."""

from __future__ import annotations

from app.api.packs import build_pack_from_entries, export_pack
from app.core import pack_io
from app.models.pack_schema import Classification, ExportRequest


def test_export_removes_original_text_and_source(safe_root):
    specs = [
        {
            "text": "Public guidance on tax category CSV columns.",
            "original_text": "税区分のCSV列に関する公開ガイダンス。",
            "canonical_text": "Public guidance on tax category CSV columns.",
            "source_language": "ja",
            "canonical_language": "en",
            "translation_note": "translated",
            "classification": Classification.PUBLIC,
        }
    ]
    pack, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="export-src-test",
        title="Export Source Test",
        entries=specs,
    )

    resp = export_pack(
        ExportRequest(
            agent_id="tax-agent",
            pack_path=pack_io.relpath_from_root(saved_path),
            export_name="export-src-test-shareable",
            allowed_classifications=[Classification.PUBLIC, Classification.SHAREABLE],
            remove_sources=True,
            redact_sensitive_text=True,
        )
    )

    assert resp.included_count == 1
    export = pack_io.load_pack(resp.export_path)
    entry = export.entries[0]

    # Private Japanese source and provenance are removed.
    assert entry.original_text is None
    assert entry.translation_note is None
    assert entry.provenance.source == "removed"
    assert entry.provenance.origin_pack_id is None
    # Canonical English text is still present for the receiving agent.
    assert "tax category" in entry.canonical_text.lower()


def test_export_keeps_original_when_sources_not_removed(safe_root):
    specs = [
        {
            "text": "Public note.",
            "original_text": "公開メモ。",
            "canonical_text": "Public note.",
            "source_language": "ja",
            "canonical_language": "en",
            "classification": Classification.PUBLIC,
        }
    ]
    _, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="export-keep-test",
        title="Export Keep Test",
        entries=specs,
    )
    resp = export_pack(
        ExportRequest(
            agent_id="tax-agent",
            pack_path=pack_io.relpath_from_root(saved_path),
            export_name="export-keep-shareable",
            allowed_classifications=[Classification.PUBLIC],
            remove_sources=False,
            redact_sensitive_text=False,
        )
    )
    export = pack_io.load_pack(resp.export_path)
    assert export.entries[0].original_text == "公開メモ。"
