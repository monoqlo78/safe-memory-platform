"""Web import path: register a pre-built .smp.json pack via a staged upload.

Covers POST /api/packs/import-from-upload-ref (hidden from OpenAPI, key-guarded)
and the GET /import browser page. Binary pack files cannot travel through GPT
Actions, so the browser stages the file (init -> PUT) then imports it by
upload_id; the pack becomes queryable by pack_id in the agent's catalog.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import pack_io
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "import-upload-key-xyz"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


def _build_pack_object(client, auth, agent="src-agent", pack_id="orig"):
    resp = client.post(
        "/api/packs/build",
        json={
            "agent_id": agent,
            "pack_id": pack_id,
            "title": "Original Pack",
            "source_text": "The quarterly revenue grew. Taxes were filed on time.",
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return pack_io.load_pack(resp.json()["pack_path"])


def _pack_bytes(pack):
    return json.dumps(pack.model_dump(mode="json")).encode("utf-8")


def _stage(client, auth, data, filename="pack.smp.json"):
    """Stage raw bytes via init + PUT, returning the upload_id."""
    init = client.post(
        "/api/uploads/init",
        json={"filename": filename, "content_type": "application/json", "size": len(data)},
        headers=auth,
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


def _import(client, auth, upload_id, agent_id="dst-agent", pack_id=None):
    payload = {"upload_id": upload_id, "agent_id": agent_id}
    if pack_id is not None:
        payload["pack_id"] = pack_id
    return client.post("/api/packs/import-from-upload-ref", json=payload, headers=auth)


# ---------------------------------------------------------------------------
# Happy path: staged pack -> imported -> queryable by pack_id
# ---------------------------------------------------------------------------
def test_import_from_upload_registers_pack(client, auth):
    pack = _build_pack_object(client, auth)
    upload_id = _stage(client, auth, _pack_bytes(pack))

    resp = _import(client, auth, upload_id, agent_id="dst-agent", pack_id="shared-iq")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pack_id"] == "shared-iq"
    assert body["entry_count"] >= 1
    assert body["verified"] is True
    assert isinstance(body["classification_summary"], dict)

    catalog = client.get("/api/agents/dst-agent/catalog", headers=auth).json()
    assert "shared-iq" in {p["pack_id"] for p in catalog["packs"]}

    # The imported pack is queryable by pack_id.
    q = client.post(
        "/api/packs/query",
        json={"agent_id": "dst-agent", "pack_id": "shared-iq", "query": "revenue", "top_k": 3},
        headers=auth,
    )
    assert q.status_code == 200, q.text
    assert q.json()["pack_id"] == "shared-iq"


def test_import_from_upload_uses_manifest_pack_id_when_omitted(client, auth):
    pack = _build_pack_object(client, auth, pack_id="manifest-id")
    upload_id = _stage(client, auth, _pack_bytes(pack))

    resp = _import(client, auth, upload_id, agent_id="dst-agent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["pack_id"] == "manifest-id"


def test_import_from_upload_consumes_staged_bytes(client, auth):
    """Staged bytes are single-use: a second import of the same id 404s."""
    pack = _build_pack_object(client, auth)
    upload_id = _stage(client, auth, _pack_bytes(pack))

    first = _import(client, auth, upload_id, pack_id="one")
    assert first.status_code == 200, first.text

    second = _import(client, auth, upload_id, pack_id="two")
    assert second.status_code == 404


def test_import_from_upload_tampered_ledger_verified_false(client, auth):
    pack = _build_pack_object(client, auth)
    pack.entries[0].text = pack.entries[0].text + " TAMPERED"
    upload_id = _stage(client, auth, _pack_bytes(pack))

    resp = _import(client, auth, upload_id, pack_id="tampered")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verified"] is False
    assert any("ledger" in w.lower() for w in body["warnings"])


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_import_from_upload_broken_json_rejected(client, auth):
    upload_id = _stage(client, auth, b"{not valid json")
    resp = _import(client, auth, upload_id, pack_id="broken")
    assert resp.status_code == 400


def test_import_from_upload_non_pack_json_rejected(client, auth):
    upload_id = _stage(client, auth, b'{"hello": "world"}')
    resp = _import(client, auth, upload_id, pack_id="nonpack")
    assert resp.status_code == 400


def test_import_from_upload_unknown_upload_id_404(client, auth):
    resp = _import(client, auth, "nonexistent-upload-id", pack_id="missing")
    assert resp.status_code == 404


def test_import_from_upload_requires_api_key(client, auth):
    pack = _build_pack_object(client, auth)
    upload_id = _stage(client, auth, _pack_bytes(pack))
    # No API key header on the import call.
    resp = client.post(
        "/api/packs/import-from-upload-ref",
        json={"upload_id": upload_id, "agent_id": "dst-agent"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OpenAPI visibility + web page
# ---------------------------------------------------------------------------
def test_import_from_upload_hidden_from_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "/api/packs/import-from-upload-ref" not in spec["paths"]
    op_ids = {
        op.get("operationId")
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }
    assert "importMemoryPackFromUploadRef" not in op_ids
    # The URL-based import stays visible.
    assert "importPackByRef" in op_ids


def test_import_page_served(client):
    resp = client.get("/import")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Import Packs" in body
    assert "/api/packs/import-from-upload-ref" in body
    assert "/api/uploads/init" in body
