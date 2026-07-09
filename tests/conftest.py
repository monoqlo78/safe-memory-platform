"""Shared pytest fixtures for the Safe Memory Platform tests.

Non-integration tests run in deterministic fallback mode (the Qwen client is
disabled) and use a temporary SAFE_MEMORY_ROOT so they never touch real data.
Tests marked ``integration`` are allowed to use real Qwen credentials.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Use a throwaway storage root BEFORE importing app config so import-time
# directory creation never touches real pack data.
os.environ["SAFE_MEMORY_ROOT"] = tempfile.mkdtemp(prefix="smp_test_root_")

from app.config import settings  # noqa: E402
from app.core.qwen_client import qwen_client  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: test that may call the real Qwen API."
    )


@pytest.fixture(autouse=True)
def _force_fallback(request, monkeypatch):
    """Disable the Qwen client for deterministic fallback behavior."""
    if request.node.get_closest_marker("integration"):
        return
    monkeypatch.setattr(qwen_client, "_enabled", False, raising=False)
    monkeypatch.setattr(qwen_client, "_client", None, raising=False)


@pytest.fixture
def safe_root(tmp_path, monkeypatch):
    """Point storage at a fresh temp dir for each test."""
    monkeypatch.setattr(settings, "safe_memory_root", str(tmp_path))
    return tmp_path
