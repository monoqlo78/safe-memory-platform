"""Large-file / LLM-safe staged upload flow.

Covers the init -> PUT bytes (token) -> build-from-upload-ref (async) -> poll
job -> download pipeline, plus token/size/auth guards, retention parity with the
multipart endpoint, staging cleanup, and the open /upload page.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import settings
from app.core import storage
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "upload-ref-key-123"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


def _init(client, headers, filename="notes.txt", size=None):
    resp = client.post(
        "/api/uploads/init",
        json={"filename": filename, "content_type": "text/plain", "size": size},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _put(client, upload_id, token, data, **kw):
    return client.put(
        f"/api/uploads/{upload_id}/content",
        params={"token": token},
        content=data,
        **kw,
    )


def _build_ref(client, headers, upload_id, pack_id, retention_mode, **extra):
    payload = {
        "upload_id": upload_id,
        "agent_id": "tax-agent",
        "pack_id": pack_id,
        "title": "Staged Pack",
        "source_language": "ja",
        "retention_mode": retention_mode,
        **extra,
    }
    return client.post("/api/packs/build-from-upload-ref", json=payload, headers=headers)


def _xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append([r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_full_flow_init_put_build_poll_download(client, auth):
    init = _init(client, auth, filename="knowledge.txt")
    assert init["upload_url"].endswith(f"/api/uploads/{init['upload_id']}/content")
    assert init["method"] == "PUT"

    put = _put(client, init["upload_id"], init["upload_token"], b"The invoice total is 500 USD.")
    assert put.status_code == 200, put.text
    assert put.json()["received"] is True
    assert put.json()["size"] > 0

    ref = _build_ref(client, auth, init["upload_id"], "staged-pack", "process_and_return")
    assert ref.status_code == 200, ref.text
    assert ref.json()["status"] == "PROCESSING"
    job_id = ref.json()["job_id"]

    # BackgroundTasks complete within the TestClient request lifecycle.
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] >= 1
    # download_url is a stable, signature-free token URL (no-auth, GPT-safe).
    assert "/api/packs/dl/" in job["download_url"]

    dl = client.get(job["download_url"], headers=auth)
    assert dl.status_code == 200
    assert dl.json()["manifest"]["pack_id"] == "staged-pack"


def test_flow_with_xlsx(client, auth):
    init = _init(client, auth, filename="book.xlsx")
    data = _xlsx_bytes(["請求書の合計は500円です", "消費税は10%です"])
    put = _put(client, init["upload_id"], init["upload_token"], data)
    assert put.status_code == 200, put.text

    ref = _build_ref(client, auth, init["upload_id"], "xlsx-pack", "process_and_return")
    job_id = ref.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] >= 1


# ---------------------------------------------------------------------------
# Token & size guards
# ---------------------------------------------------------------------------
def test_put_wrong_token_rejected(client, auth):
    init = _init(client, auth)
    resp = _put(client, init["upload_id"], "not-the-token", b"data")
    assert resp.status_code == 403


def test_put_missing_token_rejected(client, auth):
    init = _init(client, auth)
    resp = client.put(f"/api/uploads/{init['upload_id']}/content", content=b"data")
    assert resp.status_code == 403


def test_put_unknown_upload_id(client, auth):
    resp = _put(client, "nope", "tok", b"data")
    assert resp.status_code == 404


def test_size_limit_returns_413(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_max_upload_mb", 1, raising=False)
    init = _init(client, auth)
    big = b"x" * (2 * 1024 * 1024)
    resp = _put(client, init["upload_id"], init["upload_token"], big)
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Retention parity with the multipart endpoint
# ---------------------------------------------------------------------------
def test_retention_process_and_return_not_in_catalog(client, auth):
    init = _init(client, auth)
    _put(client, init["upload_id"], init["upload_token"], b"Some invoice note.")
    ref = _build_ref(client, auth, init["upload_id"], "temp-ref-pack", "process_and_return")
    job = client.get(f"/api/jobs/{ref.json()['job_id']}", headers=auth).json()

    assert job["download_url"] is not None
    assert job["expires_at"] is not None
    assert job["raw_upload_deleted"] is True
    assert job["working_files_deleted"] is True

    catalog = client.get("/api/agents/tax-agent/catalog", headers=auth).json()
    assert "temp-ref-pack" not in {p["pack_id"] for p in catalog["packs"]}


def test_retention_server_vault_persists_and_in_catalog(client, auth):
    init = _init(client, auth)
    _put(client, init["upload_id"], init["upload_token"], b"Vault invoice note.")
    ref = _build_ref(client, auth, init["upload_id"], "vault-ref-pack", "server_vault")
    job = client.get(f"/api/jobs/{ref.json()['job_id']}", headers=auth).json()

    assert job["pack_persisted"] is True
    assert job["expires_at"] is None
    # server_vault now always exposes a stable, signature-free download URL.
    assert job["download_url"] and "/api/packs/dl/" in job["download_url"]

    catalog = client.get("/api/agents/tax-agent/catalog", headers=auth).json()
    assert "vault-ref-pack" in {p["pack_id"] for p in catalog["packs"]}


# ---------------------------------------------------------------------------
# Staging cleanup after processing
# ---------------------------------------------------------------------------
def test_staging_deleted_after_processing(client, auth):
    init = _init(client, auth)
    _put(client, init["upload_id"], init["upload_token"], b"note")
    _build_ref(client, auth, init["upload_id"], "cleanup-pack", "process_and_return")
    assert storage.get_storage().exists(init["upload_id"]) is False


def test_staging_kept_when_debug(client, auth):
    init = _init(client, auth)
    _put(client, init["upload_id"], init["upload_token"], b"note")
    _build_ref(
        client, auth, init["upload_id"], "keep-ref-pack",
        "process_and_return", debug_keep_upload=True,
    )
    assert storage.get_storage().exists(init["upload_id"]) is True
    rec = storage.load_upload_record(init["upload_id"])
    assert rec.status.value == "consumed"


# ---------------------------------------------------------------------------
# Auth boundaries
# ---------------------------------------------------------------------------
def test_init_requires_api_key(client, auth):
    resp = client.post("/api/uploads/init", json={"filename": "a.txt"})
    assert resp.status_code == 401


def test_build_ref_requires_api_key(client, auth):
    resp = client.post(
        "/api/packs/build-from-upload-ref",
        json={"upload_id": "x", "agent_id": "a", "pack_id": "p", "title": "t"},
    )
    assert resp.status_code == 401


def test_put_content_does_not_need_api_key(client, auth):
    init = _init(client, auth)
    # No API key header on the PUT, only the token.
    resp = _put(client, init["upload_id"], init["upload_token"], b"note")
    assert resp.status_code == 200


def test_upload_page_is_open(client, auth):
    resp = client.get("/upload")
    assert resp.status_code == 200
    assert "Safe Memory" in resp.text
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Expired staging cleanup
# ---------------------------------------------------------------------------
def test_expired_staging_cleanup(client, auth):
    init = _init(client, auth)
    _put(client, init["upload_id"], init["upload_token"], b"note")

    rec = storage.load_upload_record(init["upload_id"])
    rec.expires_at = "2000-01-01T00:00:00+00:00"
    storage.save_upload_record(rec)

    resp = client.post("/api/jobs/cleanup", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["uploads_deleted"] >= 1
    assert storage.get_storage().exists(init["upload_id"]) is False
