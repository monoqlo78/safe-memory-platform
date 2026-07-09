"""Batched Japanese->English translation: alignment, fallback, and chunking."""

from __future__ import annotations

import pytest

from app.core import translation
from app.core.qwen_client import qwen_client

_BATCH_MARKER = "numbered Japanese items"


def _is_batch_call(messages) -> bool:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    return _BATCH_MARKER in system


def _user_content(messages) -> str:
    return next((m["content"] for m in messages if m["role"] == "user"), "")


@pytest.fixture
def enable_qwen(monkeypatch):
    """Pretend Qwen is enabled so batched chat calls are exercised."""
    monkeypatch.setattr(qwen_client, "_enabled", True, raising=False)
    monkeypatch.setattr(qwen_client, "_client", object(), raising=False)


# ---------------------------------------------------------------------------
# Fallback mode (Qwen disabled): one labeled fallback per input
# ---------------------------------------------------------------------------
def test_batch_fallback_returns_one_per_input():
    texts = ["源泉徴収の説明", "消費税の仕訳", "freee のCSV取込"]
    out = translation.translate_batch_to_english(texts)

    assert len(out) == len(texts)
    for original, translated in zip(texts, out):
        assert translated.startswith(translation.FALLBACK_PREFIX)
        assert original in translated


def test_batch_preserves_order_and_empty_slots():
    texts = ["あ", "", "い"]
    out = translation.normalize_accounting_batch(texts)

    assert len(out) == 3
    assert out[1] == ""  # empty input stays empty and is never sent to the model
    assert out[0].startswith(translation.FALLBACK_PREFIX)
    assert "あ" in out[0]
    assert "い" in out[2]


def test_batch_empty_list():
    assert translation.translate_batch_to_english([]) == []


# ---------------------------------------------------------------------------
# Happy path: well-formed batched response is parsed and aligned
# ---------------------------------------------------------------------------
def test_batch_happy_path_alignment_and_chunking(enable_qwen, monkeypatch):
    calls: list[int] = []

    def fake_chat(messages, **kwargs):
        assert _is_batch_call(messages)
        lines = [ln for ln in _user_content(messages).splitlines() if ln.strip()]
        calls.append(len(lines))
        out = []
        for ln in lines:
            num, text = ln.split(".", 1)
            out.append(f"{num.strip()}. EN::{text.strip()}")
        return "\n".join(out)

    monkeypatch.setattr(qwen_client, "chat_completion", fake_chat)

    texts = [f"行{i}" for i in range(6)]
    out = translation.translate_batch_to_english(texts, batch_size=2)

    assert len(out) == 6
    assert all(t.startswith("EN::") for t in out)
    assert out[0] == "EN::行0"
    # 6 items / batch_size 2 => 3 batched calls, each of size 2.
    assert calls == [2, 2, 2]


def test_batch_flattens_multiline_rows(enable_qwen, monkeypatch):
    def fake_chat(messages, **kwargs):
        lines = [ln for ln in _user_content(messages).splitlines() if ln.strip()]
        # Each input must arrive as exactly one numbered line (no leaked newlines).
        assert len(lines) == 1
        num, text = lines[0].split(".", 1)
        return f"{num.strip()}. {text.strip()}"

    monkeypatch.setattr(qwen_client, "chat_completion", fake_chat)

    out = translation.translate_batch_to_english(["勘定科目: 売上\n金額: 1000"], batch_size=5)
    assert len(out) == 1
    assert "勘定科目: 売上 / 金額: 1000" in out[0]


# ---------------------------------------------------------------------------
# Mismatched-count response -> per-item fallback, still N aligned items
# ---------------------------------------------------------------------------
def test_batch_mismatch_triggers_per_item_fallback(enable_qwen, monkeypatch):
    def fake_chat(messages, **kwargs):
        if _is_batch_call(messages):
            # Deliberately wrong count (1 line for a multi-item batch).
            return "1. only one line for the whole batch"
        # Per-item translation call (translate_to_english): echo the input.
        return f"EN[{_user_content(messages)}]"

    monkeypatch.setattr(qwen_client, "chat_completion", fake_chat)

    texts = ["あ", "い", "う"]
    out = translation.translate_batch_to_english(texts, batch_size=10)

    assert out == ["EN[あ]", "EN[い]", "EN[う]"]


def test_batch_empty_model_response_falls_back(enable_qwen, monkeypatch):
    def fake_chat(messages, **kwargs):
        if _is_batch_call(messages):
            return ""  # no content at all
        return f"EN[{_user_content(messages)}]"

    monkeypatch.setattr(qwen_client, "chat_completion", fake_chat)

    out = translation.translate_batch_to_english(["x", "y"], batch_size=10)
    assert out == ["EN[x]", "EN[y]"]


# ---------------------------------------------------------------------------
# Numbered-line parser accepts '1.', '1)', and '1:' formats
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sep", [".", ")", ":"])
def test_parse_numbered_response_formats(sep):
    answer = f"1{sep} first\n2{sep} second\n3{sep} third"
    parsed = translation._parse_numbered_response(answer, 3)
    assert parsed == ["first", "second", "third"]


def test_parse_numbered_response_wrong_count_returns_none():
    assert translation._parse_numbered_response("1. only", 3) is None
    assert translation._parse_numbered_response("", 2) is None
    # Non-contiguous numbering is rejected.
    assert translation._parse_numbered_response("1. a\n3. c", 2) is None
