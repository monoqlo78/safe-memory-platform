"""Application configuration.

Loads settings from environment variables using pydantic-settings.
Never hardcode secrets here; they are read from the environment / .env file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Safe Memory Platform backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Qwen Cloud (OpenAI compatible) configuration.
    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )
    qwen_chat_model: str = Field(default="qwen-plus", alias="QWEN_CHAT_MODEL")
    qwen_embedding_model: str = Field(
        default="text-embedding-v4", alias="QWEN_EMBEDDING_MODEL"
    )

    # Per-request timeout (seconds) for Qwen chat/embedding calls. Without an
    # explicit bound the OpenAI SDK waits up to ~600s per attempt, so a stalled
    # upstream can make a large batched import appear to hang forever. When a
    # call times out we fall back to deterministic local behaviour, so the job
    # always finishes instead of blocking.
    qwen_timeout_seconds: float = Field(
        default=30.0, alias="QWEN_TIMEOUT_SECONDS"
    )
    # Retries the OpenAI SDK performs per failed Qwen call. Kept low so a bad
    # upstream degrades to the fallback quickly rather than multiplying timeouts.
    qwen_max_retries: int = Field(default=1, alias="QWEN_MAX_RETRIES")

    # Local filesystem storage root. All pack IO is confined to this directory.
    safe_memory_root: str = Field(default="/app/SafeMemory", alias="SAFE_MEMORY_ROOT")

    app_env: str = Field(default="local", alias="APP_ENV")

    # Minutes a temporary (session / process_and_return) pack lives before it is
    # eligible for cleanup. Does not affect server_vault packs.
    safe_memory_temp_ttl_minutes: int = Field(
        default=60, alias="SAFE_MEMORY_TEMP_TTL_MINUTES"
    )

    # Simple shared API key for gating /api/* routes. Empty => dev mode (open).
    safe_memory_api_key: str = Field(default="", alias="SAFE_MEMORY_API_KEY")

    # Number of rows sent to Qwen in a single batched translation call. Larger
    # batches are much faster for big Excel imports; too large may hit context
    # limits. Falls back to per-item translation if a batch response misaligns.
    translation_batch_size: int = Field(
        default=20, alias="SAFE_MEMORY_TRANSLATION_BATCH_SIZE"
    )

    # Max tokens for the query answer generation. Lower => faster query responses
    # (helps stay under GPT Actions / Claude timeouts). Does not affect other
    # chat_completion callers.
    answer_max_tokens: int = Field(default=400, alias="SAFE_MEMORY_ANSWER_MAX_TOKENS")

    # Maximum size (MB) accepted by the staging upload channel used for large
    # files (LLM-safe direct upload). Streamed to disk; larger uploads get 413.
    safe_memory_max_upload_mb: int = Field(
        default=50, alias="SAFE_MEMORY_MAX_UPLOAD_MB"
    )

    # Minutes a staged upload lives before it is eligible for cleanup if never
    # consumed by a build request.
    safe_memory_upload_ttl_minutes: int = Field(
        default=60, alias="SAFE_MEMORY_UPLOAD_TTL_MINUTES"
    )

    # Storage backend for staged uploads. "local" stages under SAFE_MEMORY_ROOT.
    # "oss" is reserved for a future Alibaba OSS backend.
    safe_memory_storage_backend: str = Field(
        default="local", alias="SAFE_MEMORY_STORAGE_BACKEND"
    )

    # Comma-separated list of allowed CORS origins.
    safe_memory_cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8787",
        alias="SAFE_MEMORY_CORS_ORIGINS",
    )

    # Absolute public HTTPS base URL (e.g. a Cloudflare tunnel) advertised in the
    # generated openapi.json `servers` field. Required by GPT Actions. Empty =>
    # no servers field (default FastAPI behavior).
    safe_memory_public_base_url: str = Field(
        default="", alias="SAFE_MEMORY_PUBLIC_BASE_URL"
    )

    # Maximum size (MB) accepted when importing a pack from a remote URL
    # (importPackByRef). Streamed with a hard cap; larger downloads get 413.
    safe_memory_max_import_mb: int = Field(
        default=25, alias="SAFE_MEMORY_MAX_IMPORT_MB"
    )

    # When True, importPackByRef rejects packs whose ledger hash chain is invalid.
    # When False (default), such packs are imported with verified=false + warning.
    safe_memory_import_require_valid_ledger: bool = Field(
        default=False, alias="SAFE_MEMORY_IMPORT_REQUIRE_VALID_LEDGER"
    )

    # Timeout (seconds) for server-side fetches of remote files/packs by URL
    # (buildPackFromUrl / importPackByRef).
    safe_memory_url_fetch_timeout_seconds: int = Field(
        default=30, alias="SAFE_MEMORY_URL_FETCH_TIMEOUT_SECONDS"
    )

    # Upper bound (seconds) that buildPackFromUrl (and the MCP build_pack_from_url
    # tool) waits synchronously for a build to finish before falling back to
    # returning a job_id for polling. Kept inside the ~45s ChatGPT Actions
    # gateway limit so fast builds return a completed pack (with download_url) in
    # a single round-trip, while slow/large builds degrade to async polling.
    safe_memory_sync_build_wait_seconds: float = Field(
        default=40.0, alias="SAFE_MEMORY_SYNC_BUILD_WAIT_SECONDS"
    )

    # ---- Ingestion limits (single file + folder bundles) -------------------
    safe_memory_max_upload_file_size_mb: int = Field(
        default=50, alias="SAFE_MEMORY_MAX_UPLOAD_FILE_SIZE_MB"
    )
    safe_memory_max_folder_files: int = Field(
        default=200, alias="SAFE_MEMORY_MAX_FOLDER_FILES"
    )
    safe_memory_max_folder_total_size_mb: int = Field(
        default=200, alias="SAFE_MEMORY_MAX_FOLDER_TOTAL_SIZE_MB"
    )
    safe_memory_allowed_extensions: str = Field(
        default=(
            ".txt,.md,.csv,.tsv,.xlsx,.xls,.json,.pdf,.docx,.pptx,"
            ".png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp"
        ),
        alias="SAFE_MEMORY_ALLOWED_EXTENSIONS",
    )
    # OCR (Tesseract) tuning for scanned PDFs and image uploads. Kept modest so a
    # small ECS instance is not overwhelmed by large scans.
    safe_memory_ocr_languages: str = Field(
        default="jpn+eng", alias="SAFE_MEMORY_OCR_LANGUAGES"
    )
    safe_memory_ocr_dpi: int = Field(default=200, alias="SAFE_MEMORY_OCR_DPI")
    safe_memory_max_ocr_pages: int = Field(
        default=30, alias="SAFE_MEMORY_MAX_OCR_PAGES"
    )

    # ---- Alibaba Cloud OSS handoff -----------------------------------------
    # Secrets are read from the environment only; never hardcode them here.
    oss_enabled: bool = Field(default=False, alias="OSS_ENABLED")
    oss_bucket: str = Field(default="", alias="OSS_BUCKET")
    oss_region: str = Field(default="", alias="OSS_REGION")
    oss_endpoint: str = Field(default="", alias="OSS_ENDPOINT")
    oss_bucket_endpoint: str = Field(default="", alias="OSS_BUCKET_ENDPOINT")
    oss_access_key_id: str = Field(default="", alias="OSS_ACCESS_KEY_ID")
    oss_access_key_secret: str = Field(default="", alias="OSS_ACCESS_KEY_SECRET")
    oss_upload_prefix: str = Field(default="uploads/", alias="OSS_UPLOAD_PREFIX")
    oss_export_prefix: str = Field(default="exports/", alias="OSS_EXPORT_PREFIX")
    oss_signed_url_ttl_seconds: int = Field(
        default=3600, alias="OSS_SIGNED_URL_TTL_SECONDS"
    )
    oss_delete_source_after_processing: bool = Field(
        default=True, alias="OSS_DELETE_SOURCE_AFTER_PROCESSING"
    )

    @property
    def oss_ready(self) -> bool:
        """True when OSS is enabled AND all required settings are present."""
        return bool(
            self.oss_enabled
            and (self.oss_endpoint or "").strip()
            and (self.oss_bucket or "").strip()
            and (self.oss_access_key_id or "").strip()
            and (self.oss_access_key_secret or "").strip()
        )

    @property
    def allowed_extensions(self) -> list[str]:
        """Parse SAFE_MEMORY_ALLOWED_EXTENSIONS into a normalized lowercase list."""
        raw = self.safe_memory_allowed_extensions or ""
        exts = []
        for part in raw.split(","):
            ext = part.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            exts.append(ext)
        return exts or [".txt", ".md", ".csv", ".xlsx"]

    @property
    def has_qwen_credentials(self) -> bool:
        """True when a non-placeholder API key is configured."""
        key = (self.qwen_api_key or "").strip()
        return bool(key) and key.lower() not in {"replace_me", "changeme", "your_key"}

    @property
    def auth_enabled(self) -> bool:
        """True when a non-placeholder Safe Memory API key is configured."""
        key = (self.safe_memory_api_key or "").strip()
        return bool(key) and key.lower() not in {"change_me", "changeme", "replace_me"}

    @property
    def cors_origins(self) -> list[str]:
        """Parse the comma-separated CORS origins into a clean list."""
        raw = self.safe_memory_cors_origins or ""
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        return origins or ["http://localhost:3000", "http://localhost:8787"]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


# Convenient module-level settings object.
settings = get_settings()
