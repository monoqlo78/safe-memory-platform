"""Alibaba OSS handoff integration tests.

The oss2 SDK is never imported: ``oss_storage._get_bucket`` is monkeypatched to a
fake bucket, so these tests never touch the network. Covers process_and_return /
server_vault OSS upload + signed URL, the OSS-disabled local fallback, presign
endpoints, unsupported-file / folder-limit handling, .json ingest, and a
caplog assertion that neither AccessKeys nor signed-URL query strings leak.
"""

from __future__ import annotations

import io
import logging
import zipfile
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import packs as packs_api
from app.config import settings
from app.core import oss_storage, pack_import
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "oss-test-key"
PUBLIC_URL = "https://8.8.8.8/data.csv"

SIGNED_URL = (
    "https://bkt.oss-ap-southeast-1.aliyuncs.com/exports/job/pack.smp.json"
    "?OSSAccessKeyId=AKIDSECRET123&Signature=SIGSECRET456&Expires=9999999999"
)


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


@pytest.fixture
def enable_oss(monkeypatch):
    """Turn OSS on with dummy config and a fake bucket (no oss2 import)."""
    monkeypatch.setattr(settings, "oss_enabled", True, raising=False)
    monkeypatch.setattr(settings, "oss_bucket", "bkt", raising=False)
    monkeypatch.setattr(
        settings, "oss_endpoint", "https://oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(settings, "oss_access_key_id", "AKIDSECRET123", raising=False)
    monkeypatch.setattr(
        settings, "oss_access_key_secret", "SIGSECRET456", raising=False
    )
    bucket = MagicMock()
    bucket.put_object_from_file.return_value = MagicMock(etag="etag123")
    bucket.put_object.return_value = MagicMock(etag="etag123")
    bucket.sign_url.return_value = SIGNED_URL
    bucket.delete_object.return_value = None
    bucket.object_exists.return_value = True
    monkeypatch.setattr(oss_storage, "_get_bucket", lambda: bucket)
    return bucket


def _mock_fetch(monkeypatch, body, content_type="text/csv"):
    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    monkeypatch.setattr(
        pack_import,
        "_build_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=10,
        ),
    )


def _build(client, headers, body, url=PUBLIC_URL, ct="text/csv", **extra):
    _payload_body = body
    payload = {
        "url": url,
        "agent_id": "oss-agent",
        "pack_id": "oss-pack",
        "title": "OSS Pack",
        **extra,
    }
    return client.post("/api/packs/build-from-url", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# OSS handoff via build-from-url
# ---------------------------------------------------------------------------
def test_process_and_return_uploads_to_oss_and_signs(
    client, auth, monkeypatch, enable_oss
):
    _mock_fetch(monkeypatch, b"row one\nrow two\nrow three")
    resp = _build(client, auth, b"row one\nrow two\nrow three")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["oss_export_uploaded"] is True
    assert job["oss_object_key"].startswith("exports/")
    assert job["oss_object_key"].endswith("oss-pack.smp.json")
    assert job["catalog_visible"] is False
    # download_url is now a stable, signature-free token URL (never the raw
    # OSS signed URL, whose base64 signature can be corrupted in transit).
    assert "/api/packs/dl/" in job["download_url"]
    assert job["download_url"] != SIGNED_URL
    assert "Signature=" not in job["download_url"]
    assert "aliyuncs.com" not in job["download_url"]
    enable_oss.put_object_from_file.assert_called_once()


def test_server_vault_persists_and_signs(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"alpha\nbeta\ngamma")
    resp = _build(client, auth, b"alpha\nbeta\ngamma", retention_mode="server_vault")
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["pack_persisted"] is True
    assert job["catalog_visible"] is True
    assert job["oss_export_uploaded"] is True
    assert "/api/packs/dl/" in job["download_url"]
    assert job["download_url"] != SIGNED_URL


def test_oss_disabled_falls_back_to_local_download(client, auth, monkeypatch):
    # OSS not configured -> local tokenized download URL, no OSS fields.
    monkeypatch.setattr(settings, "oss_enabled", False, raising=False)
    _mock_fetch(monkeypatch, b"one\ntwo\nthree")
    resp = _build(client, auth, b"one\ntwo\nthree")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()
    assert job["oss_export_uploaded"] is False
    assert job["oss_object_key"] is None
    # Local fallback is still exposed via a stable token URL.
    assert "/api/packs/dl/" in job["download_url"]


def test_return_download_url_false_skips_oss(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"one\ntwo")
    resp = _build(client, auth, b"one\ntwo", return_download_url=False)
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["oss_export_uploaded"] is False
    enable_oss.put_object_from_file.assert_not_called()


def test_no_secret_or_signed_query_in_logs(
    client, auth, monkeypatch, enable_oss, caplog
):
    _mock_fetch(monkeypatch, b"row one\nrow two")
    with caplog.at_level(logging.INFO):
        resp = _build(client, auth, b"row one\nrow two")
        job_id = resp.json()["job_id"]
        client.get(f"/api/jobs/{job_id}", headers=auth)
    text = caplog.text
    assert "SIGSECRET456" not in text
    assert "AKIDSECRET123" not in text
    assert "Signature=" not in text
    assert "OSSAccessKeyId=" not in text


# ---------------------------------------------------------------------------
# redact / service helpers
# ---------------------------------------------------------------------------
def test_redact_signed_url_strips_query():
    assert oss_storage.redact_signed_url(SIGNED_URL) == (
        "https://bkt.oss-ap-southeast-1.aliyuncs.com/exports/job/pack.smp.json"
    )
    assert oss_storage.redact_signed_url("") == ""


def test_is_enabled_requires_full_config(monkeypatch):
    monkeypatch.setattr(settings, "oss_enabled", True, raising=False)
    monkeypatch.setattr(settings, "oss_bucket", "", raising=False)
    assert oss_storage.is_enabled() is False


def test_bucket_endpoint_prefers_region_endpoint(monkeypatch):
    # A virtual-hosted OSS_BUCKET_ENDPOINT must NOT double-prefix the bucket.
    monkeypatch.setattr(settings, "oss_bucket", "mybkt", raising=False)
    monkeypatch.setattr(
        settings, "oss_endpoint", "https://oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "oss_bucket_endpoint",
        "https://mybkt.oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    # region endpoint wins outright
    assert oss_storage._bucket_construction_endpoint() == (
        "https://oss-ap-southeast-1.aliyuncs.com"
    )


def test_bucket_endpoint_strips_bucket_prefix_guard(monkeypatch):
    # If only a virtual-hosted endpoint is available, the <bucket>. label is
    # stripped so oss2.Bucket does not build <bucket>.<bucket>.oss-...
    monkeypatch.setattr(settings, "oss_bucket", "mybkt", raising=False)
    monkeypatch.setattr(settings, "oss_endpoint", "", raising=False)
    monkeypatch.setattr(
        settings,
        "oss_bucket_endpoint",
        "https://mybkt.oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    assert oss_storage._bucket_construction_endpoint() == (
        "https://oss-ap-southeast-1.aliyuncs.com"
    )


def test_get_bucket_passes_region_endpoint_to_oss2(monkeypatch):
    # oss2 is not installed; inject a fake module and assert the endpoint arg.
    import sys
    import types

    captured = {}

    fake_oss2 = types.ModuleType("oss2")

    def _auth(key_id, key_secret):
        return ("auth", key_id, key_secret)

    def _bucket(auth, endpoint, bucket_name):
        captured["endpoint"] = endpoint
        captured["bucket"] = bucket_name
        return MagicMock()

    fake_oss2.Auth = _auth
    fake_oss2.Bucket = _bucket
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)

    monkeypatch.setattr(settings, "oss_enabled", True, raising=False)
    monkeypatch.setattr(settings, "oss_bucket", "mybkt", raising=False)
    monkeypatch.setattr(
        settings, "oss_endpoint", "https://oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "oss_bucket_endpoint",
        "https://mybkt.oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(settings, "oss_access_key_id", "AKID", raising=False)
    monkeypatch.setattr(settings, "oss_access_key_secret", "SECRET", raising=False)

    oss_storage._get_bucket()
    # No double bucket prefix in the endpoint passed to oss2.Bucket.
    assert captured["endpoint"] == "https://oss-ap-southeast-1.aliyuncs.com"
    assert captured["bucket"] == "mybkt"
    assert "mybkt.mybkt" not in captured["endpoint"]


# ---------------------------------------------------------------------------
# presign endpoints (hidden from schema, auth-guarded, 503 when OSS off)
# ---------------------------------------------------------------------------
def test_presign_endpoints_hidden_from_openapi(client):
    spec = client.get("/openapi.json").json()
    ops = {
        o.get("operationId")
        for p in spec["paths"].values()
        for o in p.values()
        if isinstance(o, dict)
    }
    assert "presignUpload" not in ops
    assert "presignDownload" not in ops
    assert "deleteObject" not in ops


def test_presign_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    r = client.post("/api/files/presign-download", json={"object_key": "x"})
    assert r.status_code == 401


def test_presign_503_when_oss_disabled(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "oss_enabled", False, raising=False)
    r = client.post(
        "/api/files/presign-download",
        json={"object_key": "exports/x.smp.json"},
        headers=auth,
    )
    assert r.status_code == 503


def test_presign_download_ok_when_enabled(client, auth, enable_oss):
    r = client.post(
        "/api/files/presign-download",
        json={"object_key": "exports/x.smp.json"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["download_url"] == SIGNED_URL


def test_presign_upload_and_delete(client, auth, enable_oss):
    up = client.post(
        "/api/files/presign-upload",
        json={"filename": "a.csv", "content_type": "text/csv"},
        headers=auth,
    )
    assert up.status_code == 200
    assert up.json()["object_key"].startswith("uploads/")
    assert up.json()["upload_url"] == SIGNED_URL

    dl = client.request(
        "DELETE",
        "/api/files/object",
        json={"object_key": "uploads/a.csv"},
        headers=auth,
    )
    assert dl.status_code == 200
    assert dl.json()["deleted"] is True


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_health_reports_oss_enabled(client, enable_oss):
    body = client.get("/health").json()
    assert body["oss_enabled"] is True


def test_health_oss_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "oss_enabled", False, raising=False)
    assert client.get("/health").json()["oss_enabled"] is False


# ---------------------------------------------------------------------------
# .json ingest + folder unsupported / limits
# ---------------------------------------------------------------------------
def test_json_array_ingest(client, auth, monkeypatch):
    body = b'["first fact", "second fact", "third fact"]'
    _mock_fetch(monkeypatch, body, content_type="application/json")
    resp = _build(client, auth, body, url="https://8.8.8.8/data.json")
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["entry_count"] >= 3


def _folder_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_folder_zip_records_unsupported_files(client, auth, monkeypatch):
    body = _folder_zip(
        {
            "docs/a.csv": "a one\na two\na three",
            "docs/b.txt": "hello world",
            "docs/skip.pdf": b"%PDF-1.4 not ingestible",
        }
    )
    _mock_fetch(monkeypatch, body, content_type="application/zip")
    resp = _build(client, auth, body, url="https://8.8.8.8/folder.zip")
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    assert job["input_type"] == "folder"
    names = {u["filename"] for u in job["unsupported_files"]}
    assert "skip.pdf" in names


def test_folder_zip_max_file_count(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_max_folder_files", 2, raising=False)
    body = _folder_zip(
        {f"f{i}.csv": f"row {i} a\nrow {i} b" for i in range(5)}
    )
    _mock_fetch(monkeypatch, body, content_type="application/zip")
    resp = _build(client, auth, body, url="https://8.8.8.8/folder.zip")
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    assert job["status"] == "FAILED"
    assert any("file limit" in w.lower() for w in job["warnings"])
