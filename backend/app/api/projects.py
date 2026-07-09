"""Memory Workspace project run endpoint."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from app.core import pack_io
from app.core.pack_io import UnsafePathError
from app.core.policy import can_send_entry_to_llm, can_use_entry_for_query
from app.core.qwen_client import qwen_client
from app.core.search import hybrid_search
from app.models.pack_schema import Classification, Entry, get_retrieval_text
from app.models.project_schema import (
    ProjectRunRequest,
    ProjectRunResponse,
    SuggestedMemory,
    UsedMemory,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post(
    "/run",
    response_model=ProjectRunResponse,
    operation_id="runProjectWithMemory",
    summary="Run a project task using memory packs",
    description=(
        "Complete a project task using the most relevant memory drawn from one "
        "or more Safe Memory Packs. SECRET entries are never sent to the LLM. "
        "Returns the generated output, the memory used, and suggested new "
        "memories to capture."
    ),
)
def run_project(req: ProjectRunRequest) -> ProjectRunResponse:
    """Run a project task using relevant memory from one or more packs."""
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="task must not be empty.")
    if not req.pack_paths:
        raise HTTPException(status_code=400, detail="pack_paths must not be empty.")

    query_embedding = qwen_client.embed_text(req.task)

    scored: List[tuple[Entry, str, float]] = []
    for pack_path in req.pack_paths:
        try:
            path = pack_io.ensure_safe_path(pack_path)
            pack = pack_io.load_pack(path)
        except UnsafePathError:
            raise HTTPException(status_code=400, detail="Unsafe pack path rejected.")
        except FileNotFoundError:
            # Skip missing packs rather than aborting the whole run.
            continue

        usable = [e for e in pack.entries if can_use_entry_for_query(e)]
        ranked = hybrid_search(req.task, query_embedding, usable, top_k=req.top_k)
        for entry, score in ranked:
            scored.append((entry, pack.manifest.pack_id, score))

    scored.sort(key=lambda t: t[2], reverse=True)
    top = scored[: req.top_k]

    used_memories: List[UsedMemory] = []
    llm_entries = []
    for entry, pack_id, score in top:
        used_memories.append(
            UsedMemory(
                entry_id=entry.id,
                pack_id=pack_id,
                classification=entry.classification,
                score=score,
            )
        )
        if can_send_entry_to_llm(entry):
            llm_entries.append({"id": entry.id, "text": get_retrieval_text(entry)})

    output, fallback_used = _generate_output(req.task, llm_entries)
    suggestions = _suggest_memories(req.task, output)

    return ProjectRunResponse(
        project_id=req.project_id,
        output=output,
        used_memory_ids=[m.entry_id for m in used_memories],
        used_memories=used_memories,
        suggested_new_memories=suggestions,
        fallback_used=fallback_used,
    )


def _generate_output(task: str, llm_entries: list) -> tuple[str, bool]:
    """Produce a project output using Qwen, with a safe fallback."""
    if qwen_client.enabled and llm_entries:
        context = "\n\n".join(
            f"[{i + 1}] (id={m['id']}) {m['text']}"
            for i, m in enumerate(llm_entries)
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a project agent. Complete the task using ONLY the "
                    "provided memory entries. Be concrete and actionable. Do not "
                    "fabricate facts that are not supported by the memory."
                ),
            },
            {
                "role": "user",
                "content": f"Task: {task}\n\nRelevant memory:\n{context}",
            },
        ]
        answer = qwen_client.chat_completion(messages)
        if answer:
            return answer, False

    result = qwen_client.answer_with_context(task, llm_entries)
    return str(result["answer"]), True


def _suggest_memories(task: str, output: str) -> List[SuggestedMemory]:
    """Suggest new memory entries derived from the produced output."""
    suggestions: List[SuggestedMemory] = []
    snippet = output.strip()
    if snippet:
        first_line = snippet.splitlines()[0][:280]
        suggestions.append(
            SuggestedMemory(
                text=f"Outcome for task '{task[:80]}': {first_line}",
                suggested_classification=Classification.INTERNAL,
            )
        )
    return suggestions
