"""Persistence for one-time (SAS-like) upload-link claims.

Each claim is stored as one small JSON file per claim under
``SAFE_MEMORY_ROOT/upload_links/`` (no database). A claim carries a secret
``token`` that authorizes the anonymous ``/u/{token}`` upload page, and a
``claim_id`` the authenticated LLM uses to poll for the result.

Secrets (the token, the master API key) are never logged.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from app.core import pack_io
from app.models.upload_link_schema import UploadLinkClaim

logger = logging.getLogger("safe_memory.upload_links")

# Hard cap on how long a one-time link may live (defence in depth).
MAX_EXPIRES_IN_SECONDS = 3600
DEFAULT_EXPIRES_IN_SECONDS = 1800


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    """High-entropy, URL-safe one-time token."""
    return secrets.token_urlsafe(32)


def new_claim_id() -> str:
    return secrets.token_urlsafe(16)


def new_import_agent_id() -> str:
    """Unguessable, per-claim agent namespace for ephemeral imports.

    Imported packs live under this agent id only. It is returned to the
    authenticated LLM (via getUploadLinkResult), never to the anonymous page,
    so different links cannot reach each other's packs.
    """
    return "imp-" + secrets.token_urlsafe(12)


def compute_expires_at(expires_in_seconds: int) -> str:
    ttl = int(expires_in_seconds or DEFAULT_EXPIRES_IN_SECONDS)
    ttl = max(60, min(ttl, MAX_EXPIRES_IN_SECONDS))
    return (_utcnow() + timedelta(seconds=ttl)).isoformat()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_claim(claim: UploadLinkClaim) -> Path:
    """Persist a claim to ``upload_links/<claim_id>.json`` (atomic write)."""
    path = pack_io.upload_link_path(claim.claim_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = claim.model_dump(mode="json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def load_claim(claim_id: str) -> Optional[UploadLinkClaim]:
    """Load a claim by claim_id, or None if missing/invalid."""
    try:
        path = pack_io.upload_link_path(claim_id)
    except pack_io.UnsafePathError:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return UploadLinkClaim.model_validate(json.load(fh))
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not load upload-link claim %s", claim_id)
        return None


def list_claims() -> List[UploadLinkClaim]:
    base = pack_io.upload_links_dir()
    claims: List[UploadLinkClaim] = []
    if not base.exists():
        return claims
    for candidate in sorted(base.glob("*.json")):
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                claims.append(UploadLinkClaim.model_validate(json.load(fh)))
        except Exception:
            continue
    return claims


# ---------------------------------------------------------------------------
# Token lookup
# ---------------------------------------------------------------------------
def _match_token(claim: UploadLinkClaim, token: str) -> bool:
    return secrets.compare_digest(claim.token, token)


def find_claim_by_token(token: str) -> Optional[UploadLinkClaim]:
    """Return a non-expired claim matching ``token`` (uses not checked).

    Used by the result-polling page so it can keep reading the outcome even
    after the single build has been consumed.
    """
    if not token:
        return None
    for claim in list_claims():
        if _match_token(claim, token) and not claim.is_expired():
            return claim
    return None


def find_usable_claim_by_token(token: str) -> Optional[UploadLinkClaim]:
    """Return a claim that may still stage/build: not expired and uses remain."""
    claim = find_claim_by_token(token)
    if claim is None:
        return None
    if claim.uses_remaining() <= 0:
        return None
    return claim


def consume_use(claim: UploadLinkClaim, job_id: str) -> UploadLinkClaim:
    """Bind the resulting job to the claim and consume one use (persisted)."""
    claim.job_id = job_id
    claim.uses_consumed = int(claim.uses_consumed) + 1
    save_claim(claim)
    return claim


def record_import(claim: UploadLinkClaim, imported: dict) -> UploadLinkClaim:
    """Append one imported-pack record to an import-mode claim (persisted).

    Import-mode links accept several packs in one session, so uses are not
    consumed per file; the link stays usable until it expires. Only the safe,
    non-secret summary (agent_id, pack_id, entry_count, classifications,
    verified) is stored.
    """
    claim.imported = list(claim.imported) + [imported]
    save_claim(claim)
    return claim
