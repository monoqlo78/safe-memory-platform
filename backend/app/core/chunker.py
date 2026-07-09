"""Simple, dependency-free text chunker."""

from __future__ import annotations

from typing import List


def chunk_text(text: str, max_chars: int = 900, overlap: int = 120) -> List[str]:
    """Split ``text`` into overlapping chunks.

    Attempts to break on paragraph or sentence boundaries near ``max_chars``
    to keep chunks readable, then falls back to hard slicing.

    Args:
        text: The source text to chunk.
        max_chars: Maximum characters per chunk.
        overlap: Number of characters shared between consecutive chunks.

    Returns:
        A list of non-empty text chunks.
    """
    text = (text or "").strip()
    if not text:
        return []

    if max_chars <= 0:
        return [text]

    overlap = max(0, min(overlap, max_chars - 1))

    chunks: List[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + max_chars, length)

        if end < length:
            window = text[start:end]
            # Prefer a clean break point close to the end of the window.
            break_at = max(
                window.rfind("\n\n"),
                window.rfind(". "),
                window.rfind("\n"),
            )
            # Only honor the break if it is reasonably far into the window.
            if break_at >= int(max_chars * 0.5):
                end = start + break_at + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= length:
            break

        start = max(end - overlap, start + 1)

    return chunks
