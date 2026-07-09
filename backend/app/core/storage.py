"""Storage abstraction for staged (large-file) uploads.

File bytes are staged separately from the small JSON the LLM exchanges. The
default :class:`LocalStorage` keeps them under ``SAFE_MEMORY_ROOT/uploads/`` and
is fully sandbox-confined (path-safe) like :mod:`app.core.pack_io`. An
:class:`OSSStorage` stub marks where an Alibaba OSS backend can be added later
WITHOUT touching callers -- select it via ``SAFE_MEMORY_STORAGE_BACKEND=oss``.

Staging records (metadata) are persisted here too, as one JSON file per upload,
so cleanup of expired-but-never-consumed uploads lives alongside the bytes.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, List, Optional, Union

from app.config import settings
from app.core import pack_io
from app.models.upload_schema import UploadRecord, UploadStatus

logger = logging.getLogger("safe_memory.storage")

_UPLOADS_DIR = "uploads"
_META_NAME = "upload.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def uploads_root() -> Path:
    """Return the staging root directory (path-safe)."""
    return pack_io.ensure_safe_path(Path(_UPLOADS_DIR))


def upload_dir(upload_id: str) -> Path:
    """Return the per-upload staging directory (path-safe)."""
    safe = pack_io._sanitize_segment(upload_id)
    return pack_io.ensure_safe_path(Path(_UPLOADS_DIR) / safe)


def _meta_path(upload_id: str) -> Path:
    return pack_io.ensure_safe_path(upload_dir(upload_id) / _META_NAME)


def compute_upload_expires_at() -> str:
    ttl = max(0, int(settings.safe_memory_upload_ttl_minutes))
    return (_utcnow() + timedelta(minutes=ttl)).isoformat()


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------
class Storage:
    """Minimal storage interface for staged upload bytes."""

    backend_name = "base"

    def save(self, upload_id: str, filename: str, data: Union[bytes, IO[bytes]]) -> str:
        raise NotImplementedError

    def path_for(self, upload_id: str) -> Optional[str]:
        raise NotImplementedError

    def open(self, upload_id: str) -> Optional[bytes]:
        raise NotImplementedError

    def delete(self, upload_id: str) -> bool:
        raise NotImplementedError

    def exists(self, upload_id: str) -> bool:
        raise NotImplementedError


class LocalStorage(Storage):
    """Stages upload bytes under ``SAFE_MEMORY_ROOT/uploads/<id>/<filename>``."""

    backend_name = "local"

    def _dest(self, upload_id: str, filename: str) -> Path:
        safe_name = Path(filename or "upload.bin").name or "upload.bin"
        safe_name = pack_io._sanitize_segment(safe_name)
        target = pack_io.ensure_safe_path(upload_dir(upload_id) / safe_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def save(self, upload_id: str, filename: str, data: Union[bytes, IO[bytes]]) -> str:
        target = self._dest(upload_id, filename)
        if hasattr(data, "read"):
            with target.open("wb") as fh:
                shutil.copyfileobj(data, fh)
        else:
            with target.open("wb") as fh:
                fh.write(data)  # type: ignore[arg-type]
        return pack_io.relpath_from_root(target)

    def reserve(self, upload_id: str, filename: str) -> Path:
        """Return a writable destination path (dir ensured). Local-only helper
        used for streaming request bodies straight to disk."""
        return self._dest(upload_id, filename)

    def _find_file(self, upload_id: str) -> Optional[Path]:
        base = upload_dir(upload_id)
        if not base.exists():
            return None
        for candidate in sorted(base.iterdir()):
            if candidate.is_file() and candidate.name != _META_NAME:
                return candidate
        return None

    def path_for(self, upload_id: str) -> Optional[str]:
        found = self._find_file(upload_id)
        return pack_io.relpath_from_root(found) if found else None

    def open(self, upload_id: str) -> Optional[bytes]:
        found = self._find_file(upload_id)
        if found is None:
            return None
        with found.open("rb") as fh:
            return fh.read()

    def delete(self, upload_id: str) -> bool:
        base = upload_dir(upload_id)
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
            return True
        return False

    def exists(self, upload_id: str) -> bool:
        return self._find_file(upload_id) is not None


class OSSStorage(Storage):
    """Stub for a future Alibaba Cloud OSS backend.

    Implement these methods against the OSS SDK to move staged bytes off the
    local disk WITHOUT changing any callers (they use :func:`get_storage`).
    """

    backend_name = "oss"

    def save(self, upload_id: str, filename: str, data: Union[bytes, IO[bytes]]) -> str:
        raise NotImplementedError("OSS storage backend is not implemented yet.")

    def path_for(self, upload_id: str) -> Optional[str]:
        raise NotImplementedError("OSS storage backend is not implemented yet.")

    def open(self, upload_id: str) -> Optional[bytes]:
        raise NotImplementedError("OSS storage backend is not implemented yet.")

    def delete(self, upload_id: str) -> bool:
        raise NotImplementedError("OSS storage backend is not implemented yet.")

    def exists(self, upload_id: str) -> bool:
        raise NotImplementedError("OSS storage backend is not implemented yet.")


def get_storage() -> Storage:
    """Return the storage backend selected by SAFE_MEMORY_STORAGE_BACKEND."""
    backend = (settings.safe_memory_storage_backend or "local").strip().lower()
    if backend == "oss":
        return OSSStorage()
    return LocalStorage()


# ---------------------------------------------------------------------------
# Staging record persistence
# ---------------------------------------------------------------------------
def save_upload_record(record: UploadRecord) -> Path:
    path = _meta_path(record.upload_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(record.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def load_upload_record(upload_id: str) -> Optional[UploadRecord]:
    try:
        path = _meta_path(upload_id)
    except pack_io.UnsafePathError:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return UploadRecord.model_validate(json.load(fh))
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not load upload record for %s", upload_id)
        return None


def list_upload_records() -> List[UploadRecord]:
    base = uploads_root()
    records: List[UploadRecord] = []
    if not base.exists():
        return records
    for meta in sorted(base.glob(f"*/{_META_NAME}")):
        try:
            with meta.open("r", encoding="utf-8") as fh:
                records.append(UploadRecord.model_validate(json.load(fh)))
        except Exception:
            continue
    return records


def delete_upload(upload_id: str) -> bool:
    """Delete a staged upload's bytes and metadata (sandbox-confined)."""
    base = upload_dir(upload_id)
    try:
        safe = pack_io.ensure_safe_path(base)
    except pack_io.UnsafePathError:
        logger.warning("Refused to delete unsafe upload path.")
        return False
    if safe.exists():
        shutil.rmtree(safe, ignore_errors=True)
        return True
    return False


def cleanup_expired_uploads() -> int:
    """Delete staged uploads whose TTL passed and were never consumed.

    Returns the number of uploads removed.
    """
    now = _utcnow()
    removed = 0
    for record in list_upload_records():
        if record.status == UploadStatus.CONSUMED:
            continue
        expires = _parse_iso(record.expires_at)
        if expires is None or expires > now:
            continue
        if delete_upload(record.upload_id):
            removed += 1
    return removed
