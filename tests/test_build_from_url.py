"""buildPackFromUrl: fetch a raw file from a share link and build a pack.

URL-normalization is unit-tested directly. The fetch path is mocked with
httpx.MockTransport (never hits the real network); a public IP literal host is
used so the SSRF guard passes without DNS.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import packs as packs_api
from app.config import settings
from app.core import pack_import, url_normalize
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "build-from-url-key"
PUBLIC_URL = "https://8.8.8.8/data.xlsx"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


def _xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append([r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_client(body, status=200, headers=None):
    hdrs = {"content-type": "application/octet-stream"}
    if headers:
        hdrs.update(headers)

    def handler(request):
        return httpx.Response(status, content=body, headers=hdrs)

    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10
    )


def _use_mock(monkeypatch, body, **kw):
    monkeypatch.setattr(pack_import, "_build_client", lambda: _mock_client(body, **kw))


def _build_from_url(client, headers, url, **extra):
    payload = {
        "url": url,
        "agent_id": "url-agent",
        "pack_id": "url-pack",
        "title": "From URL",
        "source_language": "ja",
        **extra,
    }
    return client.post("/api/packs/build-from-url", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# url_normalize unit tests
# ---------------------------------------------------------------------------
def test_sharepoint_x_marker_to_download_aspx():
    url = (
        "https://contoso-my.sharepoint.com/:x:/g/personal/jdoe_contoso_com/"
        "IQBelQEd?e=abcd"
    )
    out = url_normalize.to_direct_download_url(url)
    assert out == (
        "https://contoso-my.sharepoint.com/personal/jdoe_contoso_com/"
        "_layouts/15/download.aspx?share=IQBelQEd"
    )


def test_sharepoint_w_and_b_markers():
    for marker in (":w:", ":b:"):
        url = (
            f"https://contoso-my.sharepoint.com/{marker}/g/personal/u_contoso_com/"
            "SHARE123?e=x"
        )
        out = url_normalize.to_direct_download_url(url)
        assert out.endswith("/_layouts/15/download.aspx?share=SHARE123")
        assert "/personal/u_contoso_com/" in out


def test_sharepoint_site_form():
    url = "https://contoso.sharepoint.com/:x:/s/finance/SHAREID?e=1"
    out = url_normalize.to_direct_download_url(url)
    assert out == (
        "https://contoso.sharepoint.com/sites/finance/"
        "_layouts/15/download.aspx?share=SHAREID"
    )


def test_gdrive_file_d():
    out = url_normalize.to_direct_download_url(
        "https://drive.google.com/file/d/ABC123/view?usp=sharing"
    )
    assert out == "https://drive.google.com/uc?export=download&id=ABC123"


def test_gdrive_open_id():
    out = url_normalize.to_direct_download_url(
        "https://drive.google.com/open?id=XYZ789"
    )
    assert out == "https://drive.google.com/uc?export=download&id=XYZ789"


def test_gdrive_uc_id():
    out = url_normalize.to_direct_download_url(
        "https://drive.google.com/uc?id=QQQ"
    )
    assert out == "https://drive.google.com/uc?export=download&id=QQQ"


def test_dropbox_dl0_to_dl1():
    out = url_normalize.to_direct_download_url(
        "https://www.dropbox.com/s/abc/file.xlsx?dl=0"
    )
    assert "dl=1" in out
    assert "dl=0" not in out


def test_unknown_url_passthrough():
    url = "https://example.com/path/file.xlsx?token=1"
    assert url_normalize.to_direct_download_url(url) == url


# ---------------------------------------------------------------------------
# Flow + guards
# ---------------------------------------------------------------------------
def test_build_from_url_xlsx_flow(client, auth, monkeypatch):
    data = _xlsx_bytes(["請求書の合計は500円です", "消費税は10%です"])
    _use_mock(
        monkeypatch,
        data,
        headers={
            "content-type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        },
    )
    resp = _build_from_url(client, auth, PUBLIC_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Bounded-synchronous: a small build finishes inside the wait window, so the
    # single call returns the terminal job (with download_url) -- no polling.
    assert body["status"] == "COMPLETED"
    assert body["entry_count"] > 0
    assert body["download_url"] and "/api/packs/dl/" in body["download_url"]

    # Polling remains backward compatible.
    job = client.get(f"/api/jobs/{body['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"


def test_build_from_url_csv_flow(client, auth, monkeypatch):
    csv_bytes = "売上高が増加した\n税金を期日通りに納付した\n".encode("utf-8")
    _use_mock(monkeypatch, csv_bytes, headers={"content-type": "text/csv"})
    resp = _build_from_url(
        client, auth, "https://8.8.8.8/data.csv", pack_id="csv-pack"
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] > 0


def test_content_disposition_drives_extension(client, auth, monkeypatch):
    # URL path ends in .aspx (SharePoint), but Content-Disposition carries the
    # real .xlsx name so the ingest pipeline reads it as a spreadsheet.
    data = _xlsx_bytes(["ヘッダー", "請求書の合計は500円です", "消費税は10%です"])
    _use_mock(
        monkeypatch,
        data,
        headers={"content-disposition": 'attachment; filename="book.xlsx"'},
    )
    url = (
        "https://contoso.sharepoint.com/personal/u/_layouts/15/"
        "download.aspx?share=ABC"
    )
    # host must be public: patch normalization target to a public IP literal.
    monkeypatch.setattr(
        url_normalize, "to_direct_download_url", lambda u: "https://8.8.8.8/download.aspx"
    )
    resp = _build_from_url(client, auth, url, pack_id="cd-pack")
    assert resp.status_code == 200, resp.text
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] > 0


def test_build_from_url_rejects_http(client, auth):
    resp = _build_from_url(client, auth, "http://8.8.8.8/data.xlsx")
    assert resp.status_code == 400


def test_build_from_url_rejects_ssrf_private_ip(client, auth):
    resp = _build_from_url(client, auth, "https://127.0.0.1/data.xlsx")
    assert resp.status_code == 400


def test_build_from_url_size_limit(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_max_upload_mb", 1, raising=False)
    big = b"x" * (2 * 1024 * 1024)
    _use_mock(monkeypatch, big, headers={"content-type": "text/plain"})
    resp = _build_from_url(client, auth, "https://8.8.8.8/big.txt")
    assert resp.status_code == 413


def test_build_from_url_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    resp = _build_from_url(client, {}, PUBLIC_URL)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SharePoint/OneDrive folder shares can't be fetched anonymously
# ---------------------------------------------------------------------------
def test_sharepoint_folder_marker_fails_with_message(client, auth):
    # A ":f:" (folder) share link is rejected before any fetch, with an
    # actionable message. No network mock needed.
    url = (
        "https://sdesignertokyo-my.sharepoint.com/:f:/g/personal/"
        "msogabe_sdesigner_tokyo/IgD1zv97?e=F8blR9"
    )
    resp = _build_from_url(client, auth, url, pack_id="sp-folder")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    job = client.get(f"/api/jobs/{body['job_id']}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("folder share links" in w.lower() for w in job["warnings"])
    assert any(".zip" in w.lower() for w in job["warnings"])


def test_html_response_fails_with_folder_message(client, auth, monkeypatch):
    # download.aspx?share= for a folder returns an HTML page (onedrive.aspx),
    # not real bytes -> fail with the actionable folder message rather than a
    # confusing "Unsupported file type".
    html = b"<!DOCTYPE html><html><head><title>onedrive.aspx</title></head></html>"
    _use_mock(monkeypatch, html, headers={"content-type": "text/html; charset=utf-8"})
    resp = _build_from_url(
        client, auth, "https://8.8.8.8/_layouts/15/download.aspx", pack_id="html-pack"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    job = client.get(f"/api/jobs/{body['job_id']}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("anonymously" in w.lower() for w in job["warnings"])


def test_single_xlsx_still_succeeds_despite_html_guard(client, auth, monkeypatch):
    # A genuine .xlsx download (real bytes) must not be blocked by the HTML guard.
    data = _xlsx_bytes(["ヘッダー", "請求書の合計は500円です", "消費税は10%です"])
    _use_mock(
        monkeypatch,
        data,
        headers={
            "content-type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "content-disposition": 'attachment; filename="book.xlsx"',
        },
    )
    resp = _build_from_url(client, auth, PUBLIC_URL, pack_id="ok-xlsx")
    assert resp.status_code == 200, resp.text
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] > 0


# ---------------------------------------------------------------------------
# OpenAPI visibility
# ---------------------------------------------------------------------------
def test_build_pack_from_url_visible_in_openapi(client):
    schema = client.get("/openapi.json").json()
    op_ids = {
        op.get("operationId")
        for path in schema["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }
    assert "buildPackFromUrl" in op_ids
    # File-transfer ops stay hidden.
    assert "initUpload" not in op_ids
    assert "uploadContent" not in op_ids
    assert "buildMemoryPackFromUpload" not in op_ids
    assert "buildMemoryPackFromUploadRef" not in op_ids


# ---------------------------------------------------------------------------
# Folder ZIP support
# ---------------------------------------------------------------------------
def _folder_zip_bytes(files):
    """Build a plain ZIP archive from {name: bytes} (a shared folder)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_folder_zip_detected_single_xlsx_not():
    # A folder ZIP with multiple plain files is a folder.
    folder = _folder_zip_bytes(
        {"a.csv": b"row1\nrow2\n", "b.txt": b"hello world"}
    )
    assert packs_api._is_folder_zip(folder) is True
    # A single .xlsx is itself a ZIP but carries [Content_Types].xml -> NOT a folder.
    assert packs_api._is_folder_zip(_xlsx_bytes(["h", "r1", "r2"])) is False


def test_folder_zip_merges_files_into_one_pack(client, auth, monkeypatch):
    files = {
        "sales.csv": "売上高が増加した\n税金を納付した\n".encode("utf-8"),
        "notes.txt": "請求書の合計は500円です".encode("utf-8"),
        "unsupported.pdf": b"%PDF-1.4 ignore me",
        "__MACOSX/._sales.csv": b"junk",
        ".hidden.csv": "隠しファイル".encode("utf-8"),
    }
    zip_bytes = _folder_zip_bytes(files)
    _use_mock(monkeypatch, zip_bytes, headers={"content-type": "application/zip"})
    resp = _build_from_url(
        client, auth, "https://8.8.8.8/folder.zip", pack_id="folder-pack"
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    # 2 csv rows + 1 txt chunk = 3 entries, merged into one pack.
    assert job["entry_count"] == 3


def test_folder_zip_provenance_has_source_filenames(client, auth, monkeypatch):
    files = {
        "alpha.csv": b"one\ntwo\n",
        "beta.txt": b"three",
    }
    zip_bytes = _folder_zip_bytes(files)
    _use_mock(monkeypatch, zip_bytes, headers={"content-type": "application/zip"})
    resp = _build_from_url(
        client,
        auth,
        "https://8.8.8.8/folder.zip",
        pack_id="prov-pack",
        retention_mode="server_vault",
    )
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"

    # Export the vault pack and confirm provenance sources are the member names.
    from app.core import pack_io

    catalog = client.get("/api/agents/url-agent/catalog", headers=auth).json()
    match = [p for p in catalog["packs"] if p["pack_id"] == "prov-pack"]
    assert match, catalog
    pack = pack_io.load_pack(match[0]["path"])
    sources = {e.provenance.source for e in pack.entries}
    assert sources == {"alpha.csv", "beta.txt"}


def test_folder_zip_rejects_zip_slip(client, auth, monkeypatch):
    # An entry that escapes the sandbox must abort the whole import.
    zip_bytes = _folder_zip_bytes({"../evil.csv": b"pwned\n", "ok.csv": b"fine\n"})
    _use_mock(monkeypatch, zip_bytes, headers={"content-type": "application/zip"})
    resp = _build_from_url(client, auth, "https://8.8.8.8/folder.zip", pack_id="slip")
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("Unsafe path" in w for w in job["warnings"])


def test_folder_zip_no_supported_files_fails(client, auth, monkeypatch):
    zip_bytes = _folder_zip_bytes({"a.pdf": b"%PDF", "b.png": b"\x89PNG"})
    _use_mock(monkeypatch, zip_bytes, headers={"content-type": "application/zip"})
    resp = _build_from_url(client, auth, "https://8.8.8.8/folder.zip", pack_id="empty")
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("No supported files" in w for w in job["warnings"])


def test_google_drive_folder_fails_clearly(client, auth):
    resp = _build_from_url(
        client,
        auth,
        "https://drive.google.com/drive/folders/ABC123?usp=sharing",
        pack_id="gd-folder",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("Google Drive folders" in w for w in job["warnings"])


def test_sharepoint_folder_marker_normalizes():
    # The :f: (folder) marker converts to download.aspx?share= just like files.
    out = url_normalize.to_direct_download_url(
        "https://contoso-my.sharepoint.com/:f:/g/personal/u_contoso_com/FID?e=1"
    )
    assert out == (
        "https://contoso-my.sharepoint.com/personal/u_contoso_com/"
        "_layouts/15/download.aspx?share=FID"
    )

