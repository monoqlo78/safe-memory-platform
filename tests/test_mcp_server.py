"""Tests for the native MCP server mounted at /mcp.

Covers: pure-ASGI auth (X-Safe-Memory-Key header OR Authorization: Bearer, plus
dev-mode passthrough), that the MCP endpoint enumerates the expected tools and
can execute one end to end, and regression guards that mounting MCP does not
leak /mcp into the GPT Actions OpenAPI schema or route it through the
BaseHTTPMiddleware key check (which breaks SSE/streaming).
"""

import asyncio
import inspect
import json

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.core.auth import API_KEY_HEADER
from app.main import app
from app.mcp_server import MCPAuthMiddleware, build_mcp

TEST_KEY = "mcp-test-key-0123456789"

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_EXPECTED_TOOLS = {
    "health",
    "build_pack_from_url",
    "get_job",
    "query_memory_pack",
    "import_pack_by_ref",
    "export_memory_pack",
    "verify_memory_pack",
    "get_agent_catalog",
    "create_upload_link",
    "get_upload_link_result",
    "build_memory_pack",
    "append",
    "run_project_with_memory",
}


def _init_payload():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }


@pytest.fixture(scope="module")
def mcp_http_client():
    """A single lifespan-managed client.

    The MCP StreamableHTTPSessionManager may only be run once per instance, and
    the module-level app holds a single instance, so the lifespan (which starts
    the session manager) must be entered exactly once for the HTTP-level tests.
    """
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_key(monkeypatch):
    """Enable API-key auth for the duration of a test."""
    monkeypatch.setattr(settings, "safe_memory_api_key", TEST_KEY, raising=False)
    return TEST_KEY


# ---------------------------------------------------------------------------
# Auth: /mcp requires a valid key via header OR Bearer token
# ---------------------------------------------------------------------------
def test_mcp_rejects_missing_key(mcp_http_client, api_key):
    resp = mcp_http_client.post("/mcp", json=_init_payload(), headers=_MCP_HEADERS)
    assert resp.status_code == 401


def test_mcp_rejects_wrong_key(mcp_http_client, api_key):
    resp = mcp_http_client.post(
        "/mcp",
        json=_init_payload(),
        headers={**_MCP_HEADERS, API_KEY_HEADER: "not-the-key"},
    )
    assert resp.status_code == 401


def test_mcp_accepts_header_key(mcp_http_client, api_key):
    resp = mcp_http_client.post(
        "/mcp",
        json=_init_payload(),
        headers={**_MCP_HEADERS, API_KEY_HEADER: TEST_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["serverInfo"]["name"] == "safe-memory-platform"


def test_mcp_accepts_bearer_key(mcp_http_client, api_key):
    resp = mcp_http_client.post(
        "/mcp",
        json=_init_payload(),
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {TEST_KEY}"},
    )
    assert resp.status_code == 200


def test_mcp_dev_mode_passthrough(mcp_http_client, monkeypatch):
    """With no key configured (dev mode), /mcp is open like the REST API."""
    monkeypatch.setattr(settings, "safe_memory_api_key", "", raising=False)
    resp = mcp_http_client.post("/mcp", json=_init_payload(), headers=_MCP_HEADERS)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# tools/list and tools/call over an in-memory MCP session (hermetic)
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _list_tools():
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = build_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        result = await client.list_tools()
        return [t.name for t in result.tools]


async def _call_tool(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = build_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def test_tools_list_enumerates_expected_tools():
    names = set(_run(_list_tools()))
    assert _EXPECTED_TOOLS.issubset(names), _EXPECTED_TOOLS - names


def test_tools_call_health():
    result = _run(_call_tool("health", {}))
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "ok"
    assert payload["service"] == "safe-memory-platform"


def test_tools_call_get_agent_catalog(safe_root):
    result = _run(_call_tool("get_agent_catalog", {"agent_id": "nobody-agent"}))
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["agent_id"] == "nobody-agent"
    assert payload["packs"] == []


# ---------------------------------------------------------------------------
# Regression guards
# ---------------------------------------------------------------------------
def test_mcp_absent_from_openapi(mcp_http_client, api_key):
    schema = mcp_http_client.get("/openapi.json").json()
    assert all(not path.startswith("/mcp") for path in schema["paths"])
    op_ids = {
        op.get("operationId")
        for path in schema["paths"].values()
        for op in path.values()
        if isinstance(op, dict)
    }
    # None of the MCP tool names should surface as GPT Action operationIds.
    assert not (_EXPECTED_TOOLS & op_ids)


def test_mcp_auth_is_pure_asgi():
    """The /mcp auth wrapper must be pure-ASGI, never BaseHTTPMiddleware.

    BaseHTTPMiddleware is known to buffer/break SSE and streaming responses;
    the MCP Streamable HTTP transport must not be wrapped by it.
    """
    assert not issubclass(MCPAuthMiddleware, BaseHTTPMiddleware)
    params = list(inspect.signature(MCPAuthMiddleware.__call__).parameters)
    assert params == ["self", "scope", "receive", "send"]


def test_health_still_open(mcp_http_client, api_key):
    """Mounting MCP must not regress the keyless /health endpoint."""
    resp = mcp_http_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
