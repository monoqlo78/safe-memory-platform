"""Alibaba Cloud OSS handoff service.

Generated packs (and optionally other artifacts) are placed in a **private** OSS
bucket and shared only via short-lived **signed URLs**. Objects are never made
public.

Security rules enforced here:
- The OSS AccessKey id/secret and the signature query string of a signed URL are
  NEVER logged. Logs only ever contain the (non-secret) object key and a
  ``redact_signed_url``-cleaned URL.
- The ``oss2`` SDK is imported lazily so the app runs fine when OSS is disabled
  or the SDK is not installed.
- When OSS is not fully configured, :func:`is_enabled` returns False and callers
  fall back to the existing local tokenized-download flow.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from app.config import settings

logger = logging.getLogger("safe_memory.oss")


class OSSNotConfiguredError(RuntimeError):
    """Raised when an OSS operation is attempted while OSS is disabled."""


def is_enabled() -> bool:
    """True when OSS is enabled AND all required settings are present."""
    return bool(settings.oss_ready)


def redact_signed_url(url: Optional[str]) -> str:
    """Return ``url`` with its query string (the signature) stripped.

    Safe to log or store in audit metadata. An empty/None url returns "".
    """
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _bucket_construction_endpoint() -> str:
    """Return the region endpoint to pass to ``oss2.Bucket(auth, endpoint, bucket)``.

    ``oss2.Bucket`` always expects a *region* endpoint (e.g.
    ``https://oss-ap-southeast-1.aliyuncs.com``) and prepends the bucket name
    itself. Passing a *virtual-hosted* endpoint (``https://<bucket>.oss-...``)
    here would double-prefix the host (``<bucket>.<bucket>.oss-...``) and fail
    DNS resolution. So we prefer ``oss_endpoint`` and, as a guard, strip a
    leading ``<bucket>.`` label if a virtual-hosted URL ever slips in.
    """
    endpoint = (settings.oss_endpoint or settings.oss_bucket_endpoint or "").strip()
    bucket = (settings.oss_bucket or "").strip()
    if not endpoint:
        return endpoint
    parts = urlsplit(endpoint if "//" in endpoint else "https://" + endpoint)
    host = parts.netloc or parts.path
    if bucket and host.startswith(bucket + "."):
        host = host[len(bucket) + 1 :]
    scheme = parts.scheme or "https"
    return urlunsplit((scheme, host, "", "", ""))


def _get_bucket():
    """Lazily build and return an oss2 Bucket. Overridden in tests.

    Imports ``oss2`` only when actually needed so the app imports cleanly
    without the SDK installed.
    """
    if not is_enabled():
        raise OSSNotConfiguredError("OSS is not enabled or not fully configured.")
    import oss2  # local import: optional dependency

    auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
    endpoint = _bucket_construction_endpoint()
    return oss2.Bucket(auth, endpoint, settings.oss_bucket)


def upload_file(
    local_path: str, object_key: str, content_type: Optional[str] = None
) -> dict:
    """Upload a local file to ``object_key`` in the private bucket.

    Returns a small dict with the object key and etag. Never logs secrets.
    """
    bucket = _get_bucket()
    headers = {"Content-Type": content_type} if content_type else None
    result = bucket.put_object_from_file(object_key, local_path, headers=headers)
    etag = getattr(result, "etag", None)
    logger.info("Uploaded object to OSS: %s", object_key)  # key only, no secret
    return {"object_key": object_key, "etag": etag}


def upload_bytes(
    data: bytes, object_key: str, content_type: Optional[str] = None
) -> dict:
    """Upload in-memory bytes to ``object_key``. Returns object key + etag."""
    bucket = _get_bucket()
    headers = {"Content-Type": content_type} if content_type else None
    result = bucket.put_object(object_key, data, headers=headers)
    etag = getattr(result, "etag", None)
    logger.info("Uploaded object to OSS: %s", object_key)
    return {"object_key": object_key, "etag": etag}


def generate_signed_download_url(object_key: str, ttl_seconds: Optional[int] = None) -> str:
    """Return a short-lived signed GET URL for ``object_key`` (private object)."""
    bucket = _get_bucket()
    ttl = int(ttl_seconds or settings.oss_signed_url_ttl_seconds)
    url = bucket.sign_url("GET", object_key, ttl, slash_safe=True)
    logger.info("Signed download URL for %s", object_key)  # never log the url query
    return url


def generate_signed_upload_url(
    object_key: str,
    ttl_seconds: Optional[int] = None,
    content_type: Optional[str] = None,
) -> str:
    """Return a short-lived signed PUT URL a client can upload to directly."""
    bucket = _get_bucket()
    ttl = int(ttl_seconds or settings.oss_signed_url_ttl_seconds)
    headers = {"Content-Type": content_type} if content_type else None
    url = bucket.sign_url("PUT", object_key, ttl, slash_safe=True, headers=headers)
    logger.info("Signed upload URL for %s", object_key)
    return url


def delete_object(object_key: str) -> bool:
    """Delete ``object_key`` from the bucket. Returns True on success."""
    try:
        bucket = _get_bucket()
        bucket.delete_object(object_key)
        logger.info("Deleted OSS object: %s", object_key)
        return True
    except OSSNotConfiguredError:
        raise
    except Exception:  # pragma: no cover - defensive; never leak details
        logger.warning("Failed to delete OSS object: %s", object_key)
        return False


def object_exists(object_key: str) -> bool:
    """Return True when ``object_key`` exists in the bucket."""
    bucket = _get_bucket()
    return bool(bucket.object_exists(object_key))
