"""Tokenized download links for exported Safe Memory Packs.

`exportMemoryPack` writes a shareable pack to disk and returns an absolute HTTPS
``download_url`` so another agent/person can fetch it (and re-import it via
``importPackByRef``) without needing the API key. The link carries a hard-to-guess
token; a tiny JSON record maps the token to the export's root-relative path.

Records live under ``SAFE_MEMORY_ROOT/dl/<token>.json`` — no database, fully
sandbox-confined like everything else.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core import pack_io

logger = logging.getLogger("safe_memory.export_links")

_DL_DIR = "dl"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _link_path(token: str) -> Path:
    safe = pack_io._sanitize_segment(token)
    return pack_io.ensure_safe_path(Path(_DL_DIR) / f"{safe}.json")


def create_export_link(
    agent_id: str,
    export_rel_path: Optional[str],
    oss_object_key: Optional[str] = None,
    job_id: Optional[str] = None,
) -> str:
    """Create a download token for an export/pack and return the token.

    The token is a URL-safe string (``secrets.token_urlsafe``), so it never
    carries base64 signature characters (``+``/``/``/``=``) that can be
    corrupted when a URL is passed through GPT/ChatGPT. ``oss_object_key`` lets
    :func:`app.api.packs.download_export` 307-redirect to a freshly signed OSS
    URL when the local pack has been cleaned up.
    """
    token = secrets.token_urlsafe(24)
    record = {
        "token": token,
        "agent_id": agent_id,
        "rel_path": export_rel_path,
        "created_at": _utcnow(),
    }
    if oss_object_key:
        record["oss_object_key"] = oss_object_key
    if job_id:
        record["job_id"] = job_id
    path = _link_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return token


def resolve_export_link_record(token: str) -> Optional[dict]:
    """Return the full token record (rel_path, oss_object_key, job_id), or None."""
    try:
        path = _link_path(token)
    except pack_io.UnsafePathError:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            record = json.load(fh)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not read export link record.")
        return None
    return record


def resolve_export_link(token: str) -> Optional[str]:
    """Return the root-relative export path for a token, or None."""
    record = resolve_export_link_record(token)
    if not record:
        return None
    return record.get("rel_path")
