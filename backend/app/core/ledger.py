"""Append-only ledger with a sha256 hash chain."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.models.pack_schema import Entry, LedgerBlock

GENESIS_HASH = "0" * 64


def compute_entry_hash(entry: Entry) -> str:
    """Compute a stable sha256 hash of a memory entry's content.

    Only content-bearing fields are hashed so that the hash is
    reproducible and independent of transient ordering.
    """
    payload = {
        "id": entry.id,
        "text": entry.text,
        "keywords": sorted(entry.keywords),
        "classification": entry.classification.value,
        "embedding_len": len(entry.embedding),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_block_hash(
    index: int,
    entry_id: Optional[str],
    action: str,
    entry_hash: str,
    previous_hash: str,
    timestamp: str,
) -> str:
    """Compute the sha256 hash that seals a ledger block."""
    payload = {
        "index": index,
        "entry_id": entry_id,
        "action": action,
        "entry_hash": entry_hash,
        "previous_hash": previous_hash,
        "timestamp": timestamp,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_ledger_block(
    entry: Optional[Entry],
    previous_hash: str,
    index: int,
    action: str = "append",
) -> LedgerBlock:
    """Create a sealed ledger block chained to ``previous_hash``."""
    timestamp = datetime.now(timezone.utc).isoformat()
    entry_hash = compute_entry_hash(entry) if entry is not None else ""
    entry_id = entry.id if entry is not None else None

    block_hash = _compute_block_hash(
        index=index,
        entry_id=entry_id,
        action=action,
        entry_hash=entry_hash,
        previous_hash=previous_hash,
        timestamp=timestamp,
    )

    return LedgerBlock(
        id=str(uuid.uuid4()),
        index=index,
        entry_id=entry_id,
        action=action,
        entry_hash=entry_hash,
        previous_hash=previous_hash,
        hash=block_hash,
        timestamp=timestamp,
    )


def append_ledger_block(
    ledger: List[LedgerBlock],
    entry: Optional[Entry],
    action: str = "append",
) -> LedgerBlock:
    """Append a new block to ``ledger`` and return it."""
    previous_hash = ledger[-1].hash if ledger else GENESIS_HASH
    index = len(ledger)
    block = create_ledger_block(entry, previous_hash, index, action)
    ledger.append(block)
    return block


def verify_ledger_chain(
    ledger: List[LedgerBlock],
    entries: Optional[List[Entry]] = None,
) -> Tuple[bool, List[str]]:
    """Verify the integrity of the ledger hash chain.

    Returns a tuple ``(valid, warnings)``.
    """
    warnings: List[str] = []

    if not ledger:
        return True, ["Ledger is empty."]

    entry_hash_by_id = {}
    if entries is not None:
        entry_hash_by_id = {e.id: compute_entry_hash(e) for e in entries}

    previous_hash = GENESIS_HASH
    valid = True

    for expected_index, block in enumerate(ledger):
        if block.index != expected_index:
            valid = False
            warnings.append(
                f"Block {block.id} has index {block.index}, expected {expected_index}."
            )

        if block.previous_hash != previous_hash:
            valid = False
            warnings.append(
                f"Block {block.index} previous_hash does not match prior block."
            )

        recomputed = _compute_block_hash(
            index=block.index,
            entry_id=block.entry_id,
            action=block.action,
            entry_hash=block.entry_hash,
            previous_hash=block.previous_hash,
            timestamp=block.timestamp,
        )
        if recomputed != block.hash:
            valid = False
            warnings.append(f"Block {block.index} hash is invalid (tampering?).")

        if entries is not None and block.entry_id is not None:
            expected_entry_hash = entry_hash_by_id.get(block.entry_id)
            if expected_entry_hash is None:
                warnings.append(
                    f"Block {block.index} references missing entry {block.entry_id}."
                )
            elif expected_entry_hash != block.entry_hash:
                valid = False
                warnings.append(
                    f"Entry {block.entry_id} content does not match its ledger hash."
                )

        previous_hash = block.hash

    return valid, warnings
