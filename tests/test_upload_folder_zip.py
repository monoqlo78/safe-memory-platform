"""Folder / multi-file staged upload via the /upload page flow.

The browser page zips a folder (or multiple files) into one ``bundle.zip`` and
runs it through the existing staged-upload pipeline (init -> PUT ->
build-from-upload-ref). A folder ZIP is detected by content, so these tests
drive the same server path a real bundle.zip takes: one merged pack, per-file
provenance, unsupported-file tracking, raw-file cleanup, plus the /upload page
markup that powers folder drag-drop.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import settings
from app.core import pack_io, storage
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "upload-folder-key"


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


def _folder_zip_bytes(files):
    """Build a plain ZIP archive from {name: bytes} (a browser bundle.zip)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _stage_and_build(client, auth, filename, data, pack_id, retention, **extra):
    """Run init -> PUT -> build-from-upload-ref and return the finished job."""
    init = client.post(
        "/api/uploads/init",
        json={"filename": filename, "content_type": "application/zip", "size": len(data)},
        headers=auth,
    ).json()
    put = client.put(
        f"/api/uploads/{init['upload_id']}/content",
        params={"token": init["upload_token"]},
        content=data,
    )
    assert put.status_code == 200, put.text
    ref = client.post(
        "/api/packs/build-from-upload-ref",
        json={
            "upload_id": init["upload_id"],
            "agent_id": "folder-agent",
            "pack_id": pack_id,
            "title": "Folder Bundle",
            "source_language": "ja",
            "retention_mode": retention,
            **extra,
        },
        headers=auth,
    )
    assert ref.status_code == 200, ref.text
    job_id = ref.json()["job_id"]
    return init, client.get(f"/api/jobs/{job_id}", headers=auth).json()


# ---------------------------------------------------------------------------
# bundle.zip -> one merged pack
# ---------------------------------------------------------------------------
def test_bundle_zip_merges_into_one_pack(client, auth):
    bundle = _folder_zip_bytes(
        {
            "sales.csv": b"row one\nrow two\n",
            "notes/readme.txt": b"a single note",
            "book.xlsx": _xlsx_bytes(["請求書 500円", "消費税 10%"]),
        }
    )
    _init, job = _stage_and_build(
        client, auth, "bundle.zip", bundle, "bundle-pack", "process_and_return"
    )
    assert job["status"] == "COMPLETED", job
    assert job["input_type"] == "folder"
    # 2 csv rows + 1 txt chunk + 1 xlsx row (first row is the header), merged.
    assert job["entry_count"] == 4


def test_bundle_zip_provenance_has_source_filenames(client, auth):
    bundle = _folder_zip_bytes(
        {"alpha.csv": b"one\ntwo\n", "docs/beta.txt": b"three"}
    )
    _init, job = _stage_and_build(
        client, auth, "bundle.zip", bundle, "prov-pack", "server_vault"
    )
    assert job["status"] == "COMPLETED", job

    catalog = client.get("/api/agents/folder-agent/catalog", headers=auth).json()
    match = [p for p in catalog["packs"] if p["pack_id"] == "prov-pack"]
    assert match, catalog
    pack = pack_io.load_pack(match[0]["path"])
    sources = {e.provenance.source for e in pack.entries}
    assert sources == {"alpha.csv", "beta.txt"}


def test_bundle_zip_tracks_unsupported_files(client, auth):
    bundle = _folder_zip_bytes(
        {"good.csv": b"row\n", "report.pdf": b"%PDF-1.4", "legacy.docx": b"PK-docx"}
    )
    _init, job = _stage_and_build(
        client, auth, "bundle.zip", bundle, "unsup-pack", "process_and_return"
    )
    assert job["status"] == "COMPLETED", job
    skipped = {u["filename"] for u in job["unsupported_files"]}
    assert "report.pdf" in skipped
    assert "legacy.docx" in skipped


def test_bundle_zip_raw_deleted_on_process_and_return(client, auth):
    bundle = _folder_zip_bytes({"a.csv": b"row\n", "b.txt": b"note"})
    init, job = _stage_and_build(
        client, auth, "bundle.zip", bundle, "clean-pack", "process_and_return"
    )
    assert job["status"] == "COMPLETED", job
    assert job["raw_upload_deleted"] is True
    assert job["working_files_deleted"] is True
    assert storage.get_storage().exists(init["upload_id"]) is False


def test_bundle_zip_no_supported_files_fails(client, auth):
    bundle = _folder_zip_bytes({"a.pdf": b"%PDF", "b.png": b"\x89PNG"})
    _init, job = _stage_and_build(
        client, auth, "bundle.zip", bundle, "empty-pack", "process_and_return"
    )
    assert job["status"] == "FAILED"
    assert job["warnings"]


# ---------------------------------------------------------------------------
# Single-file regression (page uploads one file directly, no zip)
# ---------------------------------------------------------------------------
def test_single_file_still_builds_as_file(client, auth):
    _init, job = _stage_and_build(
        client, auth, "notes.txt", b"The invoice total is 500 USD.",
        "single-pack", "process_and_return",
    )
    assert job["status"] == "COMPLETED", job
    assert job["input_type"] == "file"
    assert job["entry_count"] >= 1


# ---------------------------------------------------------------------------
# /upload page markup for folder drag-drop
# ---------------------------------------------------------------------------
def test_upload_page_has_folder_and_jszip(client):
    resp = client.get("/upload")
    assert resp.status_code == 200
    html = resp.text
    assert "webkitdirectory" in html          # folder picker
    assert "jszip" in html.lower()             # client-side zipping
    assert "webkitGetAsEntry" in html          # recursive folder drag-drop
    assert "Copy share link" in html           # OSS signed-URL sharing
    assert "importPackByRef" in html           # reuse hint for GPT
