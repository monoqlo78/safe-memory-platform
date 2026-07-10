"""One-time (SAS-like) upload links.

Lets an authenticated LLM (GPT Action) mint a keyless, single-use upload URL.
The end user opens ``/u/{token}``, drops a folder, and the resulting pack is
bound to a claim the LLM can poll for. The master API key is never exposed to
the anonymous user and is never returned or logged.

Endpoints:
- POST /api/upload-links          -> createUploadLink (master key, visible)
- GET  /api/upload-links/status   -> poll by token, for the /u page (token-auth)
- GET  /api/upload-links/{claim}  -> getUploadLinkResult (master key, visible)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.core import jobs_store, upload_links
from app.models.job_schema import job_to_response
from app.models.upload_link_schema import (
    CreateUploadLinkRequest,
    CreateUploadLinkResponse,
    ImportedPackInfo,
    UploadLinkClaim,
    UploadLinkResultResponse,
)

logger = logging.getLogger("safe_memory.upload_links")

router = APIRouter(prefix="/api/upload-links", tags=["upload-links"])


def _public_base(request: Request) -> str:
    public = (settings.safe_memory_public_base_url or "").strip()
    if public:
        return public.rstrip("/")
    return str(request.base_url).rstrip("/")


def _claim_result(claim: UploadLinkClaim) -> UploadLinkResultResponse:
    """Resolve a claim to its current, path-safe result view."""
    mode = getattr(claim, "mode", "build") or "build"

    # Import mode: several packs may be registered under the claim's ephemeral
    # namespace. Return the safe summaries so the LLM knows what to query.
    if mode == "import":
        imported = [
            ImportedPackInfo(
                agent_id=item.get("agent_id", ""),
                pack_id=item.get("pack_id", ""),
                entry_count=int(item.get("entry_count", 0) or 0),
                classifications=dict(item.get("classifications", {}) or {}),
                verified=bool(item.get("verified", False)),
            )
            for item in (claim.imported or [])
        ]
        if claim.is_expired():
            status = "EXPIRED"
        elif imported:
            status = "COMPLETED"
        else:
            status = "PENDING"
        return UploadLinkResultResponse(
            status=status,
            claim_id=claim.claim_id,
            mode="import",
            imported=imported,
        )

    if claim.job_id:
        job = jobs_store.load_job(claim.job_id)
        if job is not None:
            view = job_to_response(job)
            return UploadLinkResultResponse(
                status=view.status.value,
                claim_id=claim.claim_id,
                job_id=job.job_id,
                pack_id=view.pack_id,
                download_url=view.download_url,
                entry_count=view.entry_count,
                input_type=view.input_type,
                unsupported_files=view.unsupported_files,
                mode="build",
            )
    status = "EXPIRED" if claim.is_expired() else "PENDING"
    return UploadLinkResultResponse(status=status, claim_id=claim.claim_id, mode="build")


@router.post(
    "",
    response_model=CreateUploadLinkResponse,
    operation_id="createUploadLink",
    include_in_schema=True,
    summary="Create a one-time, keyless upload link",
    description=(
        "Mint a single-use upload URL a person opens with no login or API key "
        "to drop files. Returns upload_url, claim_id, expires_at. Share it, "
        "then poll getUploadLinkResult with claim_id until COMPLETED. Set "
        "mode=import to upload finished .smp.json packs into a private "
        "temporary space instead of building."
    ),
)
def create_upload_link(
    req: CreateUploadLinkRequest, request: Request
) -> CreateUploadLinkResponse:
    """Create a scoped one-time upload link and return its public URL."""
    token = upload_links.new_token()
    mode = "import" if (req.mode or "build").strip().lower() == "import" else "build"
    # Import-mode links register finished packs into a private, unguessable,
    # TTL-expired namespace; they never touch the shared server vault.
    import_agent_id = upload_links.new_import_agent_id() if mode == "import" else None
    claim = UploadLinkClaim(
        claim_id=upload_links.new_claim_id(),
        token=token,
        agent_id=(req.agent_id or "shared").strip() or "shared",
        pack_id=(req.pack_id or f"upload-{uuid.uuid4().hex[:8]}").strip(),
        title=req.title or "Uploaded via one-time link",
        source_language=req.source_language,
        canonical_language=req.canonical_language or "en",
        retention_mode=req.retention_mode or "process_and_return",
        default_classification=req.classification or "internal",
        expires_at=upload_links.compute_expires_at(req.expires_in_seconds),
        max_uses=max(1, int(req.max_uses or 1)),
        mode=mode,
        import_agent_id=import_agent_id,
    )
    upload_links.save_claim(claim)

    upload_url = f"{_public_base(request)}/u/{token}"
    return CreateUploadLinkResponse(
        upload_url=upload_url,
        claim_id=claim.claim_id,
        expires_at=claim.expires_at,
        mode=mode,
    )


@router.get(
    "/status",
    response_model=UploadLinkResultResponse,
    operation_id="getUploadLinkStatusByToken",
    include_in_schema=False,
    summary="Poll a one-time upload link by token (for the /u page)",
)
def upload_link_status(token: str = Query(...)) -> UploadLinkResultResponse:
    """Token-scoped status poll used by the anonymous /u/{token} page."""
    claim = upload_links.find_claim_by_token((token or "").strip())
    if claim is None:
        raise HTTPException(status_code=401, detail="Invalid or expired upload token.")
    return _claim_result(claim)


@router.get(
    "/{claim_id}",
    response_model=UploadLinkResultResponse,
    operation_id="getUploadLinkResult",
    include_in_schema=True,
    summary="Get the result of a one-time upload link",
    description=(
        "Poll a one-time upload link by claim_id. Returns status "
        "(PENDING|PROCESSING|COMPLETED|FAILED|EXPIRED). When COMPLETED it "
        "includes download_url (a shareable signed URL), pack_id, entry_count, "
        "input_type, and unsupported_files, ready for importPackByRef."
    ),
)
def get_upload_link_result(claim_id: str) -> UploadLinkResultResponse:
    """Resolve a claim to its current status/result for the calling LLM."""
    claim = upload_links.load_claim(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Unknown claim_id.")
    return _claim_result(claim)
