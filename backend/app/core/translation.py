"""Japanese -> English translation and normalization for demo knowledge.

Uses the Qwen chat model when credentials are available; otherwise returns a
clearly-labeled fallback so the demo never fails. Batched helpers translate many
rows in a single chat call for large Excel imports.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, List

from app.core.qwen_client import qwen_client

logger = logging.getLogger("safe_memory.translation")

FALLBACK_PREFIX = "[UNTRANSLATED FALLBACK] "

_TRANSLATION_SYSTEM_PROMPT = (
    "You are translating Japanese accounting, tax, and CSV import knowledge for "
    "an international hackathon demo. Translate into clear, concise English. "
    "Preserve product names such as freee, Money Forward Cloud, Yayoi, and "
    "Japanese accounting terms when needed. Preserve CSV column names when "
    "important. Do not invent facts. Return only the English translation."
)

_NORMALIZE_SYSTEM_PROMPT = (
    "You are normalizing Japanese accounting and tax knowledge into clean, "
    "canonical English knowledge for an international hackathon demo. Produce a "
    "concise, well-structured English statement of the underlying accounting or "
    "tax concept. Preserve product names (freee, Money Forward Cloud, Yayoi) and "
    "relevant CSV column names. Do not invent facts. Return only the English text."
)

# Appended to the system prompt for batched calls to enforce strict alignment.
_BATCH_PROTOCOL = (
    "\n\nYou will receive several numbered Japanese items, one per line, like "
    "'1. ...', '2. ...'. Translate EACH item independently and return the SAME "
    "number of numbered lines, one output per input, in the same order. Use the "
    "format '<number>. <english>' with exactly one line per item. Do NOT merge, "
    "split, reorder, drop, or add items. Do not add commentary, headers, or blank "
    "lines between items."
)

# Matches '1. text', '1) text', or '1: text'.
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*[.):]\s?(.*)$")


def translate_to_english(text: str) -> str:
    """Translate Japanese ``text`` to English.

    Returns a fallback string prefixed with ``[UNTRANSLATED FALLBACK]`` when
    Qwen is unavailable or the call fails.
    """
    text = (text or "").strip()
    if not text:
        return ""

    if qwen_client.enabled:
        answer = qwen_client.chat_completion(
            [
                {"role": "system", "content": _TRANSLATION_SYSTEM_PROMPT},
                {"role": "user", "content": text[:6000]},
            ],
            temperature=0.0,
            max_tokens=1200,
        )
        if answer:
            return answer.strip()
        logger.warning("Translation fell back (Qwen returned no content).")

    return FALLBACK_PREFIX + text


def normalize_accounting_knowledge_to_english(text: str) -> str:
    """Translate and normalize Japanese accounting knowledge into English.

    Falls back to :func:`translate_to_english` behavior (and ultimately the
    labeled fallback) so the demo never stops.
    """
    text = (text or "").strip()
    if not text:
        return ""

    if qwen_client.enabled:
        answer = qwen_client.chat_completion(
            [
                {"role": "system", "content": _NORMALIZE_SYSTEM_PROMPT},
                {"role": "user", "content": text[:6000]},
            ],
            temperature=0.1,
            max_tokens=1200,
        )
        if answer:
            return answer.strip()
        logger.warning("Normalization fell back (Qwen returned no content).")

    # Fall back to plain translation (which itself has a safe fallback).
    return translate_to_english(text)


# ---------------------------------------------------------------------------
# Batched translation (single Qwen call per batch of rows)
# ---------------------------------------------------------------------------
def _labeled_fallback(texts: List[str]) -> List[str]:
    """Return the labeled fallback for each input, never raising."""
    return [
        (FALLBACK_PREFIX + (t or "").strip()) if (t or "").strip() else ""
        for t in texts
    ]


def _flatten_for_numbering(text: str) -> str:
    """Collapse a multi-line record into a single line for the numbered protocol."""
    return " / ".join(
        segment.strip() for segment in (text or "").splitlines() if segment.strip()
    )


def _parse_numbered_response(answer: str, expected: int) -> List[str] | None:
    """Parse a numbered model response into ``expected`` aligned items.

    Returns a list of length ``expected`` on success, or ``None`` if the response
    does not contain exactly one numbered line per input (1..expected).
    """
    if not answer:
        return None
    parsed: dict[int, str] = {}
    for line in answer.splitlines():
        match = _NUMBERED_LINE_RE.match(line)
        if not match:
            continue
        num = int(match.group(1))
        # Keep the first occurrence of each index; ignore stray duplicates.
        parsed.setdefault(num, match.group(2).strip())
    if sorted(parsed.keys()) != list(range(1, expected + 1)):
        return None
    return [parsed[i] for i in range(1, expected + 1)]


def _translate_one_chunk(
    texts: List[str],
    system_prompt: str,
    per_item_fn: Callable[[str], str],
    temperature: float,
) -> List[str]:
    """Translate a single chunk (<= batch_size) in one Qwen call.

    Falls back to per-item translation for THIS chunk only if the batched
    response is misaligned, so alignment is never wrong.
    """
    if not texts:
        return []

    if not qwen_client.enabled:
        return [per_item_fn(t) for t in texts]

    numbered = "\n".join(
        f"{i}. {_flatten_for_numbering(t)[:4000]}" for i, t in enumerate(texts, start=1)
    )
    answer = qwen_client.chat_completion(
        [
            {"role": "system", "content": system_prompt + _BATCH_PROTOCOL},
            {"role": "user", "content": numbered},
        ],
        temperature=temperature,
        max_tokens=max(1200, len(texts) * 256),
    )
    aligned = _parse_numbered_response(answer or "", len(texts))
    if aligned is not None:
        return [line.strip() for line in aligned]

    # Misaligned (or empty) batch response: translate this chunk item-by-item so
    # each output stays aligned to its input.
    logger.warning(
        "Batched translation misaligned (%d inputs); falling back per-item.",
        len(texts),
    )
    return [per_item_fn(t) for t in texts]


def _batch_translate(
    texts: List[str],
    system_prompt: str,
    per_item_fn: Callable[[str], str],
    batch_size: int,
    temperature: float,
) -> List[str]:
    """Translate ``texts`` in chunks of ``batch_size``. Never raises."""
    if not texts:
        return []
    size = max(1, int(batch_size or 1))

    # Preserve original positions; only send non-empty rows to the model.
    results: List[str] = [""] * len(texts)
    payload: List[str] = []
    positions: List[int] = []
    for idx, raw in enumerate(texts):
        stripped = (raw or "").strip()
        if stripped:
            payload.append(stripped)
            positions.append(idx)

    try:
        translated: List[str] = []
        for start in range(0, len(payload), size):
            chunk = payload[start : start + size]
            translated.extend(
                _translate_one_chunk(chunk, system_prompt, per_item_fn, temperature)
            )
    except Exception:  # pragma: no cover - defensive; never lose rows
        logger.warning("Batch translation raised; using labeled fallback.")
        translated = _labeled_fallback(payload)

    for pos, value in zip(positions, translated):
        results[pos] = value
    return results


def translate_batch_to_english(texts: List[str], batch_size: int = 20) -> List[str]:
    """Translate many Japanese strings to English in batched Qwen calls.

    Sends up to ``batch_size`` items per chat call using a strict numbered
    protocol. Returns exactly one output per input, in order. When Qwen is
    disabled/fails, each item becomes ``[UNTRANSLATED FALLBACK] <original>``.
    Never raises and never loses rows.
    """
    return _batch_translate(
        texts,
        _TRANSLATION_SYSTEM_PROMPT,
        translate_to_english,
        batch_size,
        temperature=0.0,
    )


def normalize_accounting_batch(texts: List[str], batch_size: int = 20) -> List[str]:
    """Batched counterpart of :func:`normalize_accounting_knowledge_to_english`.

    Same alignment guarantees as :func:`translate_batch_to_english`, using the
    accounting-normalization prompt and per-item fallback.
    """
    return _batch_translate(
        texts,
        _NORMALIZE_SYSTEM_PROMPT,
        normalize_accounting_knowledge_to_english,
        batch_size,
        temperature=0.1,
    )
