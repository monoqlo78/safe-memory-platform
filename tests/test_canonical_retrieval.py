"""Canonical English text drives retrieval and answering."""

from __future__ import annotations

from app.api.packs import build_pack_from_entries, query_pack
from app.core import pack_io
from app.core.search import hybrid_search
from app.core.qwen_client import qwen_client
from app.models.pack_schema import Classification, QueryRequest, get_retrieval_text


def _build(safe_root):
    specs = [
        {
            "text": "税区分は仕訳CSVの列として表現されます。",
            "original_text": "税区分は仕訳CSVの列として表現されます。",
            "canonical_text": (
                "Tax categories are represented as columns in the journal CSV import."
            ),
            "source_language": "ja",
            "canonical_language": "en",
            "classification": Classification.PUBLIC,
        },
        {
            "text": "オフィス家具の減価償却について。",
            "original_text": "オフィス家具の減価償却について。",
            "canonical_text": "Notes about office furniture depreciation schedules.",
            "source_language": "ja",
            "canonical_language": "en",
            "classification": Classification.PUBLIC,
        },
    ]
    pack, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="canonical-test",
        title="Canonical Retrieval Test",
        entries=specs,
    )
    return pack, saved_path


def test_hybrid_search_uses_canonical_text(safe_root):
    pack, _ = _build(safe_root)
    query = "How are tax categories represented in a journal CSV?"
    q_emb = qwen_client.embed_text(query)

    ranked = hybrid_search(query, q_emb, pack.entries, top_k=2)
    top_entry = ranked[0][0]

    assert "Tax categories" in get_retrieval_text(top_entry)


def test_query_response_returns_canonical_text(safe_root):
    _, saved_path = _build(safe_root)
    resp = query_pack(
        QueryRequest(
            agent_id="tax-agent",
            pack_path=pack_io.relpath_from_root(saved_path),
            query="How are tax categories represented in a journal CSV?",
            top_k=2,
        )
    )

    assert resp.hits, "expected at least one hit"
    # The top hit text is the English canonical text, not the Japanese source.
    assert "Tax categories" in resp.hits[0].text
    assert resp.used_memory_ids


def test_query_emits_structured_log(safe_root, caplog):
    """Query logs agent_id/pack_id/hits for demo traceability, no secret values."""
    _, saved_path = _build(safe_root)
    with caplog.at_level("INFO", logger="safe_memory.packs"):
        query_pack(
            QueryRequest(
                agent_id="tax-agent",
                pack_path=pack_io.relpath_from_root(saved_path),
                query="How are tax categories represented in a journal CSV?",
                top_k=2,
            )
        )

    lines = [r.getMessage() for r in caplog.records if "queryMemoryPack" in r.getMessage()]
    assert lines, "expected a queryMemoryPack log line"
    line = lines[-1]
    assert "agent_id=tax-agent" in line
    assert "pack_id=canonical-test" in line
    assert "top_k=2" in line
    assert "hits=" in line
    # The API key is never logged.
    assert "X-Safe-Memory-Key" not in line

