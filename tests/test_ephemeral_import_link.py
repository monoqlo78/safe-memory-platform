"""Ephemeral, tenant-isolated pack import via a one-time upload link.

An import-mode one-time link (createUploadLink mode="import") lets a user open
the keyless ``/u/{token}`` page and upload finished ``.smp.json`` packs. Each
pack is imported into the link's PRIVATE, unguessable namespace (never the
shared vault) and auto-expires with the link's TTL. getUploadLinkResult returns
the imported packs so the authenticated LLM learns which agent_id/pack_id to
query. Other links cannot reach these packs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import jobs_store, pack_io
from app.core.auth import API_KEY_HEADER, UPLOAD_TOKEN_HEADER
from app.main import app

TEST_KEY = "ephemeral-import-key-xyz"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_pack_bytes(client, auth, agent="src-agent", pack_id="orig", text=None):
    text = text or "The quarterly revenue grew. Taxes were filed on time."
    resp = client.post(
        "/api/packs/build",
        json={
            "agent_id": agent,
            "pack_id": pack_id,
            "title": "Original Pack",
            "source_text": text,
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    pack = pack_io.load_pack(resp.json()["pack_path"])
    return json.dumps(pack.model_dump(mode="json")).encode("utf-8")


def _create_import_link(client, auth):
    resp = client.post(
        "/api/upload-links",
        json={"title": "Import my packs", "mode": "import"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token_of(client, auth, claim_id):
    """The token is never returned by the API; read it from the persisted claim."""
    from app.core import upload_links

    claim = upload_links.load_claim(claim_id)
    assert claim is not None
    return claim.token


def _stage_token(client, token, data, filename="pack.smp.json"):
    headers = {UPLOAD_TOKEN_HEADER: token}
    init = client.post(
        "/api/uploads/init",
        json={"filename": filename, "content_type": "application/json", "size": len(data)},
        headers=headers,
    )
    assert init.status_code == 200, init.text
    init = init.json()
    put = client.put(
        f"/api/uploads/{init['upload_id']}/content",
        params={"token": init["upload_token"]},
        content=data,
    )
    assert put.status_code == 200, put.text
    return init["upload_id"]


def _import_token(client, token, upload_id):
    return client.post(
        "/api/packs/import-from-upload-ref",
        json={"upload_id": upload_id, "agent_id": "import"},
        headers={UPLOAD_TOKEN_HEADER: token},
    )


def _import_via_link(client, auth, data):
    link = _create_import_link(client, auth)
    token = _token_of(client, auth, link["claim_id"])
    upload_id = _stage_token(client, token, data)
    resp = _import_token(client, token, upload_id)
    assert resp.status_code == 200, resp.text
    return link, resp.json()


# ---------------------------------------------------------------------------
# Link creation
# ---------------------------------------------------------------------------
def test_create_import_link_returns_import_mode(client, auth):
    link = _create_import_link(client, auth)
    assert link["mode"] == "import"
    assert link["upload_url"].endswith("/u/" + _token_of(client, auth, link["claim_id"]))


def test_build_mode_is_default(client, auth):
    resp = client.post("/api/upload-links", json={"title": "x"}, headers=auth)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "build"


# ---------------------------------------------------------------------------
# Import -> result listing -> queryable
# ---------------------------------------------------------------------------
def test_import_mode_result_lists_imported_packs(client, auth):
    data = _build_pack_bytes(client, auth, pack_id="ephem-a")
    link, result = _import_via_link(client, auth, data)
    assert result["pack_id"] == "ephem-a"

    poll = client.get(f"/api/upload-links/{link['claim_id']}", headers=auth)
    assert poll.status_code == 200, poll.text
    body = poll.json()
    assert body["status"] == "COMPLETED"
    assert body["mode"] == "import"
    assert len(body["imported"]) == 1
    item = body["imported"][0]
    assert item["pack_id"] == "ephem-a"
    assert item["agent_id"].startswith("imp-")
    assert item["entry_count"] >= 1
    assert "classifications" in item


def test_imported_pack_is_queryable_in_ephemeral_namespace(client, auth):
    data = _build_pack_bytes(client, auth, pack_id="ephem-q")
    link, _ = _import_via_link(client, auth, data)
    item = client.get(
        f"/api/upload-links/{link['claim_id']}", headers=auth
    ).json()["imported"][0]

    q = client.post(
        "/api/packs/query",
        json={
            "agent_id": item["agent_id"],
            "pack_id": item["pack_id"],
            "query": "What happened to revenue?",
        },
        headers=auth,
    )
    assert q.status_code == 200, q.text
    assert q.json()["pack_id"] == "ephem-q"


def test_import_not_written_to_shared_vault(client, auth):
    """The pack must NOT land in the request's placeholder agent ('import')."""
    data = _build_pack_bytes(client, auth, pack_id="ephem-vault")
    _import_via_link(client, auth, data)
    # The keyless page sends agent_id='import'; the server overrides it with the
    # link's private namespace, so 'import' must have no such pack.
    assert pack_io.find_pack_by_id("import", "ephem-vault") is None


def test_multiple_packs_on_one_import_link(client, auth):
    link = _create_import_link(client, auth)
    token = _token_of(client, auth, link["claim_id"])
    for pid in ("ephem-1", "ephem-2", "ephem-3"):
        data = _build_pack_bytes(client, auth, pack_id=pid)
        uid = _stage_token(client, token, data)
        resp = _import_token(client, token, uid)
        assert resp.status_code == 200, resp.text

    body = client.get(f"/api/upload-links/{link['claim_id']}", headers=auth).json()
    assert body["status"] == "COMPLETED"
    got = sorted(i["pack_id"] for i in body["imported"])
    assert got == ["ephem-1", "ephem-2", "ephem-3"]
    # All share the same private namespace.
    agents = {i["agent_id"] for i in body["imported"]}
    assert len(agents) == 1


# ---------------------------------------------------------------------------
# Tenant isolation: two links get distinct, unreachable namespaces
# ---------------------------------------------------------------------------
def test_two_links_are_isolated(client, auth):
    data_a = _build_pack_bytes(client, auth, pack_id="iso-a")
    data_b = _build_pack_bytes(client, auth, pack_id="iso-b")
    link_a, _ = _import_via_link(client, auth, data_a)
    link_b, _ = _import_via_link(client, auth, data_b)

    agent_a = client.get(
        f"/api/upload-links/{link_a['claim_id']}", headers=auth
    ).json()["imported"][0]["agent_id"]
    agent_b = client.get(
        f"/api/upload-links/{link_b['claim_id']}", headers=auth
    ).json()["imported"][0]["agent_id"]

    assert agent_a != agent_b
    # link B's pack is not reachable under link A's namespace.
    assert pack_io.find_pack_by_id(agent_a, "iso-b") is None
    assert pack_io.find_pack_by_id(agent_b, "iso-a") is None


# ---------------------------------------------------------------------------
# TTL: cleanup removes the ephemeral pack after expiry
# ---------------------------------------------------------------------------
def test_expired_import_is_cleaned_up(client, auth):
    data = _build_pack_bytes(client, auth, pack_id="ephem-ttl")
    link, _ = _import_via_link(client, auth, data)
    item = client.get(
        f"/api/upload-links/{link['claim_id']}", headers=auth
    ).json()["imported"][0]
    agent_id = item["agent_id"]

    assert pack_io.find_pack_by_id(agent_id, "ephem-ttl") is not None

    # Force the backing temp job past its TTL, then run the cleanup sweep.
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    touched = 0
    for job in jobs_store.list_jobs():
        if job.agent_id == agent_id and job.pack_id == "ephem-ttl":
            job.expires_at = past
            jobs_store.save_job(job)
            touched += 1
    assert touched == 1

    summary = client.post("/api/jobs/cleanup", headers=auth)
    assert summary.status_code == 200, summary.text
    assert summary.json()["packs_deleted"] >= 1

    # Pack file is gone; querying it now 404s.
    assert pack_io.find_pack_by_id(agent_id, "ephem-ttl") is None
    q = client.post(
        "/api/packs/query",
        json={"agent_id": agent_id, "pack_id": "ephem-ttl", "query": "revenue?"},
        headers=auth,
    )
    assert q.status_code == 404


# ---------------------------------------------------------------------------
# A build-mode token must not be repurposed to import packs
# ---------------------------------------------------------------------------
def test_build_token_cannot_import(client, auth):
    resp = client.post(
        "/api/upload-links", json={"title": "build link"}, headers=auth
    )
    assert resp.json()["mode"] == "build"
    token = _token_of(client, auth, resp.json()["claim_id"])

    data = _build_pack_bytes(client, auth, pack_id="nope")
    upload_id = _stage_token(client, token, data)
    imp = _import_token(client, token, upload_id)
    assert imp.status_code == 403


# ---------------------------------------------------------------------------
# The keyless /u import page and OpenAPI guards
# ---------------------------------------------------------------------------
def test_import_mode_u_page_served(client, auth):
    link = _create_import_link(client, auth)
    token = _token_of(client, auth, link["claim_id"])
    page = client.get(f"/u/{token}")
    assert page.status_code == 200
    body = page.text
    assert "Import Your Packs" in body
    assert "X-Upload-Token" in body
    # Keyless: no master API key field on this page.
    assert "X-Safe-Memory-Key" not in body


def test_upload_link_operation_ids_still_visible(client):
    spec = client.get("/openapi.json").json()
    op_ids = {
        v.get("operationId")
        for path in spec["paths"].values()
        for v in path.values()
        if isinstance(v, dict)
    }
    assert "createUploadLink" in op_ids
    assert "getUploadLinkResult" in op_ids
    # The internal import endpoint stays hidden.
    assert "importMemoryPackFromUploadRef" not in op_ids
    assert "/api/packs/import-from-upload-ref" not in spec["paths"]


def _descriptions_by_op(spec):
    out = {}
    for path in spec["paths"].values():
        for op in path.values():
            if isinstance(op, dict) and op.get("operationId"):
                out[op["operationId"]] = op.get("description", "")
    return out


def test_action_descriptions_embed_import_flow_guidance(client):
    """GPT Actions have no connect-time instructions, so the flow lives in the
    operation descriptions (and each stays within the 300-char GPT limit)."""
    desc = _descriptions_by_op(client.get("/openapi.json").json())

    create = desc["createUploadLink"]
    assert "mode=import" in create
    assert "attached" in create.lower() or "pasted" in create.lower()

    result = desc["getUploadLinkResult"]
    assert "imported" in result.lower()
    assert "agent_id" in result and "pack_id" in result

    query = desc["queryMemoryPack"]
    assert "getUploadLinkResult" in query

    for op_id in ("createUploadLink", "getUploadLinkResult", "queryMemoryPack"):
        assert len(desc[op_id]) <= 300, f"{op_id} description over 300 chars"

