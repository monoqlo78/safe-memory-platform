"""OSS presign endpoints (machine-facing; hidden from the GPT/Claude schema).

These let trusted machine clients obtain short-lived signed OSS URLs to upload to
or download from the private bucket directly, and to delete objects. They are
guarded by the X-Safe-Memory-Key middleware (they live under ``/api``) and are
``include_in_schema=False`` because GPT/Claude cannot use raw byte transfer.

When OSS is disabled or not fully configured, every endpoint returns 503.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core import oss_storage

router = APIRouter(prefix="/api/files", tags=["files"])


class PresignUploadRequest(BaseModel):
    filename: str
    content_type: Optional[str] = None
    prefix: str = "uploads/"


class PresignUploadResponse(BaseModel):
    object_key: str
    upload_url: str
    expires_at: str


class PresignDownloadRequest(BaseModel):
    object_key: str


class PresignDownloadResponse(BaseModel):
    download_url: str
    expires_at: str


class DeleteObjectRequest(BaseModel):
    object_key: str


class DeleteObjectResponse(BaseModel):
    deleted: bool


def _require_oss() -> None:
    if not oss_storage.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="OSS is not enabled or not fully configured on this server.",
        )


def _expires_at() -> str:
    return (
        datetime.now(timezone.utc)
        + timedelta(seconds=settings.oss_signed_url_ttl_seconds)
    ).isoformat()


@router.post(
    "/presign-upload",
    response_model=PresignUploadResponse,
    operation_id="presignUpload",
    include_in_schema=False,
)
def presign_upload(req: PresignUploadRequest) -> PresignUploadResponse:
    """Return a signed PUT URL for a new object under ``prefix``."""
    _require_oss()
    prefix = req.prefix or settings.oss_upload_prefix
    if not prefix.endswith("/"):
        prefix += "/"
    object_key = f"{prefix}{uuid.uuid4().hex}/{req.filename}"
    url = oss_storage.generate_signed_upload_url(
        object_key, content_type=req.content_type
    )
    return PresignUploadResponse(
        object_key=object_key, upload_url=url, expires_at=_expires_at()
    )


@router.post(
    "/presign-download",
    response_model=PresignDownloadResponse,
    operation_id="presignDownload",
    include_in_schema=False,
)
def presign_download(req: PresignDownloadRequest) -> PresignDownloadResponse:
    """Return a signed GET URL for an existing object."""
    _require_oss()
    url = oss_storage.generate_signed_download_url(req.object_key)
    return PresignDownloadResponse(download_url=url, expires_at=_expires_at())


@router.delete(
    "/object",
    response_model=DeleteObjectResponse,
    operation_id="deleteObject",
    include_in_schema=False,
)
def delete_object(req: DeleteObjectRequest) -> DeleteObjectResponse:
    """Delete an object from the private bucket."""
    _require_oss()
    return DeleteObjectResponse(deleted=oss_storage.delete_object(req.object_key))
