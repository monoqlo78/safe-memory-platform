"""Stable, signature-free pack download URLs.

Job ``download_url`` is now a tokenized ``/api/packs/dl/{token}`` URL instead of
a raw OSS signed URL. The token carries only URL-safe characters, so the base64
signature of an OSS URL can never be corrupted when the link is passed through
GPT/ChatGPT. The token route streams the local pack when present, else
307-redirects to a freshly signed OSS URL; the signature then travels only in the
``Location`` header.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import export_links, jobs_store, oss_storage, pack_import, pack_io
from app.core.auth import API_KEY_HEADER
from app.main import app
from app.models.job_schema import JobRecord, JobStatus, RetentionMode, job_to_response

TEST_KEY = "dl-test-key"
PUBLIC_URL = "https://8.8.8.8/data.csv"

SIGNED_URL = (
    "https://bkt.oss-ap-southeast-1.aliyuncs.com/exports/job/pack.smp.json"
    "?OSSAccessKeyId=AKIDSECRET123&Signature=SIG+slash/eq=456&Expires=9999999999"
)

_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


@pytest.fixture
def enable_oss(monkeypatch):
    monkeypatch.setattr(settings, "oss_enabled", True, raising=False)
    monkeypatch.setattr(settings, "oss_bucket", "bkt", raising=False)
    monkeypatch.setattr(
        settings, "oss_endpoint", "https://oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(settings, "oss_access_key_id", "AKIDSECRET123", raising=False)
    monkeypatch.setattr(settings, "oss_access_key_secret", "SIGSECRET456", raising=False)
    bucket = MagicMock()
    bucket.put_object_from_file.return_value = MagicMock(etag="etag123")
    bucket.sign_url.return_value = SIGNED_URL
    monkeypatch.setattr(oss_storage, "_get_bucket", lambda: bucket)
    return bucket


def _mock_fetch(monkeypatch, body, content_type="text/csv"):
    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    monkeypatch.setattr(
        pack_import,
        "_build_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10
        ),
    )


def _build(client, headers, body, **extra):
    _mock_fetch_body = body
    payload = {
        "url": PUBLIC_URL,
        "agent_id": "dl-agent",
        "pack_id": "dl-pack",
        "title": "DL Pack",
        **extra,
    }
    return client.post("/api/packs/build-from-url", json=payload, headers=headers)


def _token_of(download_url: str) -> str:
    return download_url.rsplit("/api/packs/dl/", 1)[1]


# ---------------------------------------------------------------------------
# download_url shape
# ---------------------------------------------------------------------------
def test_build_download_url_is_token_not_signed(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"row one\nrow two\nrow three")
    resp = _build(client, auth, b"row one\nrow two\nrow three")
    assert resp.status_code == 200
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()

    url = job["download_url"]
    assert "/api/packs/dl/" in url
    assert url != SIGNED_URL
    assert "Signature=" not in url
    assert "aliyuncs.com" not in url
    assert "OSSAccessKeyId" not in url


def test_token_is_url_safe(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"a\nb\nc")
    resp = _build(client, auth, b"a\nb\nc")
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()
    token = _token_of(job["download_url"])
    assert _URL_SAFE_RE.match(token)
    assert "+" not in token and "/" not in token and "=" not in token


# ---------------------------------------------------------------------------
# token route behavior: stream local, else 307 redirect, else 404
# ---------------------------------------------------------------------------
def test_token_streams_local_pack(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"alpha\nbeta\ngamma")
    resp = _build(client, auth, b"alpha\nbeta\ngamma")
    job = client.get(f"/api/jobs/{resp.json()['job_id']}", headers=auth).json()

    # process_and_return keeps the pack locally -> streamed, no API key needed.
    dl = client.get(job["download_url"])
    assert dl.status_code == 200
    assert dl.json()["manifest"]["pack_id"] == "dl-pack"


def test_token_redirects_to_oss_when_local_missing(
    client, auth, monkeypatch, enable_oss
):
    _mock_fetch(monkeypatch, b"one\ntwo\nthree")
    resp = _build(client, auth, b"one\ntwo\nthree")
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()

    # Simulate TTL cleanup of the local pack; OSS object still exists.
    rec = jobs_store.load_job(job_id)
    pack_io.ensure_safe_path(rec.pack_path).unlink()

    dl = client.get(job["download_url"], follow_redirects=False)
    assert dl.status_code == 307
    assert dl.headers["location"] == SIGNED_URL


def test_token_404_when_neither(client, auth, monkeypatch, enable_oss):
    _mock_fetch(monkeypatch, b"x\ny\nz")
    resp = _build(client, auth, b"x\ny\nz")
    job_id = resp.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth).json()

    rec = jobs_store.load_job(job_id)
    pack_io.ensure_safe_path(rec.pack_path).unlink()
    # OSS now disabled -> no local pack and no redirect target.
    monkeypatch.setattr(settings, "oss_enabled", False, raising=False)

    dl = client.get(job["download_url"], follow_redirects=False)
    assert dl.status_code == 404


# ---------------------------------------------------------------------------
# exportMemoryPack regression: no oss_object_key -> local stream
# ---------------------------------------------------------------------------
def test_export_link_without_oss_streams_local(client, auth, safe_root):
    # A token minted with only a local rel_path (no OSS) streams the file.
    rel = "exports/regression.smp.json"
    path = pack_io.ensure_safe_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"manifest": {"pack_id": "regression"}}', encoding="utf-8")

    token = export_links.create_export_link("dl-agent", rel)
    dl = client.get(f"/api/packs/dl/{token}")
    assert dl.status_code == 200
    assert dl.json()["manifest"]["pack_id"] == "regression"


# ---------------------------------------------------------------------------
# job_to_response never emits a raw OSS host
# ---------------------------------------------------------------------------
def test_job_to_response_never_emits_raw_oss_host():
    job = JobRecord(
        job_id="j1",
        agent_id="a",
        pack_id="p",
        status=JobStatus.COMPLETED,
        retention_mode=RetentionMode.PROCESS_AND_RETURN,
        oss_object_key="exports/j1/p.smp.json",
        oss_export_uploaded=True,
        download_token="tok_ABC-123_xyz",
    )
    resp = job_to_response(job)
    assert resp.download_url == "/api/packs/dl/tok_ABC-123_xyz"
    assert "aliyuncs.com" not in resp.download_url
    assert "Signature=" not in resp.download_url
    # oss_object_key stays available for the redirect fallback.
    assert resp.oss_object_key == "exports/j1/p.smp.json"


def test_job_to_response_absolute_with_public_base(monkeypatch):
    monkeypatch.setattr(
        settings, "safe_memory_public_base_url", "https://smp.example.com", raising=False
    )
    job = JobRecord(
        job_id="j2",
        agent_id="a",
        pack_id="p",
        status=JobStatus.COMPLETED,
        retention_mode=RetentionMode.PROCESS_AND_RETURN,
        download_token="tok2",
    )
    resp = job_to_response(job)
    assert resp.download_url == "https://smp.example.com/api/packs/dl/tok2"
