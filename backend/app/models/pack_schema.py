"""Pydantic models for the Safe Memory Pack format and API contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


def _utcnow() -> str:
    """Return an ISO 8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


class Classification(str, Enum):
    """Confidentiality classification for a memory entry or pack."""

    PUBLIC = "PUBLIC"
    SHAREABLE = "SHAREABLE"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    SECRET = "SECRET"
    EPHEMERAL = "EPHEMERAL"


# ---------------------------------------------------------------------------
# Core pack models
# ---------------------------------------------------------------------------


class Policy(BaseModel):
    """Policy flags derived from a classification."""

    classification: Classification = Classification.INTERNAL
    exportable: bool = True
    shareable: bool = False
    usable_for_query: bool = True
    send_to_llm: bool = True
    redact_on_export: bool = False


class Provenance(BaseModel):
    """Where a memory entry came from."""

    source: str = "user_input"
    origin_pack_id: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    method: str = "memory_forge"


class Metadata(BaseModel):
    """Metadata attached to a single memory entry."""

    chunk_index: int = 0
    char_count: int = 0
    language: str = "auto"
    tags: List[str] = Field(default_factory=list)


class Entry(BaseModel):
    """A single memory entry inside a Safe Memory Pack.

    Bilingual fields are optional and default to ``None`` so that older packs
    containing only ``text`` remain fully backward compatible.
    """

    id: str
    text: str
    embedding: List[float] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    classification: Classification = Classification.INTERNAL
    policy: Policy = Field(default_factory=Policy)
    metadata: Metadata = Field(default_factory=Metadata)
    provenance: Provenance = Field(default_factory=Provenance)
    created_at: str = Field(default_factory=_utcnow)

    # Optional bilingual provenance / normalization fields.
    original_text: Optional[str] = None
    canonical_text: Optional[str] = None
    source_language: Optional[str] = None
    canonical_language: Optional[str] = None
    translation_note: Optional[str] = None

    def get_retrieval_text(self) -> str:
        """Return the text used for retrieval and answering.

        Prefers the English ``canonical_text`` when present, otherwise falls
        back to the backward-compatible ``text`` field.
        """
        return get_retrieval_text(self)


def get_retrieval_text(entry: "Entry") -> str:
    """Return the retrieval text for an entry (canonical English if present).

    Used for embedding generation, keyword generation, hybrid search, the Qwen
    answer context, and English-first demo output. Falls back to ``entry.text``
    for packs that predate the bilingual fields.
    """
    canonical = getattr(entry, "canonical_text", None)
    if canonical and canonical.strip():
        return canonical
    return entry.text


class LedgerBlock(BaseModel):
    """An append-only ledger block forming a sha256 hash chain."""

    id: str
    index: int
    entry_id: Optional[str] = None
    action: str = "append"
    entry_hash: str = ""
    previous_hash: str = ""
    hash: str = ""
    timestamp: str = Field(default_factory=_utcnow)


class Manifest(BaseModel):
    """Top-level descriptor of a Safe Memory Pack."""

    pack_id: str
    agent_id: str
    title: str
    version: str = "0.1.0"
    default_classification: Classification = Classification.INTERNAL
    embedding_model: str = ""
    chat_model: str = ""
    embedding_dim: int = 0
    entry_count: int = 0
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)
    format: str = "safe-memory-pack"
    spec_version: str = "0.1"


class SafeMemoryPack(BaseModel):
    """A complete, portable, policy-aware agent memory file."""

    manifest: Manifest
    entries: List[Entry] = Field(default_factory=list)
    ledger: List[LedgerBlock] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class BuildPackRequest(BaseModel):
    agent_id: str
    pack_id: str
    title: str
    source_text: str
    default_classification: Classification = Classification.INTERNAL
    delete_source_after_build: bool = True


class BuildPackResponse(BaseModel):
    pack_id: str
    pack_path: str
    entry_count: int
    audit_path: str
    classification: Classification


class QueryRequest(BaseModel):
    agent_id: str
    pack_path: Optional[str] = None
    pack_id: Optional[str] = None
    query: str
    top_k: int = 12
    allowed_classifications: Optional[List[Classification]] = None
    include_private: bool = False


class QueryHit(BaseModel):
    entry_id: str
    text: str
    score: float
    classification: Classification
    # Only populated when include_private=true; never for SECRET content.
    original_text: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    used_memory_ids: List[str]
    hits: List[QueryHit]
    classifications: List[Classification]
    fallback_used: bool = False
    pack_id: Optional[str] = None
    confidence: float = 0.0
    warnings: List[str] = Field(default_factory=list)


class QueryByUploadRequest(BaseModel):
    """Cross-search every pack uploaded via one import-mode one-time link.

    The caller only holds the ``claim_id`` returned by createUploadLink
    (mode=import); it never needs the ephemeral ``imp-`` agent_id/pack_id of the
    individual packs. The server resolves those from the claim.
    """

    claim_id: str
    query: str
    top_k: int = 12
    include_private: bool = False


class UploadedPackHit(BaseModel):
    """One pack that contributed retained hits to a query-by-upload answer."""

    agent_id: str
    pack_id: str
    hits: int = 0


class QueryByUploadResponse(BaseModel):
    answer: str
    used_packs: List[UploadedPackHit] = Field(default_factory=list)
    classifications: List[Classification] = Field(default_factory=list)
    confidence: float = 0.0
    fallback: bool = False


class AppendRequest(BaseModel):
    agent_id: str
    pack_path: str
    text: str
    source: str = "user_input"
    suggested_classification: Optional[Classification] = None


class AppendResponse(BaseModel):
    entry_id: str
    ledger_block_id: str
    version: str
    classification: Classification


class ExportRequest(BaseModel):
    agent_id: str
    pack_path: Optional[str] = None
    pack_id: Optional[str] = None
    export_name: str
    allowed_classifications: Optional[List[Classification]] = None
    remove_sources: bool = False
    redact_sensitive_text: bool = True


class ExportResponse(BaseModel):
    export_path: str
    included_count: int
    excluded_count: int
    warnings: List[str] = Field(default_factory=list)
    # Absolute HTTPS link (token-authorized) to fetch the exported pack, so
    # another agent can re-import it via importPackByRef. Relative if no public
    # base URL is configured.
    download_url: Optional[str] = None


class ImportByRefRequest(BaseModel):
    """Import a Safe Memory Pack from a remote HTTPS URL (a .smp.json file).

    File bytes cannot travel through the LLM, so a URL (plain text) is exchanged
    as the "currency" of the pack-exchange network. The server fetches, verifies,
    and imports the pack into ``agent_id``'s vault.
    """

    url: str
    agent_id: str
    # When omitted, the pack's manifest pack_id is used (or one is generated).
    pack_id: Optional[str] = None


class ImportFromUploadRefRequest(BaseModel):
    """Import a pre-built ``.smp.json`` pack from a staged upload.

    Mirrors :class:`ImportByRefRequest` but takes an ``upload_id`` (staged via
    ``/api/uploads``) instead of a URL, so a browser can import a local pack file
    without first hosting it at a public URL.
    """

    upload_id: str
    agent_id: str
    # When omitted, the pack's manifest pack_id is used (or one is generated).
    pack_id: Optional[str] = None


class ImportByRefResponse(BaseModel):
    pack_id: str
    entry_count: int
    classification_summary: dict = Field(default_factory=dict)
    verified: bool = False
    warnings: List[str] = Field(default_factory=list)


class VerifyRequest(BaseModel):
    pack_path: Optional[str] = None
    pack_id: Optional[str] = None
    agent_id: Optional[str] = None


class VerifyResponse(BaseModel):
    valid_hash_chain: bool
    manifest_present: bool
    entry_count: int
    ledger_count: int
    warnings: List[str] = Field(default_factory=list)


class UploadBuildResponse(BaseModel):
    pack_id: str
    entry_count: int
    classification_counts: dict = Field(default_factory=dict)
    # Retention / job metadata.
    job_id: Optional[str] = None
    status: Optional[str] = None
    retention_mode: Optional[str] = None
    expires_at: Optional[str] = None
    download_url: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    # Server-relative paths, only returned when debug=true.
    pack_path: Optional[str] = None
    audit_path: Optional[str] = None


class BuildFromUploadRefRequest(BaseModel):
    """Build a pack from a previously staged upload (small JSON, no bytes).

    The bytes were streamed to the staging channel via /api/uploads; here the
    LLM (or web page) references them by ``upload_id`` and processing runs
    asynchronously so large files never hit response-size / timeout limits.
    """

    upload_id: str
    agent_id: str
    pack_id: str
    title: str
    source_language: Optional[str] = None
    canonical_language: str = "en"
    default_classification: Classification = Classification.INTERNAL
    retention_mode: str = "process_and_return"
    debug_keep_upload: bool = False
    return_download_url: bool = True
    delete_source_after_processing: bool = True
    # Opt-in: translate Japanese rows to English canonical text. Default False
    # keeps ingestion fast (no Qwen chat calls); canonical_text = original text.
    translate: bool = False


class BuildRefAcceptedResponse(BaseModel):
    """Immediate response for an async ref build: poll GET /api/jobs/{job_id}."""

    job_id: str
    status: str = "PROCESSING"
    upload_id: Optional[str] = None


class BuildFromUrlRequest(BaseModel):
    """Build a pack from a raw file fetched from a public HTTPS share link.

    The server fetches the file (xlsx/csv/txt/md) from ``url``, stages the
    bytes, and runs the same async ingest pipeline as build-from-upload-ref.
    """

    url: str
    agent_id: str
    pack_id: str
    title: str
    source_language: Optional[str] = None
    canonical_language: str = "en"
    default_classification: Classification = Classification.INTERNAL
    retention_mode: str = "process_and_return"
    debug_keep_upload: bool = False
    return_download_url: bool = True
    delete_source_after_processing: bool = True
    # Opt-in: translate Japanese rows to English canonical text. Default False
    # keeps ingestion fast (no Qwen chat calls); canonical_text = original text.
    translate: bool = False


class CatalogItem(BaseModel):
    pack_id: str
    title: str
    version: str
    classification: Classification
    path: str
    entry_count: int
    updated_at: str


class CatalogResponse(BaseModel):
    agent_id: str
    packs: List[CatalogItem]
