"""Policy derivation and enforcement for memory entries."""

from __future__ import annotations

import re
from typing import List, Optional

from app.models.pack_schema import Classification, Entry, Policy


def classification_to_policy(classification: Classification) -> Policy:
    """Derive a :class:`Policy` from a classification level.

    Rules:
        - PUBLIC / SHAREABLE: freely exportable and shareable.
        - INTERNAL: usable and exportable, but not shareable externally.
        - CONFIDENTIAL: usable for query and LLM, redacted on export,
          excluded from shareable exports unless explicitly allowed.
        - SECRET: usable for local query, never sent to external LLM,
          never exported unless explicitly allowed, always redacted.
        - EPHEMERAL: usable for query but never exported.
    """
    c = classification

    if c == Classification.PUBLIC:
        return Policy(
            classification=c,
            exportable=True,
            shareable=True,
            usable_for_query=True,
            send_to_llm=True,
            redact_on_export=False,
        )
    if c == Classification.SHAREABLE:
        return Policy(
            classification=c,
            exportable=True,
            shareable=True,
            usable_for_query=True,
            send_to_llm=True,
            redact_on_export=False,
        )
    if c == Classification.INTERNAL:
        return Policy(
            classification=c,
            exportable=True,
            shareable=False,
            usable_for_query=True,
            send_to_llm=True,
            redact_on_export=False,
        )
    if c == Classification.CONFIDENTIAL:
        return Policy(
            classification=c,
            exportable=False,
            shareable=False,
            usable_for_query=True,
            send_to_llm=True,
            redact_on_export=True,
        )
    if c == Classification.SECRET:
        return Policy(
            classification=c,
            exportable=False,
            shareable=False,
            usable_for_query=True,
            send_to_llm=False,
            redact_on_export=True,
        )
    if c == Classification.EPHEMERAL:
        return Policy(
            classification=c,
            exportable=False,
            shareable=False,
            usable_for_query=True,
            send_to_llm=True,
            redact_on_export=True,
        )

    # Safe default: treat unknown as INTERNAL.
    return Policy(classification=Classification.INTERNAL)


def can_use_entry_for_query(
    entry: Entry,
    allowed_classifications: Optional[List[Classification]] = None,
) -> bool:
    """Return True if ``entry`` may be used to answer a query."""
    policy = entry.policy or classification_to_policy(entry.classification)
    if not policy.usable_for_query:
        return False
    if allowed_classifications is not None:
        if entry.classification not in allowed_classifications:
            return False
    return True


def can_send_entry_to_llm(entry: Entry) -> bool:
    """Return True if the entry text may be sent to an external LLM.

    SECRET entries must never leave the machine via an LLM call.
    """
    policy = entry.policy or classification_to_policy(entry.classification)
    return policy.send_to_llm


def can_export_entry(
    entry: Entry,
    allowed_classifications: Optional[List[Classification]] = None,
) -> bool:
    """Return True if the entry may be included in an export.

    CONFIDENTIAL and SECRET entries are only exported when explicitly
    listed in ``allowed_classifications``.
    """
    c = entry.classification
    restricted = {Classification.CONFIDENTIAL, Classification.SECRET}

    if c == Classification.EPHEMERAL:
        # Ephemeral data is never exported.
        return False

    if c in restricted:
        # Require explicit opt-in.
        if allowed_classifications is None or c not in allowed_classifications:
            return False
        return True

    if allowed_classifications is not None:
        return c in allowed_classifications

    policy = entry.policy or classification_to_policy(c)
    return policy.exportable


_SENSITIVE_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b(?:\+?\d[\d\-\s]{7,}\d)\b"),  # phone-ish
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),  # card-ish
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*\S+"),
]


def redact_text_if_needed(text: str, entry: Entry) -> str:
    """Redact sensitive substrings when the entry policy requires it."""
    policy = entry.policy or classification_to_policy(entry.classification)
    if not policy.redact_on_export:
        return text

    redacted = text
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
