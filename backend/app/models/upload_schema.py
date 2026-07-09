"""Pydantic models for the staging (large-file / LLM-safe) upload channel.

File bytes travel through a direct-upload channel so that GPT Actions / Claude
only ever exchange small JSON (an ``upload_id`` then a ``job_id``). Staging
records are persisted as one JSON file per upload under
``SAFE_MEMORY_ROOT/uploads/<upload_id>/upload.json`` (no database).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class UploadStatus(str, Enum):
    INITIALIZED = "initialized"
    RECEIVED = "received"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class UploadInitRequest(BaseModel):
    """Client asks to stage a file; no bytes are sent here."""

    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None


class UploadInitResponse(BaseModel):
    """Where and how to upload the raw bytes."""

    upload_id: str
    upload_url: str
    upload_token: str
    method: str = "PUT"
    expires_at: Optional[str] = None


class UploadContentResponse(BaseModel):
    """Result of streaming the raw bytes to the staging channel."""

    upload_id: str
    received: bool = True
    size: int = 0


class UploadRecord(BaseModel):
    """Persisted staging-upload metadata.

    ``rel_path`` and ``storage_backend`` are server-side details and are never
    returned to clients.
    """

    upload_id: str
    filename: str
    content_type: Optional[str] = None
    declared_size: Optional[int] = None
    actual_size: Optional[int] = None
    status: UploadStatus = UploadStatus.INITIALIZED
    upload_token: str
    created_at: str = Field(default_factory=_utcnow)
    expires_at: Optional[str] = None
    storage_backend: str = "local"
    rel_path: Optional[str] = None
