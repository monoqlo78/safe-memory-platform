"""Persistence and cleanup for retention-aware jobs.

Job metadata is stored as one JSON file per job under ``SAFE_MEMORY_ROOT/jobs/``.
Raw uploads and intermediate files live in a per-job working directory. All file
operations are confined to ``SAFE_MEMORY_ROOT`` and path-safe.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core import pack_io
from app.models.job_schema import JobRecord, JobStatus, RetentionMode

logger = logging.getLogger("safe_memory.jobs")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_expires_at(retention_mode: RetentionMode) -> Optional[str]:
    """Return the ISO expiry for temp modes, or None for server_vault."""
    if retention_mode == RetentionMode.SERVER_VAULT:
        return None
    ttl = max(0, int(settings.safe_memory_temp_ttl_minutes))
    return (_utcnow() + timedelta(minutes=ttl)).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_job(job: JobRecord) -> Path:
    """Persist a job record to ``jobs/<job_id>.json``."""
    path = pack_io.job_meta_path(job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = job.model_dump(mode="json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def load_job(job_id: str) -> Optional[JobRecord]:
    """Load a job record, or return None if it does not exist / is invalid."""
    try:
        path = pack_io.job_meta_path(job_id)
    except pack_io.UnsafePathError:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return JobRecord.model_validate(json.load(fh))
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not load job record for %s", job_id)
        return None


def list_jobs() -> List[JobRecord]:
    """Return all persisted job records."""
    base = pack_io.jobs_dir()
    jobs: List[JobRecord] = []
    if not base.exists():
        return jobs
    for candidate in sorted(base.glob("*.json")):
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                jobs.append(JobRecord.model_validate(json.load(fh)))
        except Exception:
            continue
    return jobs


# ---------------------------------------------------------------------------
# Working files (raw upload + intermediates)
# ---------------------------------------------------------------------------
def write_working_upload(job_id: str, filename: str, data: bytes) -> str:
    """Write the raw upload into the per-job working dir; return relative path."""
    work_dir = pack_io.job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename or "upload.bin").name or "upload.bin"
    target = pack_io.ensure_safe_path(work_dir / safe_name)
    with target.open("wb") as fh:
        fh.write(data)
    return pack_io.relpath_from_root(work_dir)


def _delete_dir_safe(rel_or_abs: Optional[str]) -> bool:
    """Recursively delete a directory, confined to the sandbox. Return True if removed."""
    if not rel_or_abs:
        return False
    try:
        path = pack_io.ensure_safe_path(rel_or_abs)
    except pack_io.UnsafePathError:
        logger.warning("Refused to delete unsafe path.")
        return False
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False


def _delete_file_safe(rel_or_abs: Optional[str]) -> bool:
    """Delete a single file, confined to the sandbox. Return True if removed."""
    if not rel_or_abs:
        return False
    try:
        path = pack_io.ensure_safe_path(rel_or_abs)
    except pack_io.UnsafePathError:
        logger.warning("Refused to delete unsafe path.")
        return False
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:  # pragma: no cover - defensive
            return False
    return False


def delete_working_files(job: JobRecord) -> bool:
    """Delete a job's working dir (raw upload + intermediates)."""
    removed = _delete_dir_safe(job.working_dir)
    # Also remove the parent jobs/<job_id> dir if now empty.
    if job.working_dir:
        try:
            parent = pack_io.ensure_safe_path(job.working_dir).parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass
    return removed


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_job(job_id: str) -> Optional[JobRecord]:
    """Force-clean a single job.

    Deletes the raw upload and working files, and (for non server_vault modes)
    the temporary pack. server_vault packs are preserved. Returns the updated
    job record, or None if the job does not exist.
    """
    job = load_job(job_id)
    if job is None:
        return None

    if delete_working_files(job) or job.raw_upload_deleted:
        job.raw_upload_deleted = True
        job.working_files_deleted = True

    if job.retention_mode != RetentionMode.SERVER_VAULT:
        _delete_file_safe(job.pack_path)
        job.pack_persisted = False

    job.status = JobStatus.DELETED
    save_job(job)
    return job


def cleanup_expired_temp_jobs() -> dict:
    """Delete temp packs / working files for jobs whose expiry has passed.

    server_vault packs are never removed. Returns a summary dict.
    """
    now = _utcnow()
    jobs_cleaned = 0
    packs_deleted = 0
    working_dirs_deleted = 0
    cleaned_ids: List[str] = []

    for job in list_jobs():
        if job.retention_mode == RetentionMode.SERVER_VAULT:
            continue
        if job.status in (JobStatus.EXPIRED, JobStatus.DELETED):
            continue
        expires = _parse_iso(job.expires_at)
        if expires is None or expires > now:
            continue

        if _delete_file_safe(job.pack_path):
            packs_deleted += 1
        if delete_working_files(job):
            working_dirs_deleted += 1

        job.status = JobStatus.EXPIRED
        job.pack_persisted = False
        job.raw_upload_deleted = True
        job.working_files_deleted = True
        job.warnings = list(job.warnings) + [
            "Temporary pack and working files expired and were deleted."
        ]
        save_job(job)

        jobs_cleaned += 1
        cleaned_ids.append(job.job_id)

    return {
        "jobs_cleaned": jobs_cleaned,
        "packs_deleted": packs_deleted,
        "working_dirs_deleted": working_dirs_deleted,
        "job_ids": cleaned_ids,
    }
