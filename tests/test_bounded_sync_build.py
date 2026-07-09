"""Bounded-synchronous buildPackFromUrl behavior.

buildPackFromUrl (and the MCP build_pack_from_url tool) now wait up to
``safe_memory_sync_build_wait_seconds`` for the build to finish. Fast builds
return the terminal job (with a signature-free download_url) in a single call;
slow builds fall back to {job_id, PROCESSING} for polling. Also covers the
server_vault download_url invariant (always minted, even without OSS).

The fetch path is mocked with httpx.MockTransport (no network); a public IP
literal host is used so the SSRF guard passes without DNS. OSS is faked (no oss2
import).
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import packs as packs_api
from app.config import settings
from app.core import oss_storage, pack_import
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "bounded-build-key"
PUBLIC_URL = "https://8.8.8.8/data.csv"

SIGNED_URL = (
    "https://bkt.oss-ap-southeast-1.aliyuncs.com/exports/job/pack.smp.json"
    "?OSSAccessKeyId=AKID&Signature=SIG&Expires=9999999999"
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
    monkeypatch.setattr(settings, "oss_enabled", True, raising=False)
    monkeypatch.setattr(settings, "oss_bucket", "bkt", raising=False)
    monkeypatch.setattr(
        settings, "oss_endpoint", "https://oss-ap-southeast-1.aliyuncs.com",
        raising=False,
    )
    monkeypatch.setattr(settings, "oss_access_key_id", "AKID", raising=False)
    monkeypatch.setattr(settings, "oss_access_key_secret", "SIG", raising=False)
    bucket = MagicMock()
    bucket.put_object_from_file.return_value = MagicMock(etag="etag123")
    bucket.sign_url.return_value = SIGNED_URL
    bucket.object_exists.return_value = True
    monkeypatch.setattr(oss_storage, "_get_bucket", lambda: bucket)
    return bucket


def _mock_fetch(monkeypatch, body, content_type="text/csv"):
    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": content_type}
        )

    monkeypatch.setattr(
        pack_import,
        "_build_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=10,
        ),
    )


def _build(client, headers, **extra):
    payload = {
        "url": PUBLIC_URL,
        "agent_id": "bnd-agent",
        "pack_id": "bnd-pack",
        "title": "Bounded Pack",
        **extra,
    }
    return client.post("/api/packs/build-from-url", json=payload, headers=headers)


def _poll(client, job_id, headers, want="COMPLETED", timeout=8.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] == want:
            return job
        time.sleep(0.1)
    return job


# ---------------------------------------------------------------------------
# Fast path: single round-trip terminal response with download_url
# ---------------------------------------------------------------------------
def test_server_vault_single_roundtrip_completed(client, auth, monkeypatch):
    _mock_fetch(monkeypatch, b"row one\nrow two\nrow three")
    resp = _build(client, auth, retention_mode="server_vault")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["entry_count"] == 3
    assert body["pack_persisted"] is True
    # Invariant: a COMPLETED job always carries a stable download_url.
    assert body["download_url"] and "/api/packs/dl/" in body["download_url"]


def test_process_and_return_single_roundtrip_completed(client, auth, monkeypatch):
    _mock_fetch(monkeypatch, b"alpha\nbeta")
    resp = _build(client, auth, retention_mode="process_and_return")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert "/api/packs/dl/" in body["download_url"]


# ---------------------------------------------------------------------------
# Slow path: wait bound elapses -> PROCESSING, job still completes in the bg
# ---------------------------------------------------------------------------
def test_slow_build_returns_processing_then_completes(client, auth, monkeypatch):
    _mock_fetch(monkeypatch, b"one\ntwo\nthree")
    # Shrink the wait window and make the ingest deliberately slow so the wait
    # elapses before the build finishes.
    monkeypatch.setattr(
        settings, "safe_memory_sync_build_wait_seconds", 0.05, raising=False
    )
    original = packs_api._process_ref_job

    def slow_process(*args, **kwargs):
        time.sleep(0.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(packs_api, "_process_ref_job", slow_process)

    resp = _build(client, auth, retention_mode="server_vault")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The wait elapsed -> accepted/PROCESSING shape for polling.
    assert body["status"] == "PROCESSING"
    assert body["job_id"]
    assert body["upload_id"]

    # The shielded background task keeps running and finishes.
    job = _poll(client, body["job_id"], auth, "COMPLETED")
    assert job["status"] == "COMPLETED"
    assert "/api/packs/dl/" in job["download_url"]


# ---------------------------------------------------------------------------
# server_vault download_url invariant (Change 3)
# ---------------------------------------------------------------------------
def test_server_vault_download_url_without_oss_streams_local(client, auth, monkeypatch):
    # OSS disabled by default -> download_url must still be present and stream
    # the locally persisted vault pack.
    _mock_fetch(monkeypatch, b"gamma\ndelta\nepsilon")
    resp = _build(client, auth, retention_mode="server_vault")
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["oss_export_uploaded"] is False
    assert body["oss_object_key"] is None
    assert body["download_url"] and "/api/packs/dl/" in body["download_url"]

    # The token URL is no-auth and streams the local pack.
    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.json()["manifest"]["pack_id"] == "bnd-pack"


def test_server_vault_download_url_when_oss_upload_fails(
    client, auth, monkeypatch, enable_oss
):
    # OSS is enabled but the upload raises -> download_url must not be None
    # (the pack is persisted locally); a warning is acceptable.
    _mock_fetch(monkeypatch, b"zeta\neta\ntheta")

    def boom(*args, **kwargs):
        raise RuntimeError("oss down")

    monkeypatch.setattr(oss_storage, "upload_file", boom)

    resp = _build(client, auth, retention_mode="server_vault")
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["oss_export_uploaded"] is False
    assert body["download_url"] and "/api/packs/dl/" in body["download_url"]
    dl = client.get(body["download_url"])
    assert dl.status_code == 200


# ---------------------------------------------------------------------------
# MCP build_pack_from_url shares the bounded-synchronous behavior
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _call_tool(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    from app.mcp_server import build_mcp

    mcp = build_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as sess:
        return await sess.call_tool(name, args)


def test_mcp_build_pack_from_url_fast_returns_completed(safe_root, monkeypatch):
    _mock_fetch(monkeypatch, b"m1\nm2\nm3")
    result = _run(
        _call_tool(
            "build_pack_from_url",
            {
                "url": PUBLIC_URL,
                "agent_id": "mcp-agent",
                "pack_id": "mcp-pack",
                "title": "MCP Pack",
            },
        )
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "COMPLETED"
    assert "/api/packs/dl/" in payload["download_url"]
