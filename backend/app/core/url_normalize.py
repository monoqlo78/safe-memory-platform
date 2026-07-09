"""Normalize share links into direct-download URLs.

GPT/Claude/users paste a "share link" (SharePoint, OneDrive, Google Drive,
Dropbox, or any HTTPS URL). Most providers serve an HTML preview page from the
share link, not the raw file. This module rewrites known providers into their
direct-download form so the fetcher receives real file bytes. Unknown URLs are
returned unchanged (fetched as-is).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_SHAREPOINT_MARKER = re.compile(r"^:[a-z]:$")

# Actionable message (English + Japanese) shown when a SharePoint / OneDrive
# *folder* share link is used. Microsoft returns a folder-browsing web page for
# anonymous folder shares, so neither ZIP download nor file enumeration works.
SHAREPOINT_FOLDER_MESSAGE = (
    "SharePoint/OneDrive folder share links can't be read anonymously (Microsoft "
    "returns a folder-browsing web page, not the files). Retry with either: "
    "(1) a share link to an individual file inside the folder (e.g. an .xlsx), or "
    "(2) compress the folder into a single .zip file and share that .zip file's "
    "link (a shared .zip returns real bytes and is expanded into one pack). "
    "／ SharePoint・OneDriveのフォルダ共有リンクは匿名では読み取れません"
    "（Microsoftがファイルではなくフォルダ閲覧用のWebページを返すため）。"
    "次のいずれかで再実行してください：(1) フォルダ内の個別ファイル"
    "（例：FX取引.xlsx）の共有リンクを渡す、(2) フォルダを1つの .zip ファイルに"
    "圧縮して、その .zip ファイルの共有リンクを渡す（zip共有なら実バイトが返り、"
    "既存のフォルダZIP展開ロジックで各ファイルを1パックに統合します）。"
)


def _set_query_param(url: str, key: str, value: str) -> str:
    """Return ``url`` with ``key=value`` set (replacing any existing key)."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params[key] = [value]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _normalize_sharepoint(scheme: str, netloc: str, path: str) -> str | None:
    """Rewrite a SharePoint/OneDrive-for-Business share link to download.aspx.

    ``/:x:/g/personal/{user}/{shareId}`` -> ``/personal/{user}/_layouts/15/download.aspx?share={shareId}``
    ``/:x:/s/{site}/{shareId}``          -> ``/sites/{site}/_layouts/15/download.aspx?share={shareId}``
    (any of the :x: :w: :b: :p: :t: :f: document markers).
    """
    parts = [p for p in path.split("/") if p]
    if not parts or not _SHAREPOINT_MARKER.match(parts[0]):
        return None
    rest = parts[1:]
    # Personal OneDrive-for-Business grant links.
    if len(rest) >= 4 and rest[0] == "g" and rest[1] == "personal":
        user, share_id = rest[2], rest[3]
        new_path = f"/personal/{user}/_layouts/15/download.aspx"
        return f"{scheme}://{netloc}{new_path}?share={share_id}"
    # Site (team) share links.
    if len(rest) >= 3 and rest[0] == "s":
        site, share_id = rest[1], rest[2]
        new_path = f"/sites/{site}/_layouts/15/download.aspx"
        return f"{scheme}://{netloc}{new_path}?share={share_id}"
    return None


def _normalize_gdrive(path: str, query: str) -> str | None:
    """Rewrite a Google Drive link to its uc?export=download form."""
    match = re.search(r"/file/d/([^/]+)", path)
    if match:
        file_id = match.group(1)
    else:
        file_id = (parse_qs(query).get("id") or [None])[0]
    if not file_id:
        return None
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def folder_fetch_error(url: str) -> str | None:
    """Return an error message if ``url`` is a folder that can't be fetched as a ZIP.

    Some providers (notably Google Drive) do not expose an anonymous direct-
    download ZIP for a *folder* share link, so we fail early with a clear,
    actionable message instead of downloading an HTML page. SharePoint /
    OneDrive / Dropbox folder links DO return a ZIP via their normalized
    download URL, so they are not flagged here.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "drive.google.com" and "/drive/folders/" in parsed.path:
        return (
            "Google Drive folders can't be fetched directly (Drive has no "
            "anonymous folder ZIP download). Zip the folder and share the .zip, "
            "use individual file links, or upload via the /upload page."
        )
    # SharePoint / OneDrive-for-Business folder share links use the ":f:" marker.
    # Microsoft serves an HTML folder page (not a ZIP) for anonymous folder
    # shares, so fail early with an actionable message.
    if host.endswith("sharepoint.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == ":f:":
            return SHAREPOINT_FOLDER_MESSAGE
    return None


def html_response_error(content_type: str, filename: str, body: bytes) -> str | None:
    """Return an error message when a fetch returned an HTML page, not a file.

    SharePoint / OneDrive anonymous folder shares (and some expired/permission
    pages) return ``text/html`` (e.g. ``onedrive.aspx``) instead of real file
    bytes. Detect that here so we surface the actionable folder message instead
    of a confusing "Unsupported file type". A genuine file download (xlsx, csv,
    a real ``.zip`` bundle, etc.) is never flagged.
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    name = (filename or "").lower()
    # Never flag a recognized data file, even if the server mislabels it.
    if name.endswith(
        (
            ".xlsx", ".xls", ".csv", ".tsv", ".txt", ".md", ".json", ".zip",
            ".docx", ".pptx", ".pdf",
            ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp",
        )
    ):
        return None
    head = body[:512].lstrip().lower() if body else b""
    looks_html = ctype in ("text/html", "application/xhtml+xml") or head.startswith(
        (b"<!doctype html", b"<html")
    )
    if looks_html:
        return SHAREPOINT_FOLDER_MESSAGE
    return None


def to_direct_download_url(url: str) -> str:
    """Return a direct-download URL for known providers, else ``url`` unchanged."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host.endswith("sharepoint.com"):
        return _normalize_sharepoint(parsed.scheme, parsed.netloc, parsed.path) or url

    if host == "drive.google.com":
        return _normalize_gdrive(parsed.path, parsed.query) or url

    if host.endswith("dropbox.com"):
        # dl=0 (preview) -> dl=1 (direct download).
        return _set_query_param(url, "dl", "1")

    if host == "1drv.ms" or host.endswith("onedrive.live.com"):
        # Personal OneDrive / short links: harmless download hint; 1drv.ms is a
        # redirector so the fetcher follows the 302 to the real file.
        return _set_query_param(url, "download", "1")

    return url
