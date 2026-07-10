"""Simple shared-key authentication for the Safe Memory Platform.

A single ``X-Safe-Memory-Key`` header gates all ``/api/*`` routes when
``SAFE_MEMORY_API_KEY`` is configured. Health and docs endpoints are always
open. The key itself is never logged.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger("safe_memory.auth")

API_KEY_HEADER = "X-Safe-Memory-Key"
UPLOAD_TOKEN_HEADER = "X-Upload-Token"

# Only these path prefixes are protected. Everything else (/, /health, /docs,
# /openapi.json, /redoc, static assets) stays open.
_PROTECTED_PREFIX = "/api/"

# These exact /api paths do their own authorization inside the endpoint (they
# accept the master key OR a scoped one-time upload token), so the blanket
# middleware key-check is skipped for them.
_SELF_AUTHED_PATHS = frozenset(
    {
        "/api/uploads/init",
        "/api/packs/build-from-upload-ref",
        "/api/packs/import-from-upload-ref",
        "/api/upload-links/status",
    }
)

_warned_dev_mode = False


def _constant_time_equals(provided: str, expected: str) -> bool:
    """Compare two strings without leaking length/timing information."""
    return hmac.compare_digest((provided or "").encode("utf-8"),
                               (expected or "").encode("utf-8"))


def log_auth_mode_once() -> None:
    """Log a single warning at startup when auth is disabled (dev mode)."""
    global _warned_dev_mode
    if settings.auth_enabled:
        logger.info("API key authentication is ENABLED for /api/* routes.")
        return
    if not _warned_dev_mode:
        logger.warning(
            "SAFE_MEMORY_API_KEY is not set: running in DEV MODE with no "
            "authentication. Do NOT expose this deployment publicly. Set "
            "SAFE_MEMORY_API_KEY to require the %s header.",
            API_KEY_HEADER,
        )
        _warned_dev_mode = True


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Require a valid API key header on protected ``/api/*`` routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only guard the API surface; leave health/docs/openapi/redoc open.
        if not path.startswith(_PROTECTED_PREFIX):
            return await call_next(request)

        # The raw-bytes upload endpoint is authorized by its own upload_token
        # (so a browser page can upload without the API key), not the API key.
        if path.startswith("/api/uploads/") and path.endswith("/content"):
            return await call_next(request)

        # Tokenized export downloads are authorized by an unguessable link token
        # (so browsers, Drive, and importPackByRef can fetch without the API key).
        if path.startswith("/api/packs/dl/"):
            return await call_next(request)

        # Endpoints that accept a scoped one-time upload token authorize
        # themselves (master key OR X-Upload-Token) inside the handler.
        if path in _SELF_AUTHED_PATHS:
            return await call_next(request)

        # Dev mode: no key configured means all requests are allowed.
        if not settings.auth_enabled:
            return await call_next(request)

        provided = request.headers.get(API_KEY_HEADER, "")
        if not provided or not _constant_time_equals(
            provided, settings.safe_memory_api_key
        ):
            # Never log the provided or expected key value.
            logger.warning("Rejected unauthorized request to %s", path)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        f"Missing or invalid {API_KEY_HEADER} header."
                    )
                },
            )

        return await call_next(request)


def _has_master_key(request: Request) -> bool:
    provided = request.headers.get(API_KEY_HEADER, "")
    return bool(provided) and _constant_time_equals(
        provided, settings.safe_memory_api_key
    )


@dataclass
class UploadAuthContext:
    """Result of authorizing a staged-upload request.

    ``mode`` is ``"key"`` for the master API key or ``"token"`` for a scoped
    one-time upload link. ``claim`` is populated only in token mode.
    """

    mode: str
    claim: Optional[object] = None


def require_upload_or_token(request: Request) -> UploadAuthContext:
    """Authorize a staged-upload request via master key OR one-time token.

    Used by ``/api/uploads/init`` and ``/api/packs/build-from-upload-ref``.
    A one-time token only grants staging + a single scoped build; it never
    unlocks catalog/query/delete or other jobs. Returns an auth context so the
    handler can apply the claim's settings and consume the use.
    """
    # Local import avoids a circular import at module load time.
    from app.core import upload_links

    token = (request.headers.get(UPLOAD_TOKEN_HEADER) or "").strip()

    # Dev mode (no master key configured): still resolve a token if provided so
    # claim binding works, otherwise treat as an open key request.
    if not settings.auth_enabled:
        if token:
            claim = upload_links.find_usable_claim_by_token(token)
            if claim is not None:
                return UploadAuthContext(mode="token", claim=claim)
        return UploadAuthContext(mode="key")

    if _has_master_key(request):
        return UploadAuthContext(mode="key")

    if token:
        claim = upload_links.find_usable_claim_by_token(token)
        if claim is not None:
            return UploadAuthContext(mode="token", claim=claim)

    logger.warning("Rejected unauthorized upload request to %s", request.url.path)
    raise HTTPException(
        status_code=401,
        detail=(
            f"Missing or invalid {API_KEY_HEADER} header or {UPLOAD_TOKEN_HEADER}."
        ),
    )
