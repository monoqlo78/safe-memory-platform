"""Staging upload channel for large / LLM-safe file ingestion.

LLMs (GPT Actions, Claude) cannot send multipart file bytes and have strict
response-size/timeout limits. This router lets a client:

1. POST /api/uploads/init            -> get an upload_id + absolute upload_url + token
2. PUT  /api/uploads/{id}/content    -> stream the raw bytes (token-auth, no API key)

The bytes never pass through the LLM; only small JSON does. Processing is then
kicked off via POST /api/packs/build-from-upload-ref.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.config import settings
from app.core import storage
from app.core.auth import UploadAuthContext, require_upload_or_token
from app.core.storage import LocalStorage
from app.models.upload_schema import (
    UploadContentResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadRecord,
    UploadStatus,
)

logger = logging.getLogger("safe_memory.uploads")

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _max_upload_bytes() -> int:
    return max(1, int(settings.safe_memory_max_upload_mb)) * 1024 * 1024


def _base_url(request: Request) -> str:
    """Absolute base URL for building the upload_url (public URL preferred)."""
    public = (settings.safe_memory_public_base_url or "").strip()
    if public:
        return public.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.post(
    "/init",
    response_model=UploadInitResponse,
    operation_id="initUpload",
    include_in_schema=False,
    summary="Initialize a staged file upload",
    description=(
        "Reserve a staging slot for a large file. Returns an upload_id, an "
        "absolute upload_url, and a one-time upload_token. PUT the raw bytes to "
        "upload_url?token=..., then call buildMemoryPackFromUploadRef. Keeps file "
        "bytes off the LLM channel. API-key protected."
    ),
)
def init_upload(
    req: UploadInitRequest,
    request: Request,
    auth: UploadAuthContext = Depends(require_upload_or_token),
) -> UploadInitResponse:
    """Create a staging record and return where to PUT the bytes."""
    if not (req.filename or "").strip():
        raise HTTPException(status_code=400, detail="filename is required.")

    upload_id = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(24)
    expires_at = storage.compute_upload_expires_at()

    record = UploadRecord(
        upload_id=upload_id,
        filename=req.filename,
        content_type=req.content_type,
        declared_size=req.size,
        status=UploadStatus.INITIALIZED,
        upload_token=token,
        expires_at=expires_at,
        storage_backend=storage.get_storage().backend_name,
    )
    storage.save_upload_record(record)

    upload_url = f"{_base_url(request)}/api/uploads/{upload_id}/content"
    return UploadInitResponse(
        upload_id=upload_id,
        upload_url=upload_url,
        upload_token=token,
        method="PUT",
        expires_at=expires_at,
    )


@router.put(
    "/{upload_id}/content",
    response_model=UploadContentResponse,
    operation_id="uploadContent",
    include_in_schema=False,
    summary="Upload raw file bytes to a staged slot",
    description=(
        "Stream the raw file body (or multipart 'file') to a staged slot, "
        "authorized by the upload_token from initUpload (no API key needed so a "
        "browser can upload directly). Enforces the max upload size (413 if "
        "exceeded). Returns the received size."
    ),
)
async def upload_content(
    upload_id: str,
    request: Request,
    token: Optional[str] = Query(default=None),
) -> UploadContentResponse:
    """Receive and store the raw bytes for a staged upload."""
    record = storage.load_upload_record(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id.")

    if not token or not hmac.compare_digest(token, record.upload_token):
        raise HTTPException(status_code=403, detail="Invalid or missing upload token.")

    store = storage.get_storage()
    max_bytes = _max_upload_bytes()
    mb = settings.safe_memory_max_upload_mb
    filename = record.filename or "upload.bin"
    total = 0

    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="Missing 'file' part.")
        data = await upload.read()
        total = len(data)
        if total > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"Upload exceeds the {mb} MB limit."
            )
        filename = getattr(upload, "filename", None) or filename
        store.save(upload_id, filename, data)
    elif isinstance(store, LocalStorage):
        # Stream the raw body straight to disk with an enforced size cap.
        dest = store.reserve(upload_id, filename)
        fh = dest.open("wb")
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_bytes:
                    fh.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413, detail=f"Upload exceeds the {mb} MB limit."
                    )
                fh.write(chunk)
        finally:
            if not fh.closed:
                fh.close()
    else:
        data = await request.body()
        total = len(data)
        if total > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"Upload exceeds the {mb} MB limit."
            )
        store.save(upload_id, filename, data)

    if total == 0:
        raise HTTPException(status_code=400, detail="Uploaded body is empty.")

    record.filename = filename
    record.actual_size = total
    record.status = UploadStatus.RECEIVED
    record.rel_path = store.path_for(upload_id)
    storage.save_upload_record(record)

    return UploadContentResponse(upload_id=upload_id, received=True, size=total)
