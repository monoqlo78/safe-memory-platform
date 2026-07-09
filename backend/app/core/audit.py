"""Audit report construction for Safe Memory Packs and exports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List

from app.core.ledger import verify_ledger_chain
from app.models.pack_schema import Classification, Entry, SafeMemoryPack

_SENSITIVE = {Classification.CONFIDENTIAL, Classification.SECRET}


def _classification_counts(entries: List[Entry]) -> Dict[str, int]:
    counter = Counter(e.classification.value for e in entries)
    return dict(counter)


def build_audit_report(pack: SafeMemoryPack) -> dict:
    """Build an audit report for a pack, including ledger verification."""
    valid_chain, chain_warnings = verify_ledger_chain(pack.ledger, pack.entries)
    counts = _classification_counts(pack.entries)

    warnings: List[str] = list(chain_warnings)
    sensitive_count = sum(
        1 for e in pack.entries if e.classification in _SENSITIVE
    )
    if sensitive_count:
        warnings.append(
            f"Pack contains {sensitive_count} CONFIDENTIAL/SECRET entries; "
            "handle exports carefully."
        )
    secret_count = counts.get(Classification.SECRET.value, 0)
    if secret_count:
        warnings.append(
            f"{secret_count} SECRET entries will never be sent to external LLMs."
        )

    return {
        "pack_id": pack.manifest.pack_id,
        "agent_id": pack.manifest.agent_id,
        "title": pack.manifest.title,
        "version": pack.manifest.version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(pack.entries),
        "ledger_count": len(pack.ledger),
        "valid_hash_chain": valid_chain,
        "classification_counts": counts,
        "sensitive_entry_count": sensitive_count,
        "warnings": warnings,
    }


def audit_export_result(
    included: List[Entry],
    excluded: List[Entry],
    allowed_classifications: List[Classification] | None,
    redact_sensitive_text: bool,
    remove_sources: bool,
) -> dict:
    """Build an audit summary for an export operation."""
    warnings: List[str] = []

    leaked = [e for e in included if e.classification in _SENSITIVE]
    if leaked:
        if allowed_classifications is None or not any(
            e.classification in allowed_classifications for e in leaked
        ):
            warnings.append(
                "CONFIDENTIAL/SECRET entries were included without explicit allowance."
            )
        else:
            warnings.append(
                f"{len(leaked)} sensitive entries were explicitly included in export."
            )
        if not redact_sensitive_text:
            warnings.append(
                "Sensitive entries were included WITHOUT redaction."
            )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "included_classifications": _classification_counts(included),
        "excluded_classifications": _classification_counts(excluded),
        "redacted": redact_sensitive_text,
        "sources_removed": remove_sources,
        "warnings": warnings,
    }
