"""Pack exchange network: importPackByRef + tokenized export download links."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import pack_import, pack_io
from app.core.auth import API_KEY_HEADER
from app.main import app

TEST_KEY = "exchange-key-abc"
PUBLIC_IP_URL = "https://8.8.8.8/pack.smp.json"


@pytest.fixture
def client(safe_root):
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return {API_KEY_HEADER: TEST_KEY}


def _mock_client(body, status=200, content_type="application/json"):
    def handler(request):
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10
    )


def _use_mock(monkeypatch, body, **kw):
    monkeypatch.setattr(pack_import, "_build_client", lambda: _mock_client(body, **kw))


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


# ---------------------------------------------------------------------------
# importPackByRef guards
# ---------------------------------------------------------------------------
def test_import_rejects_non_https(client, auth):
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": "http://8.8.8.8/pack.smp.json", "agent_id": "dst"},
        headers=auth,
    )
    assert resp.status_code == 400
    assert "https" in resp.json()["detail"].lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1", "[::1]"])
def test_import_blocks_ssrf_private(client, auth, host):
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": f"https://{host}/pack.smp.json", "agent_id": "dst"},
        headers=auth,
    )
    assert resp.status_code == 400
    assert "public" in resp.json()["detail"].lower()


def test_import_size_limit_413(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "safe_memory_max_import_mb", 1, raising=False)
    _use_mock(monkeypatch, b"x" * (2 * 1024 * 1024))
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst"},
        headers=auth,
    )
    assert resp.status_code == 413


def test_import_broken_json_rejected(client, auth, monkeypatch):
    _use_mock(monkeypatch, b"{not valid json")
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst"},
        headers=auth,
    )
    assert resp.status_code == 400


def test_import_non_pack_json_rejected(client, auth, monkeypatch):
    _use_mock(monkeypatch, b'{"hello": "world"}')
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst"},
        headers=auth,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# importPackByRef happy path + ledger verification
# ---------------------------------------------------------------------------
def test_import_happy_path(client, auth, monkeypatch):
    pack = _build_pack_object(client, auth)
    _use_mock(monkeypatch, _pack_bytes(pack))

    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst-agent", "pack_id": "shared-iq"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pack_id"] == "shared-iq"
    assert body["entry_count"] >= 1
    assert body["verified"] is True
    assert isinstance(body["classification_summary"], dict)

    catalog = client.get("/api/agents/dst-agent/catalog", headers=auth).json()
    assert "shared-iq" in {p["pack_id"] for p in catalog["packs"]}


def test_import_uses_manifest_pack_id_when_omitted(client, auth, monkeypatch):
    pack = _build_pack_object(client, auth, pack_id="manifest-id")
    _use_mock(monkeypatch, _pack_bytes(pack))

    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst-agent"},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["pack_id"] == "manifest-id"


def test_import_tampered_ledger_verified_false(client, auth, monkeypatch):
    pack = _build_pack_object(client, auth)
    pack.entries[0].text = pack.entries[0].text + " TAMPERED"
    _use_mock(monkeypatch, _pack_bytes(pack))

    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst-agent", "pack_id": "tampered"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verified"] is False
    assert any("ledger" in w.lower() for w in body["warnings"])


def test_import_tampered_strict_rejected(client, auth, monkeypatch):
    monkeypatch.setattr(
        settings, "safe_memory_import_require_valid_ledger", True, raising=False
    )
    pack = _build_pack_object(client, auth)
    pack.entries[0].text = pack.entries[0].text + " TAMPERED"
    _use_mock(monkeypatch, _pack_bytes(pack))

    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst-agent"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_import_requires_api_key(client, auth):
    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": PUBLIC_IP_URL, "agent_id": "dst"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# exportMemoryPack download_url + tokenized DL route
# ---------------------------------------------------------------------------
def _export(client, auth, agent="src-agent"):
    _build_pack_object(client, auth, agent=agent, pack_id="to-export")
    return client.post(
        "/api/packs/export",
        json={
            "agent_id": agent,
            "pack_id": "to-export",
            "export_name": "shared",
            "allowed_classifications": [
                "PUBLIC", "SHAREABLE", "INTERNAL", "CONFIDENTIAL", "SECRET", "EPHEMERAL",
            ],
        },
        headers=auth,
    )


def test_export_returns_download_url(client, auth):
    resp = _export(client, auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["download_url"]
    assert "/api/packs/dl/" in body["download_url"]


def test_export_download_url_absolute_with_public_base(client, auth, monkeypatch):
    monkeypatch.setattr(
        settings, "safe_memory_public_base_url", "https://smp.example.com", raising=False
    )
    body = _export(client, auth).json()
    assert body["download_url"].startswith("https://smp.example.com/api/packs/dl/")


def test_download_route_needs_no_api_key(client, auth):
    url = _export(client, auth).json()["download_url"]
    path = url[url.index("/api/packs/dl/"):]
    # No API key header -> still served (token-authorized).
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json()["manifest"]["pack_id"].startswith("to-export")


def test_download_unknown_token_404(client, auth):
    resp = client.get("/api/packs/dl/nonexistent-token")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Round trip: export -> download_url -> importPackByRef
# ---------------------------------------------------------------------------
def test_export_download_import_roundtrip(client, auth, monkeypatch):
    monkeypatch.setattr(
        settings, "safe_memory_public_base_url", "https://8.8.8.8", raising=False
    )
    download_url = _export(client, auth).json()["download_url"]
    assert download_url.startswith("https://8.8.8.8/api/packs/dl/")

    # Route the SSRF-safe fetch back into the app (no real network).
    def handler(request):
        inner = client.get(request.url.path)
        return httpx.Response(
            inner.status_code,
            content=inner.content,
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(
        pack_import,
        "_build_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10
        ),
    )

    resp = client.post(
        "/api/packs/import-by-ref",
        json={"url": download_url, "agent_id": "receiver", "pack_id": "received-iq"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pack_id"] == "received-iq"

    catalog = client.get("/api/agents/receiver/catalog", headers=auth).json()
    assert "received-iq" in {p["pack_id"] for p in catalog["packs"]}
