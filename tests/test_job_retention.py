"""Job & session retention: upload retention modes, jobs API, and cleanup."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import jobs_store, pack_io
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "retention-key-xyz"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return TEST_KEY


def _upload(client, pack_id, retention_mode, debug_keep_upload=False, headers=None):
    files = {"file": ("notes.txt", b"The invoice total is 500 USD.", "text/plain")}
    data = {
        "agent_id": "tax-agent",
        "pack_id": pack_id,
        "title": "Retention Pack",
        "retention_mode": retention_mode,
        "debug_keep_upload": "true" if debug_keep_upload else "false",
    }
    return client.post(
        "/api/packs/build-from-upload", data=data, files=files, headers=headers or {}
    )


# ---------------------------------------------------------------------------
# Upload retention modes
# ---------------------------------------------------------------------------
def test_process_and_return_creates_job_and_deletes_raw(client):
    resp = _upload(client, "pr-pack", "process_and_return")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["job_id"]
    assert body["retention_mode"] == "process_and_return"
    assert body["status"] == "COMPLETED"
    assert body["expires_at"] is not None
    assert body["download_url"] == f"/api/packs/dl/{jobs_store.load_job(body['job_id']).download_token}"
    assert body["entry_count"] >= 1

    job = jobs_store.load_job(body["job_id"])
    assert job.raw_upload_deleted is True
    assert job.working_files_deleted is True
    assert job.pack_persisted is False
    # Working dir removed from disk.
    assert job.working_dir is None


def test_session_mode_is_temporary(client):
    resp = _upload(client, "sess-pack", "session")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_mode"] == "session"
    assert body["expires_at"] is not None
    assert body["download_url"] is not None
    job = jobs_store.load_job(body["job_id"])
    assert job.pack_persisted is False


def test_debug_keep_upload_retains_raw(client):
    resp = _upload(client, "keep-pack", "process_and_return", debug_keep_upload=True)
    assert resp.status_code == 200, resp.text
    job = jobs_store.load_job(resp.json()["job_id"])
    assert job.raw_upload_deleted is False
    assert job.working_dir is not None
    # The working dir still exists on disk.
    assert pack_io.ensure_safe_path(job.working_dir).exists()


def test_server_vault_persists_and_appears_in_catalog(client):
    resp = _upload(client, "vault-pack", "server_vault")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_mode"] == "server_vault"
    assert body["expires_at"] is None
    # server_vault now always exposes a stable, signature-free download URL
    # (the pack is persisted locally, streamed via the token route).
    assert body["download_url"] and "/api/packs/dl/" in body["download_url"]

    job = jobs_store.load_job(body["job_id"])
    assert job.pack_persisted is True
    assert job.raw_upload_deleted is True  # raw upload still deleted

    catalog = client.get("/api/agents/tax-agent/catalog").json()
    pack_ids = {p["pack_id"] for p in catalog["packs"]}
    assert "vault-pack" in pack_ids


def test_temp_modes_do_not_appear_in_catalog(client):
    _upload(client, "temp-not-in-catalog", "process_and_return")
    catalog = client.get("/api/agents/tax-agent/catalog").json()
    pack_ids = {p["pack_id"] for p in catalog["packs"]}
    assert "temp-not-in-catalog" not in pack_ids


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def test_download_returns_pack(client):
    body = _upload(client, "dl-pack", "process_and_return").json()
    resp = client.get(body["download_url"])
    assert resp.status_code == 200
    pack = resp.json()
    assert pack["manifest"]["pack_id"] == "dl-pack"


# ---------------------------------------------------------------------------
# Jobs API + auth
# ---------------------------------------------------------------------------
def test_get_and_delete_job(client):
    body = _upload(client, "gd-pack", "process_and_return").json()
    job_id = body["job_id"]

    got = client.get(f"/api/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["job_id"] == job_id
    # Server paths hidden without debug.
    assert got.json()["pack_path"] is None
    assert client.get(f"/api/jobs/{job_id}?debug=true").json()["pack_path"] is not None

    deleted = client.request("DELETE", f"/api/jobs/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "DELETED"
    assert deleted.json()["pack_persisted"] is False

    # Temp pack file is gone.
    job = jobs_store.load_job(job_id)
    assert not pack_io.ensure_safe_path(job.pack_path).exists()


def test_jobs_endpoints_require_auth(client, with_api_key):
    body = _upload(
        client,
        "auth-pack",
        "process_and_return",
        headers={API_KEY_HEADER: TEST_KEY},
    ).json()
    job_id = body["job_id"]

    # No key -> 401
    assert client.get(f"/api/jobs/{job_id}").status_code == 401
    assert client.request("DELETE", f"/api/jobs/{job_id}").status_code == 401
    assert client.post("/api/jobs/cleanup").status_code == 401

    # Correct key -> ok
    h = {API_KEY_HEADER: TEST_KEY}
    assert client.get(f"/api/jobs/{job_id}", headers=h).status_code == 200


def test_delete_server_vault_keeps_pack(client):
    body = _upload(client, "vault-keep", "server_vault").json()
    job_id = body["job_id"]

    deleted = client.request("DELETE", f"/api/jobs/{job_id}")
    assert deleted.status_code == 200
    # Vault pack is preserved even though the job is cleaned.
    assert deleted.json()["pack_persisted"] is True
    job = jobs_store.load_job(job_id)
    assert pack_io.ensure_safe_path(job.pack_path).exists()
    # Still in catalog.
    catalog = client.get("/api/agents/tax-agent/catalog").json()
    assert "vault-keep" in {p["pack_id"] for p in catalog["packs"]}


# ---------------------------------------------------------------------------
# Cleanup of expired temp jobs
# ---------------------------------------------------------------------------
def test_cleanup_removes_expired_temp_and_keeps_vault(client):
    temp_body = _upload(client, "exp-temp", "process_and_return").json()
    vault_body = _upload(client, "exp-vault", "server_vault").json()

    # Back-date the temp job's expiry into the past.
    temp_job = jobs_store.load_job(temp_body["job_id"])
    temp_job.expires_at = "2000-01-01T00:00:00+00:00"
    jobs_store.save_job(temp_job)
    temp_pack_path = temp_job.pack_path
    assert pack_io.ensure_safe_path(temp_pack_path).exists()

    summary = client.post("/api/jobs/cleanup").json()
    assert summary["jobs_cleaned"] >= 1
    assert summary["packs_deleted"] >= 1
    assert temp_body["job_id"] in summary["job_ids"]

    # Temp pack deleted, job marked expired.
    assert not pack_io.ensure_safe_path(temp_pack_path).exists()
    assert jobs_store.load_job(temp_body["job_id"]).status == "EXPIRED"

    # Vault pack untouched.
    vault_job = jobs_store.load_job(vault_body["job_id"])
    assert pack_io.ensure_safe_path(vault_job.pack_path).exists()
    assert vault_body["job_id"] not in summary["job_ids"]


def test_cleanup_job_function_is_path_safe(safe_root):
    """cleanup_job on an unknown id returns None without raising."""
    assert jobs_store.cleanup_job("does-not-exist") is None
