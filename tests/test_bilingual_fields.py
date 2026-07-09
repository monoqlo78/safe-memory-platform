"""Bilingual entry fields are stored and preserved."""

from __future__ import annotations

from app.api.packs import build_pack_from_entries
from app.core import pack_io
from app.models.pack_schema import Classification, get_retrieval_text


def test_bilingual_fields_preserved(safe_root):
    specs = [
        {
            "text": "Consumption tax categories in journal CSV.",
            "original_text": "消費税区分は仕訳CSVで指定します。",
            "canonical_text": "Consumption tax categories in journal CSV.",
            "source_language": "ja",
            "canonical_language": "en",
            "translation_note": "Auto-translated for the demo.",
            "classification": Classification.PUBLIC,
        }
    ]
    pack, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="bilingual-test",
        title="Bilingual Test",
        entries=specs,
    )

    reloaded = pack_io.load_pack(saved_path)
    entry = reloaded.entries[0]

    assert entry.original_text == "消費税区分は仕訳CSVで指定します。"
    assert entry.canonical_text == "Consumption tax categories in journal CSV."
    assert entry.source_language == "ja"
    assert entry.canonical_language == "en"
    assert entry.translation_note == "Auto-translated for the demo."
    # Retrieval prefers canonical English text.
    assert get_retrieval_text(entry) == entry.canonical_text


def test_entry_without_bilingual_fields_is_backward_compatible(safe_root):
    specs = [{"text": "Plain legacy entry with only text."}]
    pack, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="legacy-test",
        title="Legacy Test",
        entries=specs,
    )
    entry = pack_io.load_pack(saved_path).entries[0]

    assert entry.original_text is None
    assert entry.canonical_text is None
    # Falls back to text.
    assert get_retrieval_text(entry) == "Plain legacy entry with only text."
