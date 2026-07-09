"""Pydantic models for job / session retention tracking.

Jobs track the lifecycle of an upload -> pack build, and control how long raw
uploads, working files, and generated packs are retained. Metadata is persisted
as one small JSON file per job under ``SAFE_MEMORY_ROOT/jobs/`` (no database).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


class RetentionMode(str, Enum):
    """How long generated data is kept after a job completes."""

    SESSION = "session"
    PROCESS_AND_RETURN = "process_and_return"
    SERVER_VAULT = "server_vault"


class Job(BaseModel):
    """Legacy lightweight job model (kept for backward compatibility)."""

    id: str
    kind: str
    status: JobStatus = JobStatus.PENDING
    detail: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)


class JobRecord(BaseModel):
    """Full retention-aware job record persisted under ``jobs/<job_id>.json``.

    ``pack_path`` and ``working_dir`` are server-relative paths used internally
    for download and cleanup; they are never returned to clients unless
    ``debug=true``.
    """

    job_id: str
    agent_id: str
    pack_id: str
    status: JobStatus = JobStatus.PENDING
    retention_mode: RetentionMode = RetentionMode.PROCESS_AND_RETURN
    created_at: str = Field(default_factory=_utcnow)
    expires_at: Optional[str] = None
    raw_upload_deleted: bool = False
    working_files_deleted: bool = False
    pack_persisted: bool = False
    download_url: Optional[str] = None
    entry_count: int = 0
    classification_counts: Dict[str, int] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    # Ingestion / OSS handoff metadata (all optional for backward compat).
    input_type: Optional[str] = None  # "file" | "folder" | "zip"
    catalog_visible: bool = False
    oss_export_uploaded: bool = False
    oss_object_key: Optional[str] = None
    expires_at_url: Optional[str] = None
    unsupported_files: List[Dict[str, str]] = Field(default_factory=list)

    # Stable, signature-free download token (maps to /api/packs/dl/{token}).
    download_token: Optional[str] = None

    # Server-side only (relative to SAFE_MEMORY_ROOT). Never exposed unless debug.
    pack_path: Optional[str] = None
    working_dir: Optional[str] = None


class JobResponse(BaseModel):
    """Public, path-safe view of a job returned by the jobs API."""

    job_id: str
    agent_id: str
    pack_id: str
    status: JobStatus
    retention_mode: RetentionMode
    created_at: str
    expires_at: Optional[str] = None
    raw_upload_deleted: bool = False
    working_files_deleted: bool = False
    pack_persisted: bool = False
    download_url: Optional[str] = Field(
        default=None,
        description=(
            "Signature-free stable link to download the .smp.json pack; hand this "
            "to users/agents or pass to importPackByRef. Present once the build "
            "COMPLETED; null while PROCESSING or on FAILED. This link MUST be "
            "surfaced to the end user as a clickable link whenever it is present; "
            "do not omit it from the reply."
        ),
    )
    entry_count: int = 0
    classification_counts: Dict[str, int] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    # Ingestion / OSS handoff metadata.
    input_type: Optional[str] = None
    catalog_visible: bool = False
    oss_export_uploaded: bool = False
    oss_object_key: Optional[str] = None
    expires_at_url: Optional[str] = None
    unsupported_files: List[Dict[str, str]] = Field(default_factory=list)

    # Set only on the bounded-synchronous build fallback (status PROCESSING),
    # so callers can reference the staged upload while polling GET /api/jobs.
    upload_id: Optional[str] = None

    # Human-readable, ready-to-display result line. Models tend to relay this
    # verbatim, so it always carries the download link when the pack is ready.
    message: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable result message intended to be shown to the end user "
            "verbatim. When the pack is ready this line contains the download "
            "link; always relay it to the user."
        ),
    )

    # Populated only when debug=true.
    pack_path: Optional[str] = None
    working_dir: Optional[str] = None


class CleanupSummary(BaseModel):
    """Result of a bulk expired-job cleanup pass."""

    jobs_cleaned: int = 0
    packs_deleted: int = 0
    working_dirs_deleted: int = 0
    uploads_deleted: int = 0
    job_ids: List[str] = Field(default_factory=list)


def _dl_url(token: str) -> str:
    """Build the stable download URL for a token from the public base (or relative)."""
    from app.config import settings

    base = (settings.safe_memory_public_base_url or "").strip().rstrip("/")
    rel = f"/api/packs/dl/{token}"
    return f"{base}{rel}" if base else rel


def _result_message(job: "JobRecord", download_url: Optional[str]) -> Optional[str]:
    """Build a human-readable, ready-to-display result line for a job.

    Models tend to relay this verbatim, so when the pack is ready it always
    carries the download link (independent of GPT instructions).
    """
    if job.status == JobStatus.COMPLETED and download_url:
        return (
            f"\u2705 Memory pack '{job.pack_id}' is ready \u2014 "
            f"{job.entry_count} entries, retention={job.retention_mode.value}. "
            f"Download the .smp.json here: {download_url}  "
            f"(share this link or pass it to importPackByRef)."
        )
    if job.status == JobStatus.COMPLETED:
        # Unusual: completed without a link. Report readiness without a Download line.
        return (
            f"\u2705 Memory pack '{job.pack_id}' is ready \u2014 "
            f"{job.entry_count} entries, retention={job.retention_mode.value}."
        )
    if job.status == JobStatus.PROCESSING:
        return (
            f"\u23f3 Build in progress (job {job.job_id}). Call getJob with this "
            f"job_id until status is COMPLETED, then present the download link to "
            f"the user."
        )
    if job.status == JobStatus.FAILED:
        detail = "; ".join(job.warnings) if job.warnings else "See job warnings."
        return f"\u274c Build failed for pack '{job.pack_id}'. {detail}"
    return None


def job_to_response(job: JobRecord, debug: bool = False) -> JobResponse:
    """Build a client-safe :class:`JobResponse`, hiding server paths by default.

    ``download_url`` is a stable, signature-free token URL (``/api/packs/dl/``)
    when the job carries a ``download_token``. That URL streams the local pack or
    307-redirects to a freshly signed OSS URL, so a raw OSS signed URL (whose
    base64 signature can be corrupted when passed through GPT/ChatGPT) is never
    returned here.
    """
    if job.download_token:
        download_url = _dl_url(job.download_token)
    else:
        download_url = job.download_url
    resp = JobResponse(
        job_id=job.job_id,
        agent_id=job.agent_id,
        pack_id=job.pack_id,
        status=job.status,
        retention_mode=job.retention_mode,
        created_at=job.created_at,
        expires_at=job.expires_at,
        raw_upload_deleted=job.raw_upload_deleted,
        working_files_deleted=job.working_files_deleted,
        pack_persisted=job.pack_persisted,
        download_url=download_url,
        entry_count=job.entry_count,
        classification_counts=job.classification_counts,
        warnings=job.warnings,
        input_type=job.input_type,
        catalog_visible=job.catalog_visible,
        oss_export_uploaded=job.oss_export_uploaded,
        oss_object_key=job.oss_object_key,
        expires_at_url=job.expires_at_url,
        unsupported_files=job.unsupported_files,
    )
    resp.message = _result_message(job, download_url)
    if debug:
        resp.pack_path = job.pack_path
        resp.working_dir = job.working_dir
    return resp
