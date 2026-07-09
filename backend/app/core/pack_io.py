"""Filesystem IO for Safe Memory Packs, confined to SAFE_MEMORY_ROOT."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.models.pack_schema import CatalogItem, Classification, SafeMemoryPack

PACK_SUFFIX = ".smp.json"


class UnsafePathError(Exception):
    """Raised when a path escapes SAFE_MEMORY_ROOT."""


def get_root() -> Path:
    """Return the resolved SAFE_MEMORY_ROOT, creating it if needed."""
    root = Path(settings.safe_memory_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_safe_path(path: str | os.PathLike[str]) -> Path:
    """Resolve ``path`` and guarantee it is inside SAFE_MEMORY_ROOT.

    Accepts absolute paths (which must be within the root) and relative
    paths (resolved against the root). Raises :class:`UnsafePathError`
    when the resolved path escapes the root.
    """
    root = get_root()
    candidate = Path(path)

    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve()

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(
            "Path is outside the Safe Memory root and was rejected."
        ) from exc

    return resolved


def _sanitize_segment(segment: str) -> str:
    """Sanitize a single path segment (agent id, pack id, name)."""
    segment = (segment or "").strip()
    # Keep it filesystem-safe and prevent traversal.
    safe = "".join(c for c in segment if c.isalnum() or c in ("-", "_", "."))
    safe = safe.replace("..", "")
    return safe or "unnamed"


def agent_dir(agent_id: str) -> Path:
    """Return the vault directory for an agent."""
    return ensure_safe_path(Path("agents") / _sanitize_segment(agent_id))


def pack_target_path(
    agent_id: str,
    pack_id: str,
    classification: Classification,
) -> Path:
    """Compute the on-disk path for a newly built pack."""
    safe_agent = _sanitize_segment(agent_id)
    safe_pack = _sanitize_segment(pack_id)
    class_dir = classification.value.lower()
    rel = Path("agents") / safe_agent / "packs" / class_dir / f"{safe_pack}{PACK_SUFFIX}"
    return ensure_safe_path(rel)


def save_pack(pack: SafeMemoryPack, path: str | os.PathLike[str]) -> Path:
    """Serialize and write a pack to ``path`` inside the root."""
    safe_path = ensure_safe_path(path)
    safe_path.parent.mkdir(parents=True, exist_ok=True)

    pack.manifest.updated_at = datetime.now(timezone.utc).isoformat()
    pack.manifest.entry_count = len(pack.entries)

    data = pack.model_dump(mode="json")
    tmp_path = safe_path.with_suffix(safe_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, safe_path)
    return safe_path


def load_pack(path: str | os.PathLike[str]) -> SafeMemoryPack:
    """Load and validate a pack from ``path`` inside the root."""
    safe_path = ensure_safe_path(path)
    if not safe_path.exists():
        raise FileNotFoundError(f"Pack not found: {safe_path.name}")
    with safe_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return SafeMemoryPack.model_validate(data)


def find_pack_by_id(agent_id: str, pack_id: str) -> Optional[Path]:
    """Locate a pack file by agent and pack id across classifications."""
    base = agent_dir(agent_id) / "packs"
    if not base.exists():
        return None

    safe_pack = _sanitize_segment(pack_id)
    for candidate in base.rglob(f"*{PACK_SUFFIX}"):
        try:
            pack = load_pack(candidate)
        except Exception:
            continue
        if pack.manifest.pack_id == pack_id or candidate.name == f"{safe_pack}{PACK_SUFFIX}":
            return candidate
    return None


def scan_agent_catalog(agent_id: str) -> List[CatalogItem]:
    """Scan an agent's vault and return a catalog of its packs."""
    base = agent_dir(agent_id) / "packs"
    items: List[CatalogItem] = []
    if not base.exists():
        return items

    root = get_root()
    for candidate in sorted(base.rglob(f"*{PACK_SUFFIX}")):
        try:
            pack = load_pack(candidate)
        except Exception:
            continue
        rel_path = candidate.resolve().relative_to(root).as_posix()
        items.append(
            CatalogItem(
                pack_id=pack.manifest.pack_id,
                title=pack.manifest.title,
                version=pack.manifest.version,
                classification=pack.manifest.default_classification,
                path=rel_path,
                entry_count=len(pack.entries),
                updated_at=pack.manifest.updated_at,
            )
        )
    return items


def export_target_path(agent_id: str, export_name: str) -> Path:
    """Compute the on-disk path for an export pack."""
    safe_name = _sanitize_segment(export_name)
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}{PACK_SUFFIX}"
    rel = Path("agents") / _sanitize_segment(agent_id) / "exports" / safe_name
    return ensure_safe_path(rel)


def temp_pack_target_path(agent_id: str, pack_id: str, job_id: str) -> Path:
    """Compute the on-disk path for a temporary (non-vault) pack.

    Temporary packs live under ``temp/`` and are intentionally NOT under the
    agent vault, so they never appear in the agent catalog.
    """
    safe_agent = _sanitize_segment(agent_id)
    safe_pack = _sanitize_segment(pack_id)
    safe_job = _sanitize_segment(job_id)
    rel = Path("temp") / safe_agent / f"{safe_pack}-{safe_job}{PACK_SUFFIX}"
    return ensure_safe_path(rel)


def jobs_dir() -> Path:
    """Return the directory holding per-job metadata JSON files."""
    return ensure_safe_path(Path("jobs"))


def job_meta_path(job_id: str) -> Path:
    """Return the metadata JSON path for a job."""
    return ensure_safe_path(Path("jobs") / f"{_sanitize_segment(job_id)}.json")


def job_work_dir(job_id: str) -> Path:
    """Return the per-job working directory (raw upload + intermediate files)."""
    return ensure_safe_path(Path("jobs") / _sanitize_segment(job_id) / "work")


def upload_links_dir() -> Path:
    """Return the directory holding per-claim one-time upload-link JSON files."""
    return ensure_safe_path(Path("upload_links"))


def upload_link_path(claim_id: str) -> Path:
    """Return the metadata JSON path for a one-time upload-link claim."""
    return ensure_safe_path(Path("upload_links") / f"{_sanitize_segment(claim_id)}.json")


def save_audit_report(
    agent_id: str,
    pack_id: str,
    report: dict,
) -> Path:
    """Persist an audit report JSON under the agent's audit directory."""
    safe_pack = _sanitize_segment(pack_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rel = (
        Path("agents")
        / _sanitize_segment(agent_id)
        / "audit"
        / f"{safe_pack}-{timestamp}.audit.json"
    )
    safe_path = ensure_safe_path(rel)
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    with safe_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return safe_path


def relpath_from_root(path: str | os.PathLike[str]) -> str:
    """Return a root-relative POSIX path string for display."""
    safe_path = ensure_safe_path(path)
    return safe_path.relative_to(get_root()).as_posix()
