"""Native MCP server for the Safe Memory Platform.

Exposes the same capabilities as the existing GPT Actions as MCP tools so Claude
(and any MCP client) can drive the platform over **Streamable HTTP** mounted on
the same FastAPI app at ``/mcp``. Tools call the existing router handlers
**in-process** (no self-HTTP, no duplicated logic); request models are built and
handed to the same functions that serve the REST API.

Auth is handled by :class:`MCPAuthMiddleware` (a pure-ASGI wrapper) rather than
the app's ``BaseHTTPMiddleware`` key check, because ``BaseHTTPMiddleware`` is
known to break SSE/streaming. The wrapper accepts either the
``X-Safe-Memory-Key`` header or an ``Authorization: Bearer <key>`` token (Claude
remote connectors send the key as a Bearer token).

Byte-transfer endpoints (uploads/init, presign, raw PUT) are intentionally NOT
exposed here — MCP is a JSON control plane. ``download_url`` values remain the
signature-free stable ``/api/packs/dl/{token}`` links.
"""

import logging
from typing import Any, List, Optional

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api import agents as agents_api
from app.api import jobs as jobs_api
from app.api import packs as packs_api
from app.api import projects as projects_api
from app.api import upload_links as upload_links_api
from app.config import settings
from app.core.auth import API_KEY_HEADER, _constant_time_equals
from app.models.pack_schema import (
    AppendRequest,
    BuildFromUrlRequest,
    BuildPackRequest,
    ExportRequest,
    ImportByRefRequest,
    QueryRequest,
    VerifyRequest,
)
from app.models.project_schema import ProjectRunRequest
from app.models.upload_link_schema import CreateUploadLinkRequest

logger = logging.getLogger("safe_memory.mcp")

APP_VERSION = "0.1.0"

_MCP_INSTRUCTIONS = (
    "Safe Memory Platform: build and query portable, policy-aware memory packs "
    "from files or share links. SECRET/CONFIDENTIAL content is never exported or "
    "sent to external LLMs. download_url is a signature-free stable link "
    "(/api/packs/dl/{token}) that streams the pack or redirects to a fresh signed "
    "URL. Build tools are async: they return a job_id; poll get_job until "
    "COMPLETED, then reuse download_url via import_pack_by_ref.\n\n"
    "Security operating rules (always follow):\n"
    "1. Never read a .smp.json (or other memory file) the user pasted or "
    "attached and answer from it directly. Always process memory through this "
    "platform so policy, SECRET filtering, and retrieval apply.\n"
    "2. To use a user's memory packs: call create_upload_link with mode=import, "
    "show the returned upload_url to the user, then poll get_upload_link_result "
    "with claim_id until COMPLETED. Query ONLY the agent_id/pack_id values it "
    "returns in imported[], using query_memory_pack.\n"
    "3. Never discover or use packs the user did not upload in this session. Do "
    "not guess agent_id or pack_id. Uploaded packs are private to the link, "
    "temporary, and auto-deleted when the link expires."
)



def _dump(obj: Any) -> Any:
    """Convert a Pydantic response (or plain value) to JSON-safe data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


async def _call(handler, *args) -> Any:
    """Run a sync handler in a threadpool, mapping HTTPException to a tool error."""
    try:
        result = await run_in_threadpool(handler, *args)
    except HTTPException as exc:
        raise ValueError(f"HTTP {exc.status_code}: {exc.detail}") from None
    return _dump(result)


def _fake_request() -> Request:
    """Minimal Request so handlers that read base_url work outside of HTTP."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "server": ("localhost", 443),
        "client": ("127.0.0.1", 0),
    }
    return Request(scope)


def build_mcp():
    """Build and return the configured FastMCP server (tools registered)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="safe-memory-platform",
        instructions=_MCP_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
    )

    # ------------------------------------------------------------------ health
    @mcp.tool()
    async def health() -> dict:
        """Report service status and configured model names (no secrets)."""
        return {
            "status": "ok",
            "service": "safe-memory-platform",
            "version": APP_VERSION,
            "qwen_enabled": settings.has_qwen_credentials,
            "auth_enabled": settings.auth_enabled,
            "oss_enabled": settings.oss_ready,
            "chat_model": settings.qwen_chat_model,
            "embedding_model": settings.qwen_embedding_model,
        }

    # ------------------------------------------------------- build_pack_from_url
    @mcp.tool()
    async def build_pack_from_url(
        url: str,
        agent_id: str,
        pack_id: str,
        title: str,
        source_language: Optional[str] = None,
        canonical_language: str = "en",
        default_classification: str = "INTERNAL",
        retention_mode: str = "process_and_return",
        translate: bool = False,
    ) -> dict:
        """Build a Safe Memory Pack from a public HTTPS share link (xlsx/csv/txt/md/
        docx/pptx/pdf/images; scanned PDFs/images are OCR'd).

        Bounded-synchronous: waits briefly for the build. Fast builds return the
        finished job (status COMPLETED with a signature-free download_url) in one
        call; slow builds return {job_id, status:"PROCESSING"} -- poll get_job
        until COMPLETED, then read download_url. translate defaults False
        (canonical=original).
        """
        req = BuildFromUrlRequest(
            url=url,
            agent_id=agent_id,
            pack_id=pack_id,
            title=title,
            source_language=source_language,
            canonical_language=canonical_language,
            default_classification=default_classification,
            retention_mode=retention_mode,
            translate=translate,
        )
        try:
            resp = await packs_api._build_pack_from_url_impl(req)
        except HTTPException as exc:
            raise ValueError(f"HTTP {exc.status_code}: {exc.detail}") from None
        return _dump(resp)

    # ---------------------------------------------------------------- get_job
    @mcp.tool()
    async def get_job(job_id: str) -> dict:
        """Get a build job's status, entry_count, classification_counts, and the
        signature-free download_url when COMPLETED."""
        return await _call(jobs_api.get_job, job_id, False)

    # ------------------------------------------------------- query_memory_pack
    @mcp.tool()
    async def query_memory_pack(
        agent_id: str,
        query: str,
        pack_id: Optional[str] = None,
        pack_path: Optional[str] = None,
        top_k: int = 12,
        allowed_classifications: Optional[List[str]] = None,
        include_private: bool = False,
    ) -> dict:
        """Query a pack's memory. Returns answer, used_memory_ids, hits,
        classifications, confidence, and warnings. SECRET content is never
        returned; original_text only when include_private and authorized.
        top_k defaults to 12."""
        req = QueryRequest(
            agent_id=agent_id,
            query=query,
            pack_id=pack_id,
            pack_path=pack_path,
            top_k=top_k,
            allowed_classifications=allowed_classifications,
            include_private=include_private,
        )
        return await _call(packs_api.query_pack, req)

    # ------------------------------------------------------- import_pack_by_ref
    @mcp.tool()
    async def import_pack_by_ref(
        url: str, agent_id: str, pack_id: Optional[str] = None
    ) -> dict:
        """Import a Safe Memory Pack (.smp.json) from an HTTPS URL into an agent's
        vault. Returns pack_id, entry_count, classification_summary, verified."""
        req = ImportByRefRequest(url=url, agent_id=agent_id, pack_id=pack_id)
        return await _call(packs_api.import_pack_by_ref, req)

    # -------------------------------------------------------- export_memory_pack
    @mcp.tool()
    async def export_memory_pack(
        agent_id: str,
        export_name: str,
        pack_id: Optional[str] = None,
        pack_path: Optional[str] = None,
        allowed_classifications: Optional[List[str]] = None,
        remove_sources: bool = False,
        redact_sensitive_text: bool = True,
    ) -> dict:
        """Export a shareable pack (SECRET excluded, sensitive text redacted by
        default). Returns a signature-free download_url for import_pack_by_ref."""
        req = ExportRequest(
            agent_id=agent_id,
            export_name=export_name,
            pack_id=pack_id,
            pack_path=pack_path,
            allowed_classifications=allowed_classifications,
            remove_sources=remove_sources,
            redact_sensitive_text=redact_sensitive_text,
        )
        return await _call(packs_api.export_pack, req)

    # -------------------------------------------------------- verify_memory_pack
    @mcp.tool()
    async def verify_memory_pack(
        pack_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        pack_path: Optional[str] = None,
    ) -> dict:
        """Verify a pack's manifest and append-only ledger hash chain so tampering
        is detectable. Identify with pack_id+agent_id (preferred) or pack_path."""
        req = VerifyRequest(pack_id=pack_id, agent_id=agent_id, pack_path=pack_path)
        return await _call(packs_api.verify_pack, req)

    # --------------------------------------------------------- get_agent_catalog
    @mcp.tool()
    async def get_agent_catalog(agent_id: str) -> dict:
        """List the vaulted packs available to an agent (pack_id, title, version,
        classification, entry_count)."""
        return await _call(agents_api.get_agent_catalog, agent_id)

    # --------------------------------------------------------- create_upload_link
    @mcp.tool()
    async def create_upload_link(
        agent_id: str = "shared",
        pack_id: Optional[str] = None,
        title: str = "Uploaded via one-time link",
        source_language: Optional[str] = None,
        canonical_language: str = "en",
        retention_mode: str = "process_and_return",
        classification: str = "internal",
        expires_in_seconds: int = 1800,
        max_uses: int = 1,
        mode: str = "build",
    ) -> dict:
        """Mint a single-use, keyless upload URL a person can open to drop
        files. Give the returned upload_url to the user; do not read their
        pasted/attached files. Use mode=import so they upload finished
        .smp.json packs into a private, temporary space. Returns {upload_url,
        claim_id, expires_at, mode}. Poll get_upload_link_result with claim_id."""
        req = CreateUploadLinkRequest(
            agent_id=agent_id,
            pack_id=pack_id,
            title=title,
            source_language=source_language,
            canonical_language=canonical_language,
            retention_mode=retention_mode,
            classification=classification,
            expires_in_seconds=expires_in_seconds,
            max_uses=max_uses,
            mode=mode,
        )
        try:
            resp = await run_in_threadpool(
                upload_links_api.create_upload_link, req, _fake_request()
            )
        except HTTPException as exc:
            raise ValueError(f"HTTP {exc.status_code}: {exc.detail}") from None
        return _dump(resp)

    # ----------------------------------------------------- get_upload_link_result
    @mcp.tool()
    async def get_upload_link_result(claim_id: str) -> dict:
        """Poll a one-time upload link by claim_id until COMPLETED. Build mode
        returns download_url, pack_id, entry_count. Import mode returns
        imported[] with the agent_id and pack_id of each uploaded pack; query
        ONLY those with query_memory_pack (do not use any other packs)."""
        return await _call(upload_links_api.get_upload_link_result, claim_id)

    # ------------------------------------------------------------ build_memory_pack
    @mcp.tool()
    async def build_memory_pack(
        agent_id: str,
        pack_id: str,
        title: str,
        source_text: str,
        default_classification: str = "INTERNAL",
        delete_source_after_build: bool = True,
    ) -> dict:
        """Build a Safe Memory Pack synchronously from inline source_text. Returns
        pack_id, pack_path, entry_count, classification."""
        req = BuildPackRequest(
            agent_id=agent_id,
            pack_id=pack_id,
            title=title,
            source_text=source_text,
            default_classification=default_classification,
            delete_source_after_build=delete_source_after_build,
        )
        return await _call(packs_api.build_pack, req)

    # ------------------------------------------------------------------- append
    @mcp.tool()
    async def append(
        agent_id: str,
        pack_path: str,
        text: str,
        source: str = "user_input",
        suggested_classification: Optional[str] = None,
    ) -> dict:
        """Append a new memory entry to an existing pack (extends the hash-chained
        ledger). Returns entry_id, ledger_block_id, version, classification."""
        req = AppendRequest(
            agent_id=agent_id,
            pack_path=pack_path,
            text=text,
            source=source,
            suggested_classification=suggested_classification,
        )
        return await _call(packs_api.append_entry, req)

    # ------------------------------------------------------ run_project_with_memory
    @mcp.tool()
    async def run_project_with_memory(
        project_id: str,
        agent_id: str,
        task: str,
        pack_paths: Optional[List[str]] = None,
        top_k: int = 12,
    ) -> dict:
        """Run a task using an agent's memory packs as context (SECRET is never
        sent to the LLM). Returns output, used_memory_ids, used_memories,
        suggested_new_memories. top_k defaults to 12."""
        req = ProjectRunRequest(
            project_id=project_id,
            agent_id=agent_id,
            task=task,
            pack_paths=pack_paths or [],
            top_k=top_k,
        )
        return await _call(projects_api.run_project, req)

    return mcp


class MCPAuthMiddleware:
    """Pure-ASGI auth wrapper for the mounted MCP app.

    Accepts ``X-Safe-Memory-Key`` OR ``Authorization: Bearer <key>``. Rejects with
    401 *before* touching the inner app, and otherwise forwards the ASGI messages
    untouched so streaming responses are never buffered (unlike
    ``BaseHTTPMiddleware``). Non-HTTP scopes pass straight through.
    """

    def __init__(self, app) -> None:
        self.app = app

    def _authorized(self, scope) -> bool:
        headers = Headers(scope=scope)
        provided = headers.get(API_KEY_HEADER)
        if not provided:
            auth = headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                provided = auth[7:].strip()
        return bool(provided) and _constant_time_equals(
            provided, settings.safe_memory_api_key
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Dev mode (no key configured): allow all, mirroring the REST behavior.
        if not settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content={
                "detail": (
                    f"Missing or invalid {API_KEY_HEADER} header or Bearer token."
                )
            },
        )
        await response(scope, receive, send)
