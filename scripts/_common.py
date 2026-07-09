"""Shared bootstrap for demo/import scripts.

Run this before importing anything from ``app`` so that:
  * the backend package is importable, and
  * SAFE_MEMORY_ROOT points at a local, Windows-friendly folder
    (``<project_root>/SafeMemory``) instead of the Docker path
    (``/app/SafeMemory``) when not running in a container.

Environment variables take precedence over the ``.env`` file in
pydantic-settings, so setting SAFE_MEMORY_ROOT here (before ``app.config`` is
imported) reliably overrides the Docker default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"


def bootstrap() -> Path:
    """Configure sys.path and SAFE_MEMORY_ROOT. Returns the project root."""
    # Force UTF-8 stdout/stderr so Japanese text and dashes print on Windows.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    current = os.environ.get("SAFE_MEMORY_ROOT", "").strip()
    # Use a local folder unless a real, existing local path was provided.
    if not current or current in {"/app/SafeMemory"} or not Path(current).exists():
        local_root = PROJECT_ROOT / "SafeMemory"
        local_root.mkdir(parents=True, exist_ok=True)
        os.environ["SAFE_MEMORY_ROOT"] = str(local_root)

    return PROJECT_ROOT
