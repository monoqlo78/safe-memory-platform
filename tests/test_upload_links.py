"""One-time (SAS-like) upload link flow.

An authenticated client mints a keyless, single-use link with createUploadLink.
The end user opens ``/u/{token}`` (no API key) and uploads a folder; the token
authorizes only staging + one scoped build. The authenticated client then polls
getUploadLinkResult for the finished pack. These tests drive the same server
path the browser page takes and assert the scoping / single-use guarantees.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import upload_links
from app.core.auth import API_KEY_HEADER, UPLOAD_TOKEN_HEADER
from app.main import app

TEST_KEY = "one-time-link-key"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


def _folder_zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _create_link(client, auth, **body):
    resp = client.post("/api/upload-links", json=body, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token_from_upload_url(url: str) -> str:
    return url.rstrip("/").split("/u/")[-1]


def _upload_with_token(client, token, data, filename="bundle.zip"):
    """init -> PUT -> build-from-upload-ref using only the one-time token."""
    tok_header = {UPLOAD_TOKEN_HEADER: token}
    init = client.post(
        "/api/uploads/init",
        json={"filename": filename, "content_type": "application/zip", "size": len(data)},
        headers=tok_header,
    )
    assert init.status_code == 200, init.text
    init = init.json()
    put = client.put(
        f"/api/uploads/{init['upload_id']}/content",
        params={"token": init["upload_token"]},
        content=data,
    )
    assert put.status_code == 200, put.text
    ref = client.post(
        "/api/packs/build-from-upload-ref",
        json={"upload_id": init["upload_id"], "agent_id": "x", "pack_id": "x", "title": "x"},
        headers=tok_header,
    )
    return init, ref


# ---------------------------------------------------------------------------
# (a) createUploadLink issues a public token URL and never leaks the master key
# ---------------------------------------------------------------------------
def test_create_upload_link_returns_public_token_url(client, auth, monkeypatch):
    monkeypatch.setattr(
        settings, "safe_memory_public_base_url", "https://smp.sdesigner.tokyo",
        raising=False,
    )
    data = _create_link(client, auth, agent_id="team", title="Project X")
    assert data["upload_url"].startswith("https://smp.sdesigner.tokyo/u/")
    assert data["claim_id"]
    assert data["expires_at"]
    # The master API key must never appear anywhere in the response.
    assert TEST_KEY not in resp_text(data)
    token = _token_from_upload_url(data["upload_url"])
    assert token and token != TEST_KEY


def resp_text(obj) -> str:
    import json

    return json.dumps(obj)


def test_create_upload_link_requires_master_key(client, auth):
    # No key at all -> middleware rejects createUploadLink.
    resp = client.post("/api/upload-links", json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# (b) valid token works; invalid / expired / used -> 401
# ---------------------------------------------------------------------------
def test_valid_token_can_stage_and_build(client, auth):
    link = _create_link(client, auth, agent_id="team", pack_id="link-pack")
    token = _token_from_upload_url(link["upload_url"])
    data = _folder_zip_bytes({"a.csv": b"term,def\nx,y\n", "b.csv": b"term,def\np,q\n"})
    init, ref = _upload_with_token(client, token, data)
    assert ref.status_code == 200, ref.text
    job = client.get(f"/api/jobs/{ref.json()['job_id']}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    # Claim settings win: the pack_id comes from the link, not the request body.
    assert job["pack_id"] == "link-pack"
    assert job["input_type"] == "folder"
    assert job["entry_count"] >= 2


def test_invalid_token_is_rejected(client, auth):
    resp = client.post(
        "/api/uploads/init",
        json={"filename": "x.zip", "size": 3},
        headers={UPLOAD_TOKEN_HEADER: "not-a-real-token"},
    )
    assert resp.status_code == 401


def test_expired_token_is_rejected(client, auth, monkeypatch):
    link = _create_link(client, auth, expires_in_seconds=60)
    token = _token_from_upload_url(link["upload_url"])
    # Force the stored claim to be already expired.
    claim = upload_links.find_claim_by_token(token)
    claim.expires_at = "2000-01-01T00:00:00+00:00"
    upload_links.save_claim(claim)
    resp = client.post(
        "/api/uploads/init",
        json={"filename": "x.zip", "size": 3},
        headers={UPLOAD_TOKEN_HEADER: token},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# (c) a one-time token is scoped: no catalog / query / delete access
# ---------------------------------------------------------------------------
def test_token_cannot_access_other_endpoints(client, auth):
    link = _create_link(client, auth)
    token = _token_from_upload_url(link["upload_url"])
    tok = {UPLOAD_TOKEN_HEADER: token}
    # Catalog, query, and job deletion are all outside the token's scope.
    assert client.get("/api/agents/team/catalog", headers=tok).status_code == 401
    assert client.post(
        "/api/packs/query", json={"pack_id": "p", "query": "q"}, headers=tok
    ).status_code == 401
    assert client.delete("/api/jobs/whatever", headers=tok).status_code == 401


# ---------------------------------------------------------------------------
# (d) getUploadLinkResult returns COMPLETED with the reusable pack details
# ---------------------------------------------------------------------------
def test_get_upload_link_result_completes(client, auth):
    link = _create_link(client, auth, pack_id="result-pack")
    token = _token_from_upload_url(link["upload_url"])
    data = _folder_zip_bytes({"a.csv": b"term,def\nx,y\n"})
    _upload_with_token(client, token, data)

    result = client.get(f"/api/upload-links/{link['claim_id']}", headers=auth)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "COMPLETED"
    assert body["pack_id"] == "result-pack"
    assert body["download_url"]
    assert body["entry_count"] >= 1
    # The result view must never echo the one-time token or master key.
    assert token not in resp_text(body)
    assert TEST_KEY not in resp_text(body)


def test_get_upload_link_result_unknown_claim(client, auth):
    assert client.get("/api/upload-links/nope", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# (e) max_uses=1 is consumed after one build; a second build is rejected
# ---------------------------------------------------------------------------
def test_single_use_token_is_consumed(client, auth):
    link = _create_link(client, auth, max_uses=1)
    token = _token_from_upload_url(link["upload_url"])
    data = _folder_zip_bytes({"a.csv": b"term,def\nx,y\n"})
    _, ref1 = _upload_with_token(client, token, data)
    assert ref1.status_code == 200
    # Second attempt with the same (now consumed) token must fail.
    resp = client.post(
        "/api/uploads/init",
        json={"filename": "b.zip", "size": 3},
        headers={UPLOAD_TOKEN_HEADER: token},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# (f) the /u/{token} page has no API-key field but has the folder picker/JSZip
# ---------------------------------------------------------------------------
def test_one_time_page_served_without_key_field(client, auth):
    link = _create_link(client, auth)
    token = _token_from_upload_url(link["upload_url"])
    page = client.get(f"/u/{token}")
    assert page.status_code == 200
    html = page.text
    assert 'id="apiKey"' not in html
    assert "X-Safe-Memory-Key" not in html
    assert "webkitdirectory" in html
    assert "jszip" in html.lower()
    assert "X-Upload-Token" in html


def test_one_time_page_invalid_token_shows_error(client, auth):
    page = client.get("/u/some-bogus-token")
    assert page.status_code == 404
    assert "one-time upload link" in page.text.lower()
