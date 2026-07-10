"""Token-scoped cross-pack query via one import-mode upload link.

``POST /api/packs/query-by-upload`` (operationId ``queryUploadedMemory``) lets a
GPT Action search EVERY pack a user uploaded through a single import link by
passing only the link's ``claim_id`` (from ``createUploadLink(mode=import)``).
The assistant never juggles the per-pack ephemeral ``imp-`` agent_id/pack_id
values, which removes a class of empty-404 "queried the wrong claim" bugs.

These tests run in deterministic fallback mode (Qwen disabled) via conftest.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import pack_io, upload_links
from app.core.auth import API_KEY_HEADER, UPLOAD_TOKEN_HEADER
from app.main import app

TEST_KEY = "query-by-upload-key-abc"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


# ---------------------------------------------------------------------------
# Helpers (mirror the ephemeral-import-link flow)
# ---------------------------------------------------------------------------
def _build_pack_bytes(client, auth, pack_id, text):
    resp = client.post(
        "/api/packs/build",
        json={
            "agent_id": "src-agent",
            "pack_id": pack_id,
            "title": f"Pack {pack_id}",
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


def _token_of(claim_id):
    """The token is never returned by the API; read it from the persisted claim."""
    claim = upload_links.load_claim(claim_id)
    assert claim is not None
    return claim.token


def _stage_token(client, token, data, filename="pack.smp.json"):
    headers = {UPLOAD_TOKEN_HEADER: token}
    init = client.post(
        "/api/uploads/init",
        json={
            "filename": filename,
            "content_type": "application/json",
            "size": len(data),
        },
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
    resp = client.post(
        "/api/packs/import-from-upload-ref",
        json={"upload_id": upload_id, "agent_id": "import"},
        headers={UPLOAD_TOKEN_HEADER: token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _import_packs(client, auth, packs):
    """Create one import link and import every (pack_id, text) into it."""
    link = _create_import_link(client, auth)
    token = _token_of(link["claim_id"])
    for pack_id, text in packs:
        data = _build_pack_bytes(client, auth, pack_id, text)
        upload_id = _stage_token(client, token, data)
        _import_token(client, token, upload_id)
    return link


# ---------------------------------------------------------------------------
# Cross-pack query: one claim_id searches every uploaded pack
# ---------------------------------------------------------------------------
def test_query_by_upload_hits_across_two_packs(client, auth):
    link = _import_packs(
        client,
        auth,
        [
            ("qbu-a", "The quarterly revenue grew strongly and taxes were filed."),
            ("qbu-b", "The revenue outlook for next year remains positive."),
        ],
    )

    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": link["claim_id"], "query": "How is revenue doing?"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    total_hits = sum(p["hits"] for p in body["used_packs"])
    assert total_hits > 0
    hit_pack_ids = {p["pack_id"] for p in body["used_packs"] if p["hits"] > 0}
    # Both uploaded packs are searched under the same private namespace.
    assert {"qbu-a", "qbu-b"} <= hit_pack_ids
    for p in body["used_packs"]:
        assert p["agent_id"].startswith("imp-")
    assert "answer" in body
    assert "confidence" in body
    assert "fallback" in body


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_unknown_claim_id_returns_404(client, auth):
    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": "does-not-exist", "query": "anything"},
        headers=auth,
    )
    assert resp.status_code == 404


def test_expired_claim_returns_404(client, auth):
    link = _import_packs(client, auth, [("qbu-exp", "Some content about revenue.")])

    # Force the claim past its TTL, then query.
    claim = upload_links.load_claim(link["claim_id"])
    claim.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    upload_links.save_claim(claim)

    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": link["claim_id"], "query": "revenue?"},
        headers=auth,
    )
    assert resp.status_code == 404


def test_build_mode_claim_returns_400(client, auth):
    # A build-mode link is not an import link and cannot be cross-queried.
    resp = client.post(
        "/api/upload-links", json={"title": "build link"}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "build"
    claim_id = resp.json()["claim_id"]

    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": claim_id, "query": "revenue?"},
        headers=auth,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tenant isolation: one claim never reaches another claim's packs
# ---------------------------------------------------------------------------
def test_isolation_other_claim_packs_unreachable(client, auth):
    link_a = _import_packs(
        client, auth, [("iso-a", "Alpha project budget details and revenue.")]
    )
    link_b = _import_packs(
        client, auth, [("iso-b", "Bravo project budget details and revenue.")]
    )

    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": link_a["claim_id"], "query": "budget details"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    pack_ids = {p["pack_id"] for p in body["used_packs"]}
    # Link A only ever sees its own pack; link B's pack is unreachable.
    assert "iso-a" in pack_ids
    assert "iso-b" not in pack_ids

    agent_a = upload_links.load_claim(link_a["claim_id"]).import_agent_id
    agent_b = upload_links.load_claim(link_b["claim_id"]).import_agent_id
    assert agent_a != agent_b
    for p in body["used_packs"]:
        assert p["agent_id"] == agent_a


# ---------------------------------------------------------------------------
# Auth: master key required
# ---------------------------------------------------------------------------
def test_missing_master_key_returns_401(client, auth):
    # No X-Safe-Memory-Key header while auth is enabled.
    resp = client.post(
        "/api/packs/query-by-upload",
        json={"claim_id": "whatever", "query": "revenue?"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OpenAPI (GPT Actions) guards
# ---------------------------------------------------------------------------
def _op_map(client):
    spec = client.get("/openapi.json").json()
    return {
        op.get("operationId"): op
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }, spec


def test_query_uploaded_memory_visible_import_ref_hidden(client):
    ops, spec = _op_map(client)
    # The new cross-pack query is visible to GPT Actions.
    assert "queryUploadedMemory" in ops
    assert "/api/packs/query-by-upload" in spec["paths"]
    # The internal per-pack import endpoint stays hidden.
    assert "importMemoryPackFromUploadRef" not in ops
    assert "/api/packs/import-from-upload-ref" not in spec["paths"]


def test_query_uploaded_memory_description_within_gpt_limit(client):
    ops, _ = _op_map(client)
    desc = ops["queryUploadedMemory"].get("description", "")
    assert desc, "queryUploadedMemory must have a description"
    assert len(desc) <= 300, len(desc)
    # The description steers the caller to the robust claim_id-only flow.
    assert "claim_id" in desc
    assert "createUploadLink(mode=import)" in desc


# ---------------------------------------------------------------------------
# Structured logging never leaks the API key or the query body
# ---------------------------------------------------------------------------
def test_structured_log_excludes_key_and_query(client, auth, caplog):
    secret_query = "SUPERSECRETQUERYTOKEN revenue outlook"
    link = _import_packs(
        client, auth, [("qbu-log", "The revenue outlook is strong this year.")]
    )

    with caplog.at_level(logging.INFO, logger="safe_memory.packs"):
        resp = client.post(
            "/api/packs/query-by-upload",
            json={"claim_id": link["claim_id"], "query": secret_query},
            headers=auth,
        )
    assert resp.status_code == 200, resp.text

    text = caplog.text
    # The per-pack structured log line is emitted...
    assert "queryUploadedMemory claim_pack" in text
    # ...but it never contains the API key or the raw query body.
    assert TEST_KEY not in text
    assert "SUPERSECRETQUERYTOKEN" not in text
