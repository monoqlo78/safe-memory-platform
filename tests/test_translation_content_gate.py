"""Ingest defaults: translation OFF, fast classification when caller specifies.

Guards for two performance changes:
  * Translation is opt-in (``translate=True``). By default canonical_text is the
    original text and no Qwen chat calls are made.
  * When the caller supplies a classification, every entry is stamped with it and
    the per-entry LLM classification is skipped -- except obvious secrets, which
    are still promoted to SECRET via a keyword heuristic (no LLM call).
"""

from __future__ import annotations

import pytest

from app.api import packs
from app.api.packs import build_pack_from_entries
from app.core.qwen_client import qwen_client
from app.models.pack_schema import Classification


@pytest.fixture
def spy_translate(monkeypatch):
    """Replace the batch translator with a recording marker translator."""
    calls: list[list[str]] = []

    def _fake(to_translate, batch_size=30):
        calls.append(list(to_translate))
        return [f"EN::{t}" for t in to_translate]

    monkeypatch.setattr(packs, "normalize_accounting_batch", _fake)
    return calls


# ---------------------------------------------------------------------------
# Translation is OFF by default
# ---------------------------------------------------------------------------
def test_translation_off_by_default_keeps_original_text(spy_translate):
    rows = ["源泉徴収の説明", "消費税の仕訳", "Symbol: USDJPY"]
    specs = packs._build_upload_specs(
        rows,
        source_language="ja",
        canonical_language="en",
        source="upload",
    )

    # No translation attempted at all -- even for Japanese rows.
    assert spy_translate == []
    assert len(specs) == 3
    for raw, spec in zip(rows, specs):
        assert spec["canonical_text"] == raw  # original text verbatim
        assert "translation_note" not in spec


# ---------------------------------------------------------------------------
# translate=True -> content-gated translation (only Japanese rows)
# ---------------------------------------------------------------------------
def test_translate_true_translates_only_japanese_rows(spy_translate):
    rows = [
        "Symbol: USDJPY\nProfit: 1200",   # ascii -> skip
        "勘定科目: 売上\n金額: 1000",       # ja -> translate
        "Ticket: 100236\nType: Buy",       # ascii -> skip
        "源泉徴収の説明",                    # ja -> translate
    ]
    specs = packs._build_upload_specs(
        rows,
        source_language="ja",
        canonical_language="en",
        source="upload",
        translate=True,
    )

    # Only the two Japanese rows were sent, preserving their relative order.
    assert spy_translate == [["勘定科目: 売上\n金額: 1000", "源泉徴収の説明"]]
    assert specs[0]["canonical_text"] == rows[0]
    assert "translation_note" not in specs[0]
    assert specs[1]["canonical_text"] == f"EN::{rows[1]}"
    assert specs[1]["translation_note"] == "Auto-translated to English via Qwen."
    assert specs[3]["canonical_text"] == f"EN::{rows[3]}"


def test_translate_true_skips_pure_ascii_rows(spy_translate):
    rows = [
        "Id: 1\nTicket: 100234\nSymbol: USDJPY\nType: Buy\nProfit: 1200",
        "Id: 2\nTicket: 100235\nSymbol: EURUSD\nType: Sell\nProfit: -300",
    ]
    packs._build_upload_specs(
        rows,
        source_language="ja",
        canonical_language="en",
        source="upload",
        translate=True,
    )
    # Pure-ASCII rows never reach the translator, even with translate=True.
    assert spy_translate == []


# ---------------------------------------------------------------------------
# Fast classification (A): explicit classification -> stamp, skip per-entry LLM
# ---------------------------------------------------------------------------
def test_explicit_classification_stamps_every_entry(spy_translate):
    rows = ["Symbol: USDJPY\nProfit: 1200", "Ticket: 100236\nType: Buy"]
    specs = packs._build_upload_specs(
        rows,
        source_language="ja",
        canonical_language="en",
        source="upload",
        apply_default_classification=True,
        default_classification=Classification.CONFIDENTIAL,
    )
    for spec in specs:
        assert spec["classification"] == Classification.CONFIDENTIAL


def test_no_explicit_classification_leaves_it_unset_for_llm(spy_translate):
    rows = ["Symbol: USDJPY\nProfit: 1200"]
    specs = packs._build_upload_specs(
        rows,
        source_language="ja",
        canonical_language="en",
        source="upload",
    )
    # No stamped classification -> build_pack_from_entries runs per-entry LLM.
    assert "classification" not in specs[0]


# ---------------------------------------------------------------------------
# Fast classification (B): obvious secrets are still promoted to SECRET
# ---------------------------------------------------------------------------
def test_secret_promotion_even_with_lower_default(spy_translate):
    rows = [
        "Symbol: USDJPY\nProfit: 1200",
        "Login password: hunter2 and api key sk-DONOTLEAK",
    ]
    specs = packs._build_upload_specs(
        rows,
        source_language=None,
        canonical_language="en",
        source="upload",
        apply_default_classification=True,
        default_classification=Classification.INTERNAL,
    )
    assert specs[0]["classification"] == Classification.INTERNAL
    # The obvious secret row is promoted regardless of the INTERNAL default.
    assert specs[1]["classification"] == Classification.SECRET


# ---------------------------------------------------------------------------
# Integration: explicit classification => zero classify_text (LLM) calls
# ---------------------------------------------------------------------------
def test_stamped_specs_skip_llm_classification(safe_root, monkeypatch):
    calls: list[str] = []

    def _spy_classify(text, default=Classification.INTERNAL):
        calls.append(text)
        return default

    monkeypatch.setattr(qwen_client, "classify_text", _spy_classify)

    specs = packs._build_upload_specs(
        ["Symbol: USDJPY\nProfit: 1200", "Ticket: 100236\nType: Buy"],
        source_language=None,
        canonical_language="en",
        source="upload",
        apply_default_classification=True,
        default_classification=Classification.INTERNAL,
    )
    build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="fast-cls",
        title="Fast Classification",
        entries=specs,
    )
    # Every entry was pre-stamped -> no per-entry LLM classification call.
    assert calls == []


def test_unstamped_specs_use_llm_classification(safe_root, monkeypatch):
    calls: list[str] = []

    def _spy_classify(text, default=Classification.INTERNAL):
        calls.append(text)
        return Classification.INTERNAL

    monkeypatch.setattr(qwen_client, "classify_text", _spy_classify)

    specs = packs._build_upload_specs(
        ["Symbol: USDJPY\nProfit: 1200", "Ticket: 100236\nType: Buy"],
        source_language=None,
        canonical_language="en",
        source="upload",
    )
    build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="llm-cls",
        title="LLM Classification",
        entries=specs,
    )
    # No stamped classification -> one LLM classification per entry.
    assert len(calls) == 2


def test_bulk_import_makes_zero_llm_chat_calls(safe_root, monkeypatch, spy_translate):
    """FX-scale case: translate omitted + classification given => embeddings only."""
    classify_calls: list[str] = []
    monkeypatch.setattr(
        qwen_client,
        "classify_text",
        lambda text, default=Classification.INTERNAL: classify_calls.append(text),
    )

    rows = [f"Id: {i}\nSymbol: USDJPY\nType: Buy\nProfit: {i * 10}" for i in range(272)]
    specs = packs._build_upload_specs(
        rows,
        source_language="ja",  # declared ja, but content is ASCII
        canonical_language="en",
        source="upload",
        apply_default_classification=True,
        default_classification=Classification.INTERNAL,
    )
    build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="fx-bulk",
        title="FX Bulk",
        entries=specs,
    )

    assert len(specs) == 272
    assert classify_calls == []      # no LLM classification chat calls
    assert spy_translate == []       # no translation chat calls

