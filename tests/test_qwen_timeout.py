"""The Qwen client must bound each call with an explicit timeout + retry cap.

Without these, the OpenAI SDK waits up to ~600s per attempt, so a stalled
upstream makes large batched imports appear to hang forever. These tests pin
the construction contract using a fake OpenAI so no network is touched.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a fake ``openai`` module capturing OpenAI() kwargs."""
    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    return captured


def _configure_credentials(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "qwen_api_key", "test-key-not-a-placeholder")


def test_qwen_client_sets_timeout_and_retries(fake_openai, monkeypatch):
    _configure_credentials(monkeypatch)
    from app.config import settings
    from app.core.qwen_client import QwenClient

    client = QwenClient()

    assert client.enabled
    assert fake_openai["timeout"] == settings.qwen_timeout_seconds
    assert fake_openai["max_retries"] == settings.qwen_max_retries


def test_qwen_timeout_defaults_are_bounded():
    from app.config import settings

    # A single call must not be able to block for minutes on end.
    assert settings.qwen_timeout_seconds <= 60
    assert settings.qwen_max_retries <= 2


def test_qwen_timeout_overridable_via_env(fake_openai, monkeypatch):
    _configure_credentials(monkeypatch)
    from app.config import settings
    from app.core.qwen_client import QwenClient

    monkeypatch.setattr(settings, "qwen_timeout_seconds", 12.5)
    monkeypatch.setattr(settings, "qwen_max_retries", 0)

    QwenClient()

    assert fake_openai["timeout"] == 12.5
    assert fake_openai["max_retries"] == 0
