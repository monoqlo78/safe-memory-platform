#!/usr/bin/env python
"""Build a Safe Memory Pack from a local file or folder (no HTTP required).

Reuses the same in-process ingest pipeline as the API (``_run_pack_build``), so
classification, translation, sealing, retention, and the optional Alibaba OSS
handoff behave identically. A folder is packed into an in-memory ZIP (respecting
allowed extensions, hidden/system-file skipping, and max file-count / total-size
limits) and fed through the existing folder-ZIP path so every file merges into a
single pack.

Absolute local paths are never printed in the summary. Secrets and signed-URL
query strings are never logged.

Example:
    python scripts/build_pack_from_path.py --input ./knowledge \\
        --agent-id acme --pack-id acme-kb --title "ACME KB" \\
        --retention-mode process_and_return
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import uuid
import zipfile

# Make the backend package importable when run from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(os.path.dirname(_HERE), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.api import packs  # noqa: E402
from app.config import settings  # noqa: E402
from app.core import jobs_store  # noqa: E402
from app.models.job_schema import RetentionMode  # noqa: E402
from app.models.pack_schema import Classification  # noqa: E402

_SKIP_PREFIXES = (".", "~", "__MACOSX")
_SKIP_NAMES = {"thumbs.db", ".ds_store"}


def _is_hidden_or_temp(name: str) -> bool:
    base = os.path.basename(name)
    if base.lower() in _SKIP_NAMES:
        return True
    return any(base.startswith(p) for p in _SKIP_PREFIXES)


def _allowed(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    return ext in settings.allowed_extensions


def _build_folder_zip(root: str) -> tuple[bytes, int, int, list]:
    """Zip supported files under ``root`` in memory (relative paths preserved)."""
    max_files = settings.safe_memory_max_folder_files
    max_total = settings.safe_memory_max_folder_total_size_mb * 1024 * 1024
    files_seen = 0
    files_added = 0
    skipped: list = []
    total = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _is_hidden_or_temp(d)]
            for fname in filenames:
                if _is_hidden_or_temp(fname):
                    continue
                files_seen += 1
                if not _allowed(fname):
                    skipped.append({"filename": fname, "reason": "unsupported type"})
                    continue
                abspath = os.path.join(dirpath, fname)
                size = os.path.getsize(abspath)
                total += size
                if total > max_total:
                    raise SystemExit(
                        f"Folder exceeds max total size "
                        f"{settings.safe_memory_max_folder_total_size_mb} MB."
                    )
                files_added += 1
                if files_added > max_files:
                    raise SystemExit(f"Folder exceeds max file count {max_files}.")
                rel = os.path.relpath(abspath, root).replace("\\", "/")
                with open(abspath, "rb") as fh:
                    zf.writestr(rel, fh.read())
    return buf.getvalue(), files_seen, files_added, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="File or folder path")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--retention-mode", default="process_and_return")
    parser.add_argument("--source-language", default=None)
    parser.add_argument("--canonical-language", default="en")
    parser.add_argument(
        "--return-download-url",
        default="true",
        choices=["true", "false"],
    )
    parser.add_argument(
        "--debug-keep-upload",
        default="false",
        choices=["true", "false"],
    )
    args = parser.parse_args()

    path = args.input
    if not os.path.exists(path):
        print(f"Input does not exist: {os.path.basename(path)}", file=sys.stderr)
        return 2

    files_seen = files_added = 0
    skipped: list = []
    if os.path.isdir(path):
        data, files_seen, files_added, skipped = _build_folder_zip(path)
        filename = "bundle.zip"
        input_kind = "folder"
    else:
        if not _allowed(os.path.basename(path)):
            print("Unsupported file type for the configured allowed extensions.",
                  file=sys.stderr)
            return 2
        with open(path, "rb") as fh:
            data = fh.read()
        filename = os.path.basename(path)
        files_seen = files_added = 1
        input_kind = "file"

    try:
        retention = RetentionMode(args.retention_mode)
    except ValueError:
        print(f"Invalid retention_mode: {args.retention_mode}", file=sys.stderr)
        return 2

    job_id = uuid.uuid4().hex
    try:
        job, _audit = packs._run_pack_build(
            job_id=job_id,
            agent_id=args.agent_id,
            pack_id=args.pack_id,
            title=args.title,
            data=data,
            filename=filename,
            source_language=args.source_language,
            canonical_language=args.canonical_language,
            default_classification=Classification.INTERNAL,
            retention_mode=retention,
            debug_keep_upload=args.debug_keep_upload == "true",
            return_download_url=args.return_download_url == "true",
            delete_source_after_processing=args.debug_keep_upload != "true",
        )
    except packs.UploadProcessingError as exc:
        print(f"Build failed: {exc.detail}", file=sys.stderr)
        return 1

    jobs_store.save_job(job)

    # Regenerate a signed URL (if OSS) via the job response helper.
    from app.models.job_schema import job_to_response

    resp = job_to_response(job)

    summary = {
        "job_id": job.job_id,
        "pack_id": job.pack_id,
        "input_type": job.input_type or input_kind,
        "files_seen": files_seen,
        "files_processed": files_added - len(skipped) if input_kind == "folder"
        else files_added,
        "files_skipped": len(skipped) + len(job.unsupported_files),
        "entry_count": job.entry_count,
        "classification_counts": job.classification_counts,
        "raw_files_deleted": job.raw_upload_deleted,
        "working_files_deleted": job.working_files_deleted,
        "pack_persisted": job.pack_persisted,
        "catalog_visible": job.catalog_visible,
        "oss_export_uploaded": job.oss_export_uploaded,
        "oss_object_key": job.oss_object_key,
        "signed_download_url_expires_at": resp.expires_at_url,
        "signed_download_url": resp.download_url,
        "unsupported_files": (skipped + job.unsupported_files) or [],
        "warnings": job.warnings,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
