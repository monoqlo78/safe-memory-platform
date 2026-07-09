"""Pydantic models for one-time (SAS-like) upload links.

A one-time upload link lets a client (typically a GPT Action) mint a keyless,
single-use URL. The end user opens ``/u/{token}`` and drops a folder without ever
seeing the master API key. The resulting pack is bound back to a *claim* record
so the LLM can poll for the result. Claims are persisted as one small JSON file
per claim under ``SAFE_MEMORY_ROOT/upload_links/`` (no database).

The ``token`` authorizes the anonymous upload page; the ``claim_id`` is the
handle the (already authenticated) LLM uses to poll for the result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class UploadLinkClaim(BaseModel):
    """Server-side record for a one-time upload link.

    ``token`` is secret (grants the anonymous upload page its scoped rights) and
    is never returned by the polling endpoints. ``job_id`` is bound once the
    scoped build starts, so the result can be resolved via the jobs store.
    """

    claim_id: str
    token: str
    agent_id: str
    pack_id: str
    title: str
    source_language: Optional[str] = None
    canonical_language: str = "en"
    retention_mode: str = "process_and_return"
    default_classification: str = "internal"
    created_at: str = Field(default_factory=_utcnow)
    expires_at: str = ""
    max_uses: int = 1
    uses_consumed: int = 0
    job_id: Optional[str] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    def uses_remaining(self) -> int:
        return max(0, int(self.max_uses) - int(self.uses_consumed))


class CreateUploadLinkRequest(BaseModel):
    """Request to mint a one-time upload link. All fields optional with defaults."""

    agent_id: str = "shared"
    pack_id: Optional[str] = None
    title: str = "Uploaded via one-time link"
    source_language: Optional[str] = None
    canonical_language: str = "en"
    retention_mode: str = "process_and_return"
    classification: str = "internal"
    expires_in_seconds: int = 1800
    max_uses: int = 1


class CreateUploadLinkResponse(BaseModel):
    """A freshly minted one-time upload link. The master key is never echoed."""

    upload_url: str
    claim_id: str
    expires_at: str


class UploadLinkResultResponse(BaseModel):
    """Poll result for a one-time upload link (what the LLM re-imports)."""

    status: str
    claim_id: str
    job_id: Optional[str] = None
    pack_id: Optional[str] = None
    download_url: Optional[str] = None
    entry_count: Optional[int] = None
    input_type: Optional[str] = None
    unsupported_files: List[Dict[str, str]] = Field(default_factory=list)
