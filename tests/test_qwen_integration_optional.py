"""Optional live Qwen integration test.

Skipped unless QWEN_API_KEY is set to a real (non-placeholder) value.
Never prints the API key.
"""

from __future__ import annotations

import os

import pytest

_key = (os.environ.get("QWEN_API_KEY") or "").strip()
_has_real_key = bool(_key) and _key.lower() not in {"replace_me", "changeme", "your_key"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_real_key,
        reason="QWEN_API_KEY not set to a real value; skipping live Qwen tests.",
    ),
]


def _fresh_client():
    # Build a client that reflects the current (real) credentials.
    from app.config import get_settings
    from app.core.qwen_client import QwenClient

    get_settings.cache_clear()
    return QwenClient()


def test_qwen_translation_call(monkeypatch):
    from app.core import translation

    client = _fresh_client()
    if not client.enabled:
        pytest.skip("Qwen client could not be initialized.")

    monkeypatch.setattr(translation, "qwen_client", client)
    result = translation.translate_to_english("消費税は仕訳CSVで指定します。")
    assert result
    assert not result.startswith("[UNTRANSLATED FALLBACK]")


def test_qwen_embedding_call():
    client = _fresh_client()
    if not client.enabled:
        pytest.skip("Qwen client could not be initialized.")

    vector = client.embed_text("journal CSV tax category")
    assert isinstance(vector, list)
    assert len(vector) > 0


def test_qwen_chat_answer_call():
    client = _fresh_client()
    if not client.enabled:
        pytest.skip("Qwen client could not be initialized.")

    result = client.answer_with_context(
        "What is described?",
        [{"id": "e1", "text": "Tax categories map to columns in a journal CSV."}],
    )
    assert result["answer"]
    assert result["fallback_used"] is False
