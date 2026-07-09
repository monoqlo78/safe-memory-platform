"""SSRF-safe fetching of a Safe Memory Pack from a remote URL.

Used by the ``importPackByRef`` endpoint. File bytes cannot travel through the
LLM, so a URL (plain text) is exchanged instead. Fetching an attacker-supplied
URL is dangerous, so this module enforces:

* HTTPS only (plain http / non-TLS is rejected).
* A hard streamed size cap (``SAFE_MEMORY_MAX_IMPORT_MB``).
* SSRF protection: the target host must not resolve to a private / loopback /
  link-local / reserved / multicast address. Redirects are followed manually and
  every hop is re-checked.

The HTTP client is built via :func:`_build_client` so tests can inject an
``httpx.MockTransport`` (or route to the app) without touching the real network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.config import settings

_MAX_REDIRECTS = 5
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Map common content types to a file extension so the ingest pipeline can pick
# the right reader when neither Content-Disposition nor the URL path carries one.
_EXT_BY_CONTENT_TYPE = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}

_INGEST_EXTS = (
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".docx",
    ".pptx",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tiff",
    ".tif",
    ".bmp",
    ".webp",
)


class ImportRefError(Exception):
    """Raised during a URL import with an HTTP-friendly status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _build_client() -> httpx.Client:
    """Build the HTTP client used to fetch a pack. Overridden in tests."""
    try:
        timeout = float(settings.safe_memory_url_fetch_timeout_seconds)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    return httpx.Client(follow_redirects=False, timeout=timeout)


def _check_scheme(url: str):
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ImportRefError(400, "Only https:// URLs are allowed.")
    if not parsed.hostname:
        raise ImportRefError(400, "URL has no host.")
    return parsed


def _ip_is_disallowed(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host(host: str):
    """Resolve a host to IP addresses (or accept an IP literal directly)."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ImportRefError(400, "Could not resolve URL host.")
    ips = []
    for info in infos:
        addr = info[4][0]
        # Strip a possible IPv6 zone id (e.g. fe80::1%eth0).
        addr = addr.split("%", 1)[0]
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return ips


def _assert_public_host(host: str) -> None:
    ips = _resolve_host(host)
    if not ips:
        raise ImportRefError(400, "Could not resolve URL host.")
    for ip in ips:
        if _ip_is_disallowed(ip):
            raise ImportRefError(
                400, "URL resolves to a non-public address and was blocked."
            )


def fetch_pack_bytes(url: str, max_bytes: int) -> bytes:
    """Fetch bytes from ``url`` with HTTPS/SSRF/size guards.

    Follows redirects manually (re-validating each hop). Raises
    :class:`ImportRefError` on any guard failure.
    """
    data, _headers, _final = _stream_fetch(url, max_bytes)
    return data


def fetch_file_from_url(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Fetch a raw file from ``url`` and derive a sensible filename.

    Returns ``(bytes, filename)``. The filename (and especially its extension)
    is derived from Content-Disposition, then the final URL path, then the
    content type -- so the ingest pipeline can pick the correct reader.
    """
    data, headers, final_url = _stream_fetch(url, max_bytes)
    return data, _derive_filename(headers, final_url)


def fetch_file_from_url_typed(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    """Like :func:`fetch_file_from_url` but also returns the response Content-Type.

    Returns ``(bytes, filename, content_type)``. Used by buildPackFromUrl to
    detect HTML folder-browsing pages (SharePoint/OneDrive anonymous folders)
    before attempting to ingest them.
    """
    data, headers, final_url = _stream_fetch(url, max_bytes)
    content_type = (headers.get("content-type") or "").strip()
    return data, _derive_filename(headers, final_url), content_type


def _stream_fetch(url: str, max_bytes: int):
    """Shared streaming fetch with scheme/SSRF/redirect/size guards.

    Returns ``(bytes, response_headers, final_url)``.
    """
    current = url
    client = _build_client()
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = _check_scheme(current)
            _assert_public_host(parsed.hostname)

            try:
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise ImportRefError(400, "Redirect without a location.")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        raise ImportRefError(
                            400,
                            f"Fetch failed with status {resp.status_code}.",
                        )
                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ImportRefError(
                                413, "Remote file exceeds the size limit."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), resp.headers, str(resp.url)
            except httpx.RequestError as exc:
                raise ImportRefError(400, f"Could not fetch URL: {type(exc).__name__}.")
        raise ImportRefError(400, "Too many redirects while fetching.")
    finally:
        client.close()


def _parse_content_disposition(value: str) -> str | None:
    """Extract a filename from a Content-Disposition header value."""
    if not value:
        return None
    # RFC 5987: filename*=UTF-8''percent%20encoded.xlsx
    match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _safe_filename(name: str) -> str:
    """Keep only the final path component of a filename."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "download.bin"


def _derive_filename(headers, final_url: str) -> str:
    """Determine a filename (with a useful extension) for an ingested file."""
    name = _parse_content_disposition(headers.get("content-disposition", ""))
    if name:
        return _safe_filename(name)

    path_seg = urlparse(final_url).path.rsplit("/", 1)[-1]
    if path_seg and path_seg.lower().endswith(_INGEST_EXTS):
        return _safe_filename(path_seg)

    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _EXT_BY_CONTENT_TYPE.get(content_type)
    if ext:
        return f"download{ext}"

    if path_seg and "." in path_seg:
        return _safe_filename(path_seg)
    return "download.bin"
