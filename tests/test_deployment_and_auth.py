"""Deployment hardening: API-key auth, OpenAPI, upload, and safe query mode.

These tests run in deterministic fallback mode (Qwen disabled) via conftest.
"""

from __future__ import annotations

import importlib
import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.packs import build_pack_from_entries
from app.config import settings
from app.core import pack_io
from app.core.auth import API_KEY_HEADER
from app.main import app
from app.models.pack_schema import Classification

TEST_KEY = "test-secret-key-123"


@pytest.fixture
def client(safe_root):
    """A TestClient whose storage points at the per-test safe root."""
    return TestClient(app)


@pytest.fixture
def with_api_key(monkeypatch):
    """Enable API-key auth for the duration of a test."""
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return TEST_KEY


# ---------------------------------------------------------------------------
# Health / docs are always open
# ---------------------------------------------------------------------------
def test_health_requires_no_auth(client, with_api_key):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["auth_enabled"] is True


def test_openapi_loads_without_auth(client, with_api_key):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    # Key GPT-Action operation IDs are present.
    op_ids = {
        op.get("operationId")
        for path in schema["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }
    for expected in {
        "buildMemoryPack",
        "queryMemoryPack",
        "exportMemoryPack",
        "verifyMemoryPack",
        "getAgentCatalog",
        "runProjectWithMemory",
    }:
        assert expected in op_ids


def test_openapi_has_no_servers_by_default(client):
    """With no public base URL configured, the servers field is omitted/empty."""
    schema = client.get("/openapi.json").json()
    assert not schema.get("servers")


def test_openapi_declares_api_key_security(client, with_api_key):
    """GPT Actions needs a declared apiKey scheme to attach X-Safe-Memory-Key."""
    schema = client.get("/openapi.json").json()
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "SafeMemoryApiKey" in schemes, schemes
    scheme = schemes["SafeMemoryApiKey"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-Safe-Memory-Key"
    # Applied as a global requirement so clients send the header everywhere.
    assert {"SafeMemoryApiKey": []} in schema.get("security", [])


def test_all_operation_descriptions_within_gpt_limit():
    """GPT Actions rejects operation descriptions longer than 300 chars."""
    schema = app.openapi()
    offenders = {}
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            desc = op.get("description")
            if desc and len(desc) > 300:
                offenders[f"{method.upper()} {path} ({op.get('operationId')})"] = len(desc)
    assert not offenders, f"Descriptions over 300 chars: {offenders}"


def _operation_ids():
    schema = app.openapi()
    ids = set()
    for methods in schema["paths"].values():
        for op in methods.values():
            if isinstance(op, dict) and op.get("operationId"):
                ids.add(op["operationId"])
    return ids


def test_file_transfer_ops_hidden_from_openapi():
    """GPT Actions can't send binary/multipart, so file-transfer ops are hidden."""
    ids = _operation_ids()
    hidden = {
        "initUpload",
        "uploadContent",
        "buildMemoryPackFromUpload",
        "buildMemoryPackFromUploadRef",
        "getUploadLinkStatusByToken",
    }
    assert not (ids & hidden), f"These should be hidden from openapi.json: {ids & hidden}"


def test_json_friendly_ops_visible_in_openapi():
    """The JSON-only operations GPT/Claude use must stay in the schema."""
    ids = _operation_ids()
    expected = {
        "buildMemoryPack",
        "queryMemoryPack",
        "exportMemoryPack",
        "verifyMemoryPack",
        "getAgentCatalog",
        "runProjectWithMemory",
        "getJob",
        "getJobDownload",
        "cleanupJobs",
        "deleteJob",
        "createUploadLink",
        "getUploadLinkResult",
        "queryUploadedMemory",
    }
    missing = expected - ids
    assert not missing, f"Missing GPT-usable operations from openapi.json: {missing}"


def _resolve_schema(schema, node):
    """Follow a single $ref (if present) into components; else return node."""
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref:
        return node
    # e.g. "#/components/schemas/JobResponse"
    name = ref.split("/")[-1]
    return schema["components"]["schemas"][name]


def test_build_from_url_200_schema_exposes_download_url():
    """buildPackFromUrl must declare a non-empty 200 schema with download_url so
    ChatGPT Actions can surface the link (regression for response_model=None)."""
    schema = app.openapi()
    op = schema["paths"]["/api/packs/build-from-url"]["post"]
    assert op["operationId"] == "buildPackFromUrl"
    body_schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert body_schema, "200 response schema must not be empty {}"
    resolved = _resolve_schema(schema, body_schema)
    props = resolved.get("properties", {})
    assert "download_url" in props, f"download_url missing from 200 schema: {props.keys()}"


def test_success_responses_declare_download_url():
    """Every visible build/job success response exposes download_url as a field so
    GPT recognizes the link by name."""
    schema = app.openapi()
    for path, method in [
        ("/api/packs/build-from-url", "post"),
        ("/api/jobs/{job_id}", "get"),
    ]:
        op = schema["paths"][path][method]
        body_schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        resolved = _resolve_schema(schema, body_schema)
        props = resolved.get("properties", {})
        assert "download_url" in props, f"{path} {method} 200 lacks download_url"


def test_openapi_advertises_public_base_url(monkeypatch):
    """When SAFE_MEMORY_PUBLIC_BASE_URL is set, openapi.json exposes it for GPT Actions."""
    public_url = "https://demo-abc123.trycloudflare.com"
    monkeypatch.setattr(
        settings, "safe_memory_public_base_url", public_url, raising=False
    )
    reloaded = importlib.reload(main_module)
    try:
        schema = TestClient(reloaded.app).get("/openapi.json").json()
        assert schema["servers"][0]["url"] == public_url
    finally:
        # Restore the default (no servers) app for the rest of the suite.
        monkeypatch.setattr(
            settings, "safe_memory_public_base_url", "", raising=False
        )
        importlib.reload(main_module)


# ---------------------------------------------------------------------------
# /api/* auth enforcement
# ---------------------------------------------------------------------------
def test_api_rejects_missing_key(client, with_api_key):
    resp = client.get("/api/agents/tax-agent/catalog")
    assert resp.status_code == 401


def test_api_rejects_wrong_key(client, with_api_key):
    resp = client.get(
        "/api/agents/tax-agent/catalog",
        headers={API_KEY_HEADER: "wrong-key"},
    )
    assert resp.status_code == 401


def test_api_accepts_correct_key(client, with_api_key):
    resp = client.get(
        "/api/agents/tax-agent/catalog",
        headers={API_KEY_HEADER: TEST_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "tax-agent"


def test_api_open_in_dev_mode(client):
    """With no key configured, /api/* is reachable (dev mode)."""
    # conftest leaves safe_memory_api_key empty by default.
    resp = client.get("/api/agents/tax-agent/catalog")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------
def test_build_from_upload_txt(client):
    files = {"file": ("notes.txt", b"The invoice total is 500 USD.", "text/plain")}
    data = {
        "agent_id": "tax-agent",
        "pack_id": "upload-txt",
        "title": "Uploaded Text Pack",
    }
    resp = client.post("/api/packs/build-from-upload", data=data, files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pack_id"] == "upload-txt"
    assert body["entry_count"] >= 1
    assert isinstance(body["classification_counts"], dict)
    # Absolute/relative server paths are hidden unless debug=true.
    assert body["pack_path"] is None


def test_build_from_upload_xlsx(client):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "ContentText"])
    ws.append(["Invoice", "The quarterly invoice total is 500 USD."])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    files = {
        "file": (
            "data.xlsx",
            buffer.read(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    data = {
        "agent_id": "tax-agent",
        "pack_id": "upload-xlsx",
        "title": "Uploaded Excel Pack",
        "debug": "true",
    }
    resp = client.post("/api/packs/build-from-upload", data=data, files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry_count"] >= 1
    # debug=true reveals the server-relative path.
    assert body["pack_path"] is not None


def test_upload_rejects_unsupported_type(client):
    files = {"file": ("evil.exe", b"MZ...", "application/octet-stream")}
    data = {"agent_id": "a", "pack_id": "p", "title": "t"}
    resp = client.post("/api/packs/build-from-upload", data=data, files=files)
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# Safe public query mode: SECRET content is never exposed
# ---------------------------------------------------------------------------
def test_query_never_exposes_secret_content(client, safe_root):
    secret_text = "Root API key value is sk-supersecret-DONOTLEAK."
    specs = [
        {
            "text": "Public guidance about invoice totals and deadlines.",
            "classification": Classification.PUBLIC,
        },
        {"text": secret_text, "classification": Classification.SECRET},
    ]
    _, saved_path, _ = build_pack_from_entries(
        agent_id="tax-agent",
        pack_id="secret-safe-test",
        title="Secret Safe Test",
        entries=specs,
    )

    resp = client.post(
        "/api/packs/query",
        json={
            "agent_id": "tax-agent",
            "pack_path": pack_io.relpath_from_root(saved_path),
            "query": "What is the root API key value?",
            "top_k": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    serialized = resp.text
    assert "DONOTLEAK" not in serialized
    assert "sk-supersecret" not in serialized

    for hit in body["hits"]:
        assert hit["classification"] != "SECRET"
    assert "SECRET" not in body["answer"] or "DONOTLEAK" not in body["answer"]
    assert body["pack_id"] == "secret-safe-test"
    assert "confidence" in body
    # A warning notes the excluded SECRET entry.
    assert any("SECRET" in w for w in body["warnings"])
