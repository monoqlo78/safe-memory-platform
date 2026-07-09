"""OpenAI-compatible client for Qwen Cloud with safe local fallbacks.

If Qwen credentials are missing or a call fails, deterministic fallbacks
are used so that demos never crash. Fallbacks are clearly flagged.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Dict, List, Optional

from app.config import settings
from app.models.pack_schema import Classification

logger = logging.getLogger("safe_memory.qwen")

FALLBACK_EMBEDDING_DIM = 128

# Keyword heuristics for the classification fallback.
_SECRET_TERMS = [
    "password",
    "api key",
    "api_key",
    "secret",
    "private key",
    "ssn",
    "social security",
    "credit card",
    "bank account",
    "seed phrase",
]
_CONFIDENTIAL_TERMS = [
    "confidential",
    "salary",
    "medical",
    "diagnosis",
    "contract",
    "nda",
    "invoice",
    "tax return",
    "my number",
    "passport",
    "internal only",
]
_PUBLIC_TERMS = [
    "public",
    "press release",
    "announcement",
    "blog post",
    "documentation",
]


class QwenClient:
    """Thin wrapper over the OpenAI-compatible Qwen Cloud API."""

    def __init__(self) -> None:
        self._client = None
        self._enabled = settings.has_qwen_credentials
        if self._enabled:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=settings.qwen_api_key,
                    base_url=settings.qwen_base_url,
                    timeout=settings.qwen_timeout_seconds,
                    max_retries=settings.qwen_max_retries,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to initialize Qwen client: %s", type(exc).__name__)
                self._client = None
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> Optional[str]:
        """Call the Qwen chat model. Returns None on failure."""
        if not self.enabled:
            return None
        try:
            response = self._client.chat.completions.create(
                model=settings.qwen_chat_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("Qwen chat_completion failed: %s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------
    def embed_text(self, text: str) -> List[float]:
        """Embed a single string, falling back to a deterministic vector."""
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Embed a list of strings.

        Uses Qwen embeddings when available; otherwise a deterministic
        hash-based fallback vector of length ``FALLBACK_EMBEDDING_DIM``.
        """
        if not texts:
            return []

        if self.enabled:
            try:
                response = self._client.embeddings.create(
                    model=settings.qwen_embedding_model,
                    input=texts,
                )
                # Preserve input order.
                ordered = sorted(response.data, key=lambda d: d.index)
                return [list(d.embedding) for d in ordered]
            except Exception as exc:
                logger.warning("Qwen embed_texts failed: %s", type(exc).__name__)

        return [self._fallback_embedding(t) for t in texts]

    @staticmethod
    def _fallback_embedding(text: str) -> List[float]:
        """Deterministic hash-based embedding of length 128.

        Not semantically meaningful, but stable and non-zero so that
        cosine similarity and the demo pipeline keep working offline.
        """
        text = (text or "").lower()
        vec = [0.0] * FALLBACK_EMBEDDING_DIM

        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            tokens = [text or "empty"]

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for i in range(FALLBACK_EMBEDDING_DIM):
                # Use two hash bytes per dimension for a signed contribution.
                b = digest[i % len(digest)]
                sign = 1.0 if (digest[(i + 1) % len(digest)] & 1) else -1.0
                vec[i] += sign * (b / 255.0)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    def classify_text(
        self,
        text: str,
        default: Classification = Classification.INTERNAL,
    ) -> Classification:
        """Classify text confidentiality using Qwen, else a heuristic."""
        if self.enabled:
            classified = self._classify_with_llm(text)
            if classified is not None:
                return classified
        return self._classify_heuristic(text, default)

    def _classify_with_llm(self, text: str) -> Optional[Classification]:
        levels = ", ".join(c.value for c in Classification)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a data confidentiality classifier. "
                    f"Respond with exactly one label from: {levels}. "
                    "Return only the label, nothing else."
                ),
            },
            {"role": "user", "content": text[:4000]},
        ]
        raw = self.chat_completion(messages, temperature=0.0, max_tokens=10)
        if not raw:
            return None
        label = raw.strip().upper().split()[0].strip(".,:")
        try:
            return Classification(label)
        except ValueError:
            return None

    @staticmethod
    def _classify_heuristic(
        text: str,
        default: Classification = Classification.INTERNAL,
    ) -> Classification:
        lowered = (text or "").lower()
        if any(term in lowered for term in _SECRET_TERMS):
            return Classification.SECRET
        if any(term in lowered for term in _CONFIDENTIAL_TERMS):
            return Classification.CONFIDENTIAL
        if any(term in lowered for term in _PUBLIC_TERMS):
            return Classification.PUBLIC
        return default

    @staticmethod
    def has_secret_terms(text: str) -> bool:
        """True when the text contains obvious secret markers (no LLM call).

        Used to *promote* an entry to SECRET even when a caller supplied a
        lower default classification, so a blanket "INTERNAL" never leaks an
        obvious password / private key.
        """
        lowered = (text or "").lower()
        return any(term in lowered for term in _SECRET_TERMS)

    # ------------------------------------------------------------------
    # Answering with context
    # ------------------------------------------------------------------
    def answer_with_context(
        self,
        query: str,
        memory_entries: List[Dict[str, str]],
    ) -> Dict[str, object]:
        """Answer a query using only the provided memory entries.

        ``memory_entries`` is a list of dicts with ``id`` and ``text``.
        Returns ``{"answer": str, "fallback_used": bool}``.
        """
        if not memory_entries:
            return {
                "answer": "No usable memory entries were available to answer this query.",
                "fallback_used": True,
            }

        context_blocks = "\n\n".join(
            f"[{i + 1}] (id={m['id']}) {m['text']}"
            for i, m in enumerate(memory_entries)
        )

        if self.enabled:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a careful assistant. Answer the user's question "
                        "using ONLY the provided memory entries. If the answer is not "
                        "in the memory, say so. Do not invent facts. Cite entries by "
                        "their id when relevant. Answer concisely, ideally under 150 "
                        "words. Prefer a short direct answer with brief bullet points "
                        "only if needed."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {query}\n\n"
                        f"Memory entries:\n{context_blocks}"
                    ),
                },
            ]
            answer = self.chat_completion(
                messages, max_tokens=settings.answer_max_tokens
            )
            if answer:
                return {"answer": answer, "fallback_used": False}

        return {
            "answer": self._answer_fallback(query, memory_entries),
            "fallback_used": True,
        }

    @staticmethod
    def _answer_fallback(query: str, memory_entries: List[Dict[str, str]]) -> str:
        """Summarize selected entries without an LLM."""
        lines = [
            "[FALLBACK ANSWER - Qwen was unavailable, showing raw relevant memory]",
            f"Question: {query}",
            "",
            "Most relevant memory entries:",
        ]
        for i, m in enumerate(memory_entries[:5], start=1):
            snippet = (m.get("text") or "").strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            lines.append(f"{i}. (id={m.get('id')}) {snippet}")
        lines.append("")
        lines.append("WARNING: This is a heuristic fallback, not a generated answer.")
        return "\n".join(lines)


# Module-level singleton.
qwen_client = QwenClient()
