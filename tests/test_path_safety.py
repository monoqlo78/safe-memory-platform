"""Path traversal outside SAFE_MEMORY_ROOT is rejected."""

from __future__ import annotations

import pytest

from app.core import pack_io
from app.core.pack_io import UnsafePathError


def test_relative_traversal_rejected(safe_root):
    with pytest.raises(UnsafePathError):
        pack_io.ensure_safe_path("../../etc/passwd")


def test_absolute_outside_root_rejected(safe_root):
    outside = "C:\\Windows\\System32\\config" if pack_io.os.name == "nt" else "/etc/passwd"
    with pytest.raises(UnsafePathError):
        pack_io.ensure_safe_path(outside)


def test_safe_relative_path_allowed(safe_root):
    resolved = pack_io.ensure_safe_path("agents/tax-agent/packs/public/x.smp.json")
    root = pack_io.get_root()
    # Resolved path stays within the root.
    assert str(resolved).startswith(str(root))


def test_deep_traversal_via_subfolder_rejected(safe_root):
    with pytest.raises(UnsafePathError):
        pack_io.ensure_safe_path("agents/../../../secrets.json")
