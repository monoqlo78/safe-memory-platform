# Safe Memory Platform

Dockerized local-first memory infrastructure for AI agents.

> **Global AI Hackathon Series with Qwen Cloud — submission.**
> **Track:** Memory Agent. **AI engine:** Qwen Cloud (`text-embedding-v4` for
> embeddings, `qwen-plus`/`qwen-flash` for classification, reasoning, and answer
> generation). **Deployment:** Alibaba Cloud ECS (Singapore) via Docker Compose,
> live at `https://smp.sdesigner.tokyo`, reachable from the Web UI, ChatGPT
> Actions, and Claude/MCP.
>
> - Architecture & diagrams: [`docs/architecture.md`](docs/architecture.md)
> - Full write-up: [`docs/hackathon-submission.md`](docs/hackathon-submission.md)
> - License: [MIT](LICENSE)
> - Demo video: _add your YouTube/Vimeo link here_

## Deployment Proof / Alibaba Cloud & Qwen Cloud Usage

This project runs on **Alibaba Cloud** and uses **Qwen Cloud** as its AI engine.
The links below point to the exact code that calls Alibaba Cloud services and APIs.

**Qwen Cloud (DashScope, OpenAI-compatible) — AI engine**

- Endpoint + models (`text-embedding-v4`, `qwen-plus`): [`backend/app/config.py#L25-L34`](backend/app/config.py#L25-L34)
- API client initialization: [`backend/app/core/qwen_client.py#L60-L76`](backend/app/core/qwen_client.py#L60-L76)
- Chat completion (classification, reasoning, answer generation): [`backend/app/core/qwen_client.py#L85-L104`](backend/app/core/qwen_client.py#L85-L104)
- Embeddings (vectorize pack entries and queries): [`backend/app/core/qwen_client.py#L113-L134`](backend/app/core/qwen_client.py#L113-L134)

**Alibaba Cloud OSS (Object Storage Service) — private pack handoff**

- OSS SDK (`oss2`) bucket connection: [`backend/app/core/oss_storage.py#L71-L82`](backend/app/core/oss_storage.py#L71-L82)
- Upload object to private bucket: [`backend/app/core/oss_storage.py#L85-L109`](backend/app/core/oss_storage.py#L85-L109)
- Short-lived signed download URL: [`backend/app/core/oss_storage.py#L112-L118`](backend/app/core/oss_storage.py#L112-L118)
- OSS configuration: [`backend/app/config.py#L159-L174`](backend/app/config.py#L159-L174)

**Alibaba Cloud ECS — runtime**

- Production Docker Compose (runs on the ECS instance): [`docker-compose.prod.yml`](docker-compose.prod.yml)
- Step-by-step ECS deployment guide: [Deploy to Alibaba Cloud ECS](#deploy-to-alibaba-cloud-ecs)
- Cloud SDK dependencies (`openai` = DashScope client, `oss2` = Alibaba OSS SDK): [`backend/requirements.txt#L6-L9`](backend/requirements.txt#L6-L9)

Live deployment: `https://smp.sdesigner.tokyo` (Alibaba Cloud ECS, Singapore region).

## Concept

Safe Memory Platform temporarily processes user data, converts it into portable Safe Memory Pack files, returns the pack to the owner, and deletes temporary working data.

Qwen Cloud powers:

- embeddings
- confidentiality classification
- reasoning
- answer generation
- safe memory exchange decisions

## Core Services

1. Memory Forge
   - Build Safe Memory Packs from temporary user data.

2. Memory Lens
   - Query, verify, and audit Safe Memory Packs.

3. Memory Workspace
   - Use packs to run project tasks with agents.

## What is a Safe Memory Pack?

A Safe Memory Pack (`*.smp.json`) is a portable, policy-aware agent memory file.
It is **not** a vector database clone. Each pack contains:

- **entries** — text chunks with Qwen embeddings, keywords, and metadata
- **classification** — one of `PUBLIC`, `SHAREABLE`, `INTERNAL`, `CONFIDENTIAL`, `SECRET`, `EPHEMERAL`
- **policy flags** — whether an entry can be used for queries, sent to an LLM, or exported
- **provenance** — where each entry came from
- **ledger** — an append-only chain of blocks, each sealed with a `sha256` hash of
  the previous block, so tampering is detectable

Policy rules enforced by the backend:

- `CONFIDENTIAL` and `SECRET` entries are excluded from exports unless explicitly allowed.
- `SECRET` entries are **never** sent to the external LLM.
- Sensitive text can be redacted on export.
- All file access is confined to `SAFE_MEMORY_ROOT`.

## Configuration

Copy the environment file and set your Qwen key:

```powershell
Copy-Item .env.example .env
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `QWEN_API_KEY` | _(none)_ | Qwen Cloud API key. **Never commit this.** |
| `QWEN_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | OpenAI-compatible endpoint |
| `QWEN_CHAT_MODEL` | `qwen-plus` | Chat / reasoning model |
| `QWEN_EMBEDDING_MODEL` | `text-embedding-v4` | Embedding model |
| `SAFE_MEMORY_ROOT` | `/app/SafeMemory` | Storage root (all IO is confined here) |
| `SAFE_MEMORY_API_KEY` | _(none)_ | Shared API key. When set, all `/api/*` routes require `X-Safe-Memory-Key`. **Never commit this.** |
| `SAFE_MEMORY_CORS_ORIGINS` | `http://localhost:3000,http://localhost:8787` | Comma-separated allowed CORS origins |
| `APP_ENV` | `local` | Environment label (`local` / `production`) |

> If `QWEN_API_KEY` is missing or a call fails, the backend uses **safe fallbacks**
> (deterministic hash embeddings, heuristic classification, and summarized answers)
> so demos never crash.

## Authentication

When `SAFE_MEMORY_API_KEY` is set, every `/api/*` route requires the header:

```
X-Safe-Memory-Key: <your key>
```

Requests without a valid key get `401`. `GET /health`, `/docs`, `/openapi.json`, and
`/redoc` are always open (no key required) so health checks and GPT Actions import work.
If `SAFE_MEMORY_API_KEY` is empty/unset the backend runs in **dev mode** (open) and logs a
single warning at startup. The key is never logged. Always set it before exposing the
service publicly.

## Run locally without Docker

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Point storage at the local SafeMemory folder
$env:SAFE_MEMORY_ROOT = (Resolve-Path "..\SafeMemory").Path

uvicorn app.main:app --reload --port 8787
```

Open http://localhost:8787/docs

## Run with Docker

```powershell
Copy-Item .env.example .env   # then edit .env and set QWEN_API_KEY
docker compose up --build
```

Open http://localhost:8787/docs

## Example curl commands

Build a pack (Memory Forge):

```bash
curl -X POST http://localhost:8787/api/packs/build \
  -H "Content-Type: application/json" \
  -d '{
        "agent_id": "tax-agent",
        "pack_id": "demo1",
        "title": "Demo Pack",
        "source_text": "The quarterly invoice total is 500 USD. The project deadline is Friday.",
        "default_classification": "INTERNAL",
        "delete_source_after_build": true
      }'
```

Query a pack (Memory Lens):

```bash
curl -X POST http://localhost:8787/api/packs/query \
  -H "Content-Type: application/json" \
  -d '{
        "agent_id": "tax-agent",
        "pack_path": "agents/tax-agent/packs/internal/demo1.smp.json",
        "query": "what is the invoice total?",
        "top_k": 3
      }'
```

Append a memory entry:

```bash
curl -X POST http://localhost:8787/api/packs/append \
  -H "Content-Type: application/json" \
  -d '{
        "agent_id": "tax-agent",
        "pack_path": "agents/tax-agent/packs/internal/demo1.smp.json",
        "text": "Follow-up: the invoice was paid on time.",
        "source": "user_input"
      }'
```

Verify the ledger hash chain:

```bash
curl -X POST http://localhost:8787/api/packs/verify \
  -H "Content-Type: application/json" \
  -d '{ "pack_path": "agents/tax-agent/packs/internal/demo1.smp.json" }'
```

Export a shareable copy (excludes CONFIDENTIAL/SECRET unless explicitly allowed):

```bash
curl -X POST http://localhost:8787/api/packs/export \
  -H "Content-Type: application/json" \
  -d '{
        "agent_id": "tax-agent",
        "pack_path": "agents/tax-agent/packs/internal/demo1.smp.json",
        "export_name": "demo_shareable",
        "allowed_classifications": ["PUBLIC", "SHAREABLE", "INTERNAL"],
        "remove_sources": true,
        "redact_sensitive_text": true
      }'
```

List an agent's catalog:

```bash
curl http://localhost:8787/api/agents/tax-agent/catalog
```

Run a project task (Memory Workspace):

```bash
curl -X POST http://localhost:8787/api/projects/run \
  -H "Content-Type: application/json" \
  -d '{
        "project_id": "p1",
        "agent_id": "tax-agent",
        "task": "Summarize the current invoice status.",
        "pack_paths": ["agents/tax-agent/packs/internal/demo1.smp.json"]
      }'
```

Build a pack from an uploaded file (multipart; `.txt`, `.md`, or `.xlsx`):

```bash
curl -X POST http://localhost:8787/api/packs/build-from-upload \
  -F "agent_id=tax-agent" \
  -F "pack_id=upload1" \
  -F "title=Uploaded Pack" \
  -F "source_language=ja" \
  -F "file=@notes.txt"
```

> When `SAFE_MEMORY_API_KEY` is set, add `-H "X-Safe-Memory-Key: <your key>"` to every
> `/api/*` request above.

## Deploy to Alibaba Cloud ECS

These steps deploy the backend to an Alibaba Cloud ECS instance using the production
compose file. The confirmed target for this deployment is:

| Setting | Value |
| --- | --- |
| ECS public IP | `<your-ecs-ip>` |
| Region | Alibaba Cloud, Singapore |
| Instance | 2 vCPU / 4 GiB, Ubuntu/Linux |
| Health check | https://smp.sdesigner.tokyo/health |
| Swagger docs | https://smp.sdesigner.tokyo/docs |
| OpenAPI (GPT Actions) | https://smp.sdesigner.tokyo/openapi.json |

### 1. Install Docker on the ECS instance

```bash
# SSH into the instance first (port 22 must be open), then:
ssh root@<your-ecs-ip>
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
# Compose plugin is included with recent Docker; verify:
docker compose version
```

### 2. Open the inbound ports in the security group

In the ECS console, edit the instance's **Security Group** and add inbound rules:

- **Port 22/TCP** — SSH, from your admin IP only.
- **Port 8787/TCP** — the API, from your allowed source (e.g. your GPT/office IP range).
- **Port 443/TCP** — only if you later add nginx + HTTPS in front of the backend
  (recommended for anything public).

> Never expose port 8787 to `0.0.0.0/0` without `SAFE_MEMORY_API_KEY` set.

### 3. Get the code and configure `.env`

```bash
git clone <your-repo-url> safe-memory-platform
cd safe-memory-platform

cp .env.example .env
# Edit .env and set REAL values:
#   QWEN_API_KEY=<your real Qwen key>
#   SAFE_MEMORY_API_KEY=<a long random secret>
#   SAFE_MEMORY_ROOT=/app/SafeMemory
#   APP_ENV=production
#   SAFE_MEMORY_CORS_ORIGINS=https://chat.openai.com,https://claude.ai,https://<your-frontend>
nano .env
```

### 4. Build and run (production)

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

This runs uvicorn **without** `--reload` and persists packs to `./SafeMemory` on the host
(mounted at `/app/SafeMemory` in the container).

### 5. Verify

```bash
curl https://smp.sdesigner.tokyo/health
# -> {"status":"ok", "auth_enabled": true, ...}
```

Open the interactive docs in a browser:

```
https://smp.sdesigner.tokyo/docs
```

### 6. Connect from GPT and Claude

The backend is a plain HTTP JSON API, so any LLM platform that can call HTTP tools can use
it. Auth is a single API-key header: `X-Safe-Memory-Key: <SAFE_MEMORY_API_KEY>`.

**GPT (Custom GPT Actions):**

1. In the GPT editor, add an Action and import the schema from
   `https://smp.sdesigner.tokyo/openapi.json`.
2. Set **Authentication → API Key → Custom header name** `X-Safe-Memory-Key`, value =
   your `SAFE_MEMORY_API_KEY`.
3. Key operation IDs: `buildMemoryPack`, `queryMemoryPack`, `exportMemoryPack`,
   `verifyMemoryPack`, `getAgentCatalog`, `runProjectWithMemory`, `buildMemoryPackFromUpload`.

**Claude (MCP or direct HTTP):**

- Use an MCP HTTP/fetch tool (or Claude's tool-use with a small wrapper) that calls the
  same endpoints, e.g. `POST https://smp.sdesigner.tokyo/api/packs/query` with the
  `X-Safe-Memory-Key` header. The `/openapi.json` schema describes every route and payload.

```bash
curl -X POST https://smp.sdesigner.tokyo/api/packs/query \
  -H "Content-Type: application/json" \
  -H "X-Safe-Memory-Key: <your key>" \
  -d '{"agent_id":"tax-agent","pack_id":"demo1","query":"what is the invoice total?"}'
```

> **Qwen base URLs (for the LLM side, not this backend):** Qwen Cloud offers both an
> OpenAI-compatible endpoint (`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`) and
> an Anthropic-compatible endpoint (`https://dashscope-intl.aliyuncs.com/apps/anthropic`).
> This backend keeps the OpenAI-compatible `QWEN_BASE_URL` for its own embeddings/reasoning;
> the Anthropic-compatible URL is only relevant if you point a Claude-style client directly
> at Qwen.

### Deployment warnings

- **Never expose the service without `SAFE_MEMORY_API_KEY` set.** An unset key runs in open
  dev mode.
- **Never commit `.env`.** It holds your Qwen key and API key.
- Prefer HTTPS (nginx + certbot, or an Alibaba Cloud SLB/ALB) for public traffic.
- Back up the host `./SafeMemory` directory; it holds all packs.

## Job & session retention

Uploads are processed as short-lived **jobs**. Raw uploaded files are only needed while a
job is being processed, so they are **session-scoped and deleted after processing**
(unless you pass `debug_keep_upload=true`). Generated packs are persisted in the server
vault **only** when `retention_mode=server_vault`; otherwise they are temporary and expire.
Job metadata is stored as one small JSON file per job under `SAFE_MEMORY_ROOT/jobs/` — no
database. Everything stays confined to the `SAFE_MEMORY_ROOT` sandbox.

`POST /api/packs/build-from-upload` accepts a `retention_mode` field with three values:

| `retention_mode`     | Raw upload            | Generated pack                                             | In agent catalog? |
| -------------------- | --------------------- | --------------------------------------------------------- | ----------------- |
| `session`            | deleted after job     | temporary; `download_url` provided; deleted after TTL     | No                |
| `process_and_return` | deleted after job     | temporary; `download_url` provided; deleted after TTL     | No                |
| `server_vault`       | deleted after job     | persisted under the agent vault; queryable later          | Yes               |

Default is `process_and_return`. Temporary packs expire after
`SAFE_MEMORY_TEMP_TTL_MINUTES` (default `60`). Pass `debug_keep_upload=true` to keep the raw
upload and working files for debugging.

### Retention endpoints (all require `X-Safe-Memory-Key` when auth is enabled)

| Method & path                     | operation_id     | Purpose                                                              |
| --------------------------------- | ---------------- | ------------------------------------------------------------------- |
| `GET /api/jobs/{job_id}`          | `getJob`         | Return job metadata (server paths hidden unless `?debug=true`).      |
| `DELETE /api/jobs/{job_id}`       | `deleteJob`      | Force-clean a job: delete raw upload, working files, and temp pack (server_vault packs are preserved). |
| `POST /api/jobs/cleanup`          | `cleanupJobs`    | Delete expired temporary packs/working files; returns cleanup counts. |
| `GET /api/jobs/{job_id}/download` | `getJobDownload` | Stream the generated pack file (available until it expires).         |

Example: build a temporary pack, then download it.

```bash
curl -sS -X POST https://smp.sdesigner.tokyo/api/packs/build-from-upload \
  -H "X-Safe-Memory-Key: $SAFE_MEMORY_API_KEY" \
  -F "agent_id=tax-agent" -F "pack_id=q3-notes" -F "title=Q3 Notes" \
  -F "retention_mode=process_and_return" \
  -F "file=@notes.txt"
# -> { "job_id": "...", "status": "COMPLETED", "retention_mode": "process_and_return",
#      "expires_at": "...", "download_url": "/api/jobs/<job_id>/download", ... }

curl -sS https://smp.sdesigner.tokyo/api/jobs/<job_id>/download \
  -H "X-Safe-Memory-Key: $SAFE_MEMORY_API_KEY" -o q3-notes.smp.json
```

To reclaim space, periodically call `POST /api/jobs/cleanup` (e.g. from cron) to purge
expired temporary packs. `server_vault` packs are never touched by cleanup.

## Large-file / LLM-safe upload flow

GPT Actions and Claude (MCP/tool-use) **cannot send multipart file bytes** and have
response-size and ~45s timeout limits. So file bytes travel through a separate
**direct-upload channel** while the LLM only ever exchanges small JSON (an `upload_id`,
then a `job_id` it polls). Processing runs **asynchronously**, so big files (e.g. a
732-row Japanese accounting Excel) never time the LLM out.

Three JSON steps + one raw-bytes PUT:

1. **`POST /api/uploads/init`** (`initUpload`, API-key) → `{ upload_id, upload_url,
   upload_token, method:"PUT", expires_at }`. `upload_url` is absolute (built from
   `SAFE_MEMORY_PUBLIC_BASE_URL`).
2. **`PUT /api/uploads/{upload_id}/content?token=...`** (`uploadContent`) → streams the
   raw bytes. Authorized by the one-time `upload_token` (no API key needed, so a browser
   can upload directly). Enforces `SAFE_MEMORY_MAX_UPLOAD_MB` (413 if exceeded).
3. **`POST /api/packs/build-from-upload-ref`** (`buildMemoryPackFromUploadRef`, API-key) →
   returns `{ job_id, status:"PROCESSING" }` immediately and processes in the background.
4. **Poll `GET /api/jobs/{job_id}`** until `status` is `COMPLETED` or `FAILED`. Retention
   semantics (`session` / `process_and_return` / `server_vault`), `download_url`,
   `expires_at`, and staging cleanup match the multipart endpoint. The staged upload is
   deleted after processing (kept only when `debug_keep_upload=true`).

```bash
BASE=https://smp.sdesigner.tokyo
KEY=$SAFE_MEMORY_API_KEY

# 1. init
INIT=$(curl -sS -X POST $BASE/api/uploads/init -H "X-Safe-Memory-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"filename":"book.xlsx"}')
UID=$(echo "$INIT" | python -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
TOK=$(echo "$INIT" | python -c 'import sys,json;print(json.load(sys.stdin)["upload_token"])')

# 2. PUT bytes (token auth, no API key)
curl -sS -X PUT "$BASE/api/uploads/$UID/content?token=$TOK" \
  --data-binary @book.xlsx

# 3. start async build
curl -sS -X POST $BASE/api/packs/build-from-upload-ref -H "X-Safe-Memory-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"upload_id\":\"$UID\",\"agent_id\":\"tax-agent\",\"pack_id\":\"q3\",\"title\":\"Q3\",\"retention_mode\":\"process_and_return\"}"

# 4. poll GET /api/jobs/<job_id> until status == COMPLETED, then download.
```

Async job status transitions: `PROCESSING → COMPLETED` (or `PROCESSING → FAILED`, with the
error in `warnings`).

### Browser upload page (`GET /upload`)

For non-engineers, an open self-contained page at **`https://smp.sdesigner.tokyo/upload`** drives
the whole flow in the browser. Enter the API key + agent/pack/title, pick a retention mode, then
**drop a folder or select multiple files** (a single file works too). No cloud sign-in is required.

- **Folder / multi-file → one pack.** Dropping a folder (recursive drag-drop via
  `webkitGetAsEntry`) or choosing multiple files zips them **in your browser** (JSZip) into one
  `bundle.zip`, keeping each file's relative path. The server detects the folder ZIP and **merges
  every supported file into a single pack**, with each entry's `provenance.source` set to its
  origin filename. Only `.txt/.md/.csv/.tsv/.json/.xlsx/.xls` are included; hidden/system files
  (`.`-prefixed, `__MACOSX`, `Thumbs.db`, `~$…`) are skipped, and the page reports how many were
  skipped. A single dropped file uploads directly (no zip), preserving the original behavior.
- **Same staged pipeline.** The bundle flows through the existing four steps — `initUpload` →
  token-authorized `PUT` → `build-from-upload-ref` → job poll — so file bytes never touch
  GPT/Claude and all size/retention guards apply.
- **Share the result.** On completion the page shows `entry_count`, merged file count,
  `classification_counts`, `retention_mode`, and any skipped/unsupported files. For
  `process_and_return` with OSS enabled, `job.download_url` is an **OSS signed URL** shown with a
  **Copy share link** button — paste that URL into GPT's `importPackByRef` to reuse the pack as a
  shareable "IQ file". A **Download** button is always available. (Without OSS the link is a local
  tokenized URL that needs the `X-Safe-Memory-Key` header.) `server_vault` shows the queryable
  vault `pack_id` instead. The page itself needs no API key; the raw PUT is authorized by the
  upload token.

> JSZip is loaded from a CDN (`https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js`)
> by the **user's browser** — the ECS server never fetches it, so no server-side egress is needed.

New config: `SAFE_MEMORY_MAX_UPLOAD_MB` (default 50), `SAFE_MEMORY_UPLOAD_TTL_MINUTES`
(staging expiry, default 60), `SAFE_MEMORY_STORAGE_BACKEND` (default `local`; an OSS stub is
ready for a future Alibaba OSS backend). Expired-but-unconsumed staged uploads are purged by
`POST /api/jobs/cleanup`.

### One-time upload links (`/u/{token}`) — keyless, single-use

So a person never has to see or paste the API key, an assistant can mint a **single-use, expiring
upload link** and just hand over the URL. No cloud login and no API key on the user's side.

Flow:

1. The assistant (already holding `X-Safe-Memory-Key`) calls **`createUploadLink`**
   (`POST /api/upload-links`). Optional fields: `agent_id`, `pack_id` (server-generated if omitted),
   `title`, `source_language`, `retention_mode` (default `process_and_return`), `classification`,
   `expires_in_seconds` (default `1800`, max `3600`), `max_uses` (default `1`). It returns
   `{ upload_url, claim_id, expires_at }` where `upload_url` is
   `https://smp.sdesigner.tokyo/u/{token}`.
2. The user opens `upload_url` — a keyless page (no API-key field) that reuses the same
   folder/JSZip upload UI — and drops a folder or files. The URL-path **token** authorizes only
   staging (`initUpload`) + **one** scoped `build-from-upload-ref` + reading that build's result.
   It can never reach catalog/query/delete or any other job.
3. On completion the page shows the OSS signed **share link** (with a Copy button). The assistant
   polls **`getUploadLinkResult`** (`GET /api/upload-links/{claim_id}`) until `status` is
   `COMPLETED`, then reuses the returned `download_url` / `pack_id` via `importPackByRef`.

The master API key is never exposed to the user, returned, or logged. Links are single-use
(`max_uses`, default 1) and expire (`expires_at`); invalid/expired/used tokens get a friendly error
page and a `401` on the upload endpoints. No new environment variables or dependencies are needed —
claims are persisted as one small JSON file per link under `SAFE_MEMORY_ROOT/upload_links/`.

## Pack exchange (import by URL)

Packs become a **network**: one agent/person exports a Safe Memory Pack, and another imports
it over a URL to gain that "IQ". Because file bytes can't travel through an LLM, a **URL
(plain text) is the currency**. The exported pack can live on this server or any HTTPS host
(Google Drive / OneDrive / iCloud share links work).

- **`POST /api/packs/export`** (`exportMemoryPack`) now also returns an absolute HTTPS
  **`download_url`** — a tokenized, API-key-free link (`/api/packs/dl/{token}`) so browsers,
  Drive, and other agents can fetch the exported `.smp.json`. It's built from
  `SAFE_MEMORY_PUBLIC_BASE_URL` (relative if unset).
- **`POST /api/packs/import-by-ref`** (`importPackByRef`, API-key, JSON-only so GPT/Claude can
  call it) fetches a pack from an HTTPS URL, verifies it, and imports it into an agent's vault.
  Request: `{ "url": "https://...", "agent_id": "...", "pack_id": "<optional>" }`. Response:
  `{ pack_id, entry_count, classification_summary, verified, warnings }`.

**Safety guards on import:**

- **HTTPS only** — plain `http://` is rejected (400).
- **Size cap** — streamed with a hard limit `SAFE_MEMORY_MAX_IMPORT_MB` (default 25); larger
  downloads get 413.
- **SSRF protection** — the target host must not resolve to a private / loopback / link-local /
  reserved / multicast address; every redirect hop is re-checked.
- **Ledger verification** — the pack's sha256 hash chain is verified. Tampered packs return
  `verified: false` (with a warning) by default, or are rejected (422) when
  `SAFE_MEMORY_IMPORT_REQUIRE_VALID_LEDGER=true`.

```bash
BASE=https://smp.sdesigner.tokyo
KEY=$SAFE_MEMORY_API_KEY

# 1. Export a shareable copy -> get an HTTPS download_url
curl -sS -X POST $BASE/api/packs/export -H "X-Safe-Memory-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"tax-agent","pack_id":"q3-notes","export_name":"q3-shared"}'
# -> { "export_path":"...", "download_url":"https://.../api/packs/dl/<token>", ... }

# 2. Another agent imports it by URL (JSON only — works from GPT/Claude)
curl -sS -X POST $BASE/api/packs/import-by-ref -H "X-Safe-Memory-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://.../api/packs/dl/<token>","agent_id":"peer-agent","pack_id":"borrowed-iq"}'
# -> { "pack_id":"borrowed-iq", "entry_count":N, "verified":true, ... }
```

> **Google Drive share links:** convert the "view" link to a direct-download link. From a link
> like `https://drive.google.com/file/d/<FILE_ID>/view`, use
> `https://drive.google.com/uc?export=download&id=<FILE_ID>` as the import `url`. OneDrive/iCloud
> public share links that return the raw file over HTTPS work directly. `SAFE_MEMORY_PUBLIC_BASE_URL`
> should be set so exported `download_url`s are absolute. New config:
> `SAFE_MEMORY_MAX_IMPORT_MB=25`, `SAFE_MEMORY_IMPORT_REQUIRE_VALID_LEDGER=false`.

## Build a pack from a share link (SharePoint / Google Drive / OneDrive / Dropbox)

`importPackByRef` imports an existing **`.smp.json` pack**. **`buildPackFromUrl`** is different:
it fetches a **raw file** (`.xlsx` / `.csv` / `.txt` / `.md`) from a share link and **builds a new
pack** through the normal ingest pipeline (classify → translate → seal → retention). This lets
GPT/Claude or a user create a pack from a document by passing only a URL — no file bytes over the
LLM channel.

- **`POST /api/packs/build-from-url`** (`buildPackFromUrl`, API-key, JSON-only, visible to
  GPT/Claude). **Async**: returns `{ job_id, status: "PROCESSING" }` immediately; poll
  `GET /api/jobs/{job_id}` until `COMPLETED`.
  Request: `{ url, agent_id, pack_id, title, source_language?, canonical_language="en",
  default_classification="INTERNAL", retention_mode="process_and_return", debug_keep_upload? }`.

**Share links are auto-normalized to direct-download URLs:**

| Provider | You paste | Server fetches |
| --- | --- | --- |
| SharePoint / OneDrive for Business | `.../:x:/g/personal/{user}/{shareId}?e=...` | `.../personal/{user}/_layouts/15/download.aspx?share={shareId}` |
| Google Drive | `.../file/d/{id}/view` or `open?id={id}` | `https://drive.google.com/uc?export=download&id={id}` |
| Dropbox | `...?dl=0` | `...?dl=1` |
| OneDrive personal (`1drv.ms`) | short link | followed via redirect + `download=1` |
| Anything else | any HTTPS URL | fetched as-is |

**In every provider, the link must be shared as "anyone with the link can download" (anonymous).**
SharePoint `?e=` preview links return HTML, so they are rewritten to the `download.aspx?share=`
form which returns the real file. The filename/extension is taken from `Content-Disposition`
first (SharePoint `download.aspx` paths have no extension), then the URL path, then the content
type — so the ingest reader is chosen correctly.

### Folder share links (ZIP)

You can also pass a **folder** share link. SharePoint / OneDrive / Dropbox return the whole
folder as a **ZIP** through the same normalized download URL (SharePoint `:f:` folder markers are
converted just like file links). The server then:

- Detects a folder ZIP (a plain archive; single Office files like `.xlsx` are *not* treated as
  folders because they carry an OPC `[Content_Types].xml` marker).
- Safely extracts it (zip-slip protection — absolute paths / `..` are rejected; a total
  uncompressed-size cap of `SAFE_MEMORY_MAX_UPLOAD_MB` guards against zip bombs).
- Ingests every supported member (`.xlsx` `.csv` `.tsv` `.txt` `.md`; hidden files, `__MACOSX`,
  and unsupported types are skipped) and **merges them into one pack** (`pack_id`), with each
  entry's `provenance.source` set to its origin filename. If no supported files are found, the
  job fails with a clear message.

> **Google Drive folders are not supported.** Drive has no anonymous folder-ZIP download (it needs
> the Drive API), so a `.../drive/folders/...` link fails fast with an actionable message: zip the
> folder and share the `.zip`, use individual file links, or use the `/upload` page. Individual
> Google Drive **file** links still work.

> **SharePoint / OneDrive folders are not supported either.** A SharePoint/OneDrive *folder* share
> link (the `:f:` marker) can't be read anonymously — Microsoft returns an HTML folder-browsing page
> (`onedrive.aspx`), not the files, and there is no anonymous ZIP or file-enumeration endpoint. Such
> links fail fast with a clear message. Instead, either **(1)** share an **individual file** inside
> the folder (e.g. an `.xlsx`), or **(2)** compress the folder into a single **`.zip` file** and
> share that `.zip` file's link. A shared `.zip` *file* returns real bytes, so the folder-ZIP
> expansion above kicks in and merges every file into one pack. You can also drop the whole folder
> on the [`/upload` page](#browser-upload-page-get-upload), which zips it in your browser and merges it into one pack —
> no cloud sign-in required.
>
> The distinction matters: sharing a **`.zip` file** works (real bytes → `_is_folder_zip` →
> extraction); asking the server to zip a **shared folder** on the fly does not (Microsoft only
> serves a web page anonymously).

**Same safety guards as the URL fetcher:** HTTPS only (`http://` → 400), SSRF protection
(private/loopback/link-local hosts rejected, re-checked per redirect hop), and a size cap of
`SAFE_MEMORY_MAX_UPLOAD_MB` (default 50; over-size → 413). Optional new config:
`SAFE_MEMORY_URL_FETCH_TIMEOUT_SECONDS=30`.

```bash
BASE=https://smp.sdesigner.tokyo
KEY=$SAFE_MEMORY_API_KEY

# 1. Kick off a build from a SharePoint share link (returns a job_id)
curl -sS -X POST $BASE/api/packs/build-from-url -H "X-Safe-Memory-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://contoso-my.sharepoint.com/:x:/g/personal/jdoe_contoso_com/IQ...?e=abcd","agent_id":"tax-agent","pack_id":"jp-accounting","title":"JP Accounting","source_language":"ja","retention_mode":"server_vault"}'
# -> { "job_id":"<id>", "status":"PROCESSING" }

# 2. Poll until COMPLETED
curl -sS $BASE/api/jobs/<id> -H "X-Safe-Memory-Key: $KEY"
# -> { "status":"COMPLETED", "entry_count":N, "classification_counts":{...}, ... }

# 3. server_vault packs appear in the catalog
curl -sS "$BASE/api/agents/tax-agent/packs" -H "X-Safe-Memory-Key: $KEY"
```

## Knowledge file and folder ingestion

The same pipeline powers every build path (`build-from-upload`, `build-from-upload-ref`,
`build-from-url`, and the `scripts/build_pack_from_path.py` CLI). Supported input files:

- **Ingested now:** `.txt`, `.md`, `.csv`, `.tsv`, `.xlsx`, `.json` (a JSON array becomes one
  record per item; a JSON object becomes `key: value` lines).
- **Recognized but not yet ingested:** `.xls`, `.pdf`, `.docx`. These are listed in the job's
  `unsupported_files` (`[{filename, reason}]`) instead of failing the whole job. A job only fails
  when *every* file is unsupported.

Folder ingestion (ZIP or the CLI's folder mode) merges every supported file into a single pack,
skips hidden / `__MACOSX` / system files, and enforces `SAFE_MEMORY_MAX_FOLDER_FILES` (default
200) and `SAFE_MEMORY_MAX_FOLDER_TOTAL_SIZE_MB` (default 200). Per-file provenance
(`provenance.source`) is preserved.

### Local CLI (no HTTP)

```bash
python scripts/build_pack_from_path.py \
  --input ./knowledge_folder \
  --agent-id acme --pack-id acme-kb --title "ACME KB" \
  --retention-mode process_and_return
# Prints a secret-free JSON summary: job_id, pack_id, input_type, files_seen,
# files_processed, entry_count, classification_counts, oss_object_key,
# signed_download_url (never logged), etc. Absolute local paths are never emitted.
```

## Alibaba OSS handoff

Generated packs can be handed off through a **private** Alibaba Cloud OSS bucket and shared only
via short-lived **signed URLs** — objects are never made public. This is optional and fully
backward compatible: when OSS is disabled the server falls back to the existing local tokenized
download (`/api/jobs/{job_id}/download`).

Enable it by setting these in the server `.env` (leave them empty in `.env.example`, and set real
values only on the ECS host):

```env
OSS_ENABLED=true
OSS_BUCKET=your-bucket
OSS_REGION=ap-southeast-1
OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com
OSS_ACCESS_KEY_ID=...        # a RAM user key — never the root account key
OSS_ACCESS_KEY_SECRET=...
OSS_UPLOAD_PREFIX=uploads/
OSS_EXPORT_PREFIX=exports/
OSS_SIGNED_URL_TTL_SECONDS=3600
OSS_DELETE_SOURCE_AFTER_PROCESSING=true
```

Behaviour by `retention_mode`:

- **process_and_return / session:** the `.smp.json` is uploaded to
  `exports/{job_id}/{pack_id}.smp.json`; the job returns a signed `download_url` (regenerated on
  each read so the raw signature is never persisted). Not added to the catalog.
- **server_vault:** persisted and catalog-visible as before; also uploaded to OSS when
  `return_download_url=true`.

`GET /health` reports `oss_enabled` (true only when OSS is on *and* fully configured). Machine
clients can request signed URLs via the hidden `POST /api/files/presign-upload`,
`POST /api/files/presign-download`, and `DELETE /api/files/object` endpoints (require
`X-Safe-Memory-Key`; return `503` when OSS is disabled; not shown in `openapi.json` because
GPT/Claude cannot transfer raw bytes).

**Security:** the bucket is private; sharing is via signed URLs only. The AccessKey id/secret and
the signature query string of any signed URL are **never logged** (`redact_signed_url` strips the
query for logs/metadata). Requires the `oss2` dependency (added to `requirements.txt` — rebuild
the image after pulling).

## Security notes

- **Never commit `.env` or hardcode API keys.** Keys are read only from environment variables and are never logged.
- **Set `SAFE_MEMORY_API_KEY`** before exposing the service; it gates all `/api/*` routes via the `X-Safe-Memory-Key` header. An empty key runs in open dev mode (with a startup warning).
- The backend only reads and writes inside `SAFE_MEMORY_ROOT`; all path inputs are sanitized and validated.
- `CONFIDENTIAL` / `SECRET` entries are excluded from shareable exports unless explicitly allowed, and `SECRET` entries are never sent to the external LLM.

## International Demo with Japanese Accounting Excel

This demo turns a Japanese accounting knowledge Excel file into a **bilingual**
Safe Memory Pack. Each entry keeps its original Japanese text as provenance and
adds a canonical **English** translation (via Qwen, with a safe fallback).
Retrieval, ranking, answering, and project output are all **English-first**,
while the Japanese source stays private and is stripped from shareable exports.

Bilingual entry fields (all optional and backward compatible):

- `text` — primary text (backward compatible)
- `original_text` — Japanese source text from Excel
- `canonical_text` — English normalized knowledge (used for retrieval)
- `source_language` — `"ja"`
- `canonical_language` — `"en"`
- `translation_note` — explanation or fallback note

> The real Excel file is **not** committed (see `.gitignore`). Place your file at
> `demoknowlege/results.xlsx`.

### Run it (Windows PowerShell)

```powershell
cd "C:\Users\msoga\OneDrive - Smart Designer\Projects\repos\safe-memory-platform"

# activate venv if needed
.\.venv\Scripts\Activate.ps1

# install dependencies (first time)
pip install -r backend/requirements.txt
pip install -r backend/requirements-dev.txt

# run the API
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8787 --reload

# in another terminal, run the importer
cd "C:\Users\msoga\OneDrive - Smart Designer\Projects\repos\safe-memory-platform"
python scripts/import_accounting_xlsx.py --input "demoknowlege/results.xlsx"

# run the full bilingual demo (import + query + export + project + narrative)
python scripts/demo_all_bilingual.py

# run tests
pytest -q
```

The importer builds a pack at
`SafeMemory/agents/tax-agent/packs/<classification>/jp-accounting-bilingual.smp.json`.
When running locally (no Docker), the scripts automatically point
`SAFE_MEMORY_ROOT` at `<project_root>/SafeMemory`.

Individual demo steps are also available:

```powershell
python scripts/demo_query_bilingual.py     # English query over the pack
python scripts/demo_export_bilingual.py    # safe shareable export (sources removed)
python scripts/demo_project_bilingual.py   # receipt-agent project run on the export
```

### With Qwen credentials

Set `QWEN_API_KEY` (never commit it). When present and not `replace_me`, the
importer produces real English translations and answers. Without it, the demo
still runs using deterministic fallbacks so it never stops.

The optional live integration test only runs when `QWEN_API_KEY` is set to a
real value:

```powershell
$env:QWEN_API_KEY = "<your-real-key>"   # set in your shell only, never commit
pytest -q tests/test_qwen_integration_optional.py
```
