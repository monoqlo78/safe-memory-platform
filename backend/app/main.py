"""Safe Memory Platform FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.api import agents, files, jobs, packs, projects, upload_links, uploads, web
from app.config import settings
from app.core import pack_io
from app.core.auth import API_KEY_HEADER, ApiKeyAuthMiddleware, log_auth_mode_once
from app.mcp_server import MCPAuthMiddleware, build_mcp

logging.basicConfig(level=logging.INFO)

# GPT Actions requires an absolute servers[0].url in the OpenAPI schema. When a
# public base URL is configured, advertise it; otherwise keep default behavior
# (no servers field).
_fastapi_kwargs = {}
_public_base_url = (settings.safe_memory_public_base_url or "").strip()
if _public_base_url:
    _fastapi_kwargs["servers"] = [{"url": _public_base_url}]

# The FastAPI app serves the REST API (/api/*, /health, /docs, /openapi.json).
# It is mounted at "/" under an outer Starlette router (see bottom of file) so
# the native MCP endpoint at /mcp can bypass the BaseHTTPMiddleware auth layer,
# which is known to break SSE/streaming.
api_app = FastAPI(
    title="Safe Memory Platform",
    version="0.1.0",
    description="Portable, policy-aware memory packs for AI agents powered by Qwen Cloud.",
    **_fastapi_kwargs,
)

# API key auth is added first so it runs after CORS on the response path and
# short-circuits unauthorized /api/* requests early on the request path.
api_app.add_middleware(ApiKeyAuthMiddleware)

api_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_app.include_router(packs.router)
api_app.include_router(agents.router)
api_app.include_router(projects.router)
api_app.include_router(jobs.router)
api_app.include_router(uploads.router)
api_app.include_router(upload_links.router)
api_app.include_router(files.router)
api_app.include_router(web.router)


def _custom_openapi():
    """OpenAPI schema that declares the shared-key auth.

    Auth is enforced by middleware (not per-route dependencies), so FastAPI
    would otherwise emit a schema with no security. GPT Actions then treats the
    API as unauthenticated and never attaches the key, producing 403s. Declaring
    an ``apiKey`` scheme makes GPT Actions (and clones) auto-prompt for and send
    the ``X-Safe-Memory-Key`` header.
    """
    if api_app.openapi_schema:
        return api_app.openapi_schema
    schema = get_openapi(
        title=api_app.title,
        version=api_app.version,
        description=api_app.description,
        routes=api_app.routes,
        servers=api_app.servers or None,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["SafeMemoryApiKey"] = {
        "type": "apiKey",
        "in": "header",
        "name": API_KEY_HEADER,
        "description": (
            "Shared API key. Send it in the "
            f"{API_KEY_HEADER} header on every /api/* request."
        ),
    }
    # Apply the requirement globally; open endpoints (health/docs) ignore it.
    schema["security"] = [{"SafeMemoryApiKey": []}]
    api_app.openapi_schema = schema
    return schema


api_app.openapi = _custom_openapi


@api_app.get("/health")
def health():
    """Health check. Reports model names but never secret values."""
    return {
        "status": "ok",
        "service": "safe-memory-platform",
        "version": "0.1.0",
        "qwen_enabled": settings.has_qwen_credentials,
        "auth_enabled": settings.auth_enabled,
        "oss_enabled": settings.oss_ready,
        "chat_model": settings.qwen_chat_model,
        "embedding_model": settings.qwen_embedding_model,
        "base_url": settings.qwen_base_url,
        "safe_memory_root": settings.safe_memory_root,
    }


# --------------------------------------------------------------------------- MCP
# Native MCP server (Streamable HTTP) served at /mcp on the same app. Tools call
# the existing REST handlers in-process. json_response + stateless_http mean tool
# calls return unary JSON (no long-lived SSE), and /mcp gets its own pure-ASGI
# auth wrapper instead of the BaseHTTPMiddleware used for /api/*.
mcp = build_mcp()
mcp_asgi_app = mcp.streamable_http_app()
_mcp_entrypoint = MCPAuthMiddleware(mcp_asgi_app)


class _RootDispatch:
    """Pure-ASGI dispatcher: /mcp[/] -> MCP app, everything else -> FastAPI app.

    Using a dispatcher (instead of a Starlette ``Mount``) means the exact path
    ``/mcp`` — what MCP clients POST to, no trailing slash — reaches the MCP app
    directly (no 307 redirect), and the MCP app never passes through the
    FastAPI ``BaseHTTPMiddleware`` stack, which is known to break SSE/streaming.
    The combined lifespan runs the app's startup chores and keeps the MCP
    session manager alive for the process lifetime.
    """

    def __init__(self, mcp_app, api, mcp_server):
        self._mcp_app = mcp_app
        self._api = api
        self._mcp = mcp_server

    async def _lifespan(self, scope, receive, send):
        async with self._mcp.session_manager.run():
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    pack_io.get_root()
                    log_auth_mode_once()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            # MCP is a single JSON-RPC endpoint mounted at /mcp inside its own
            # app (a Starlette Mount that expects the trailing slash). Normalize
            # so both /mcp and /mcp/ reach the handler without a 307 redirect.
            scope = dict(scope)
            scope["path"] = "/mcp/"
            await self._mcp_app(scope, receive, send)
            return
        await self._api(scope, receive, send)


app = _RootDispatch(_mcp_entrypoint, api_app, mcp)

# Expose the FastAPI OpenAPI generator on the outer app for tooling/tests.
app.openapi = _custom_openapi
