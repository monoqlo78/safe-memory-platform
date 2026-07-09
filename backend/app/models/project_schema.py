"""Pydantic models for Memory Workspace project runs."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from app.models.pack_schema import Classification


class ProjectRunRequest(BaseModel):
    project_id: str
    agent_id: str
    task: str
    pack_paths: List[str] = Field(default_factory=list)
    top_k: int = 12


class UsedMemory(BaseModel):
    entry_id: str
    pack_id: str
    classification: Classification
    score: float


class SuggestedMemory(BaseModel):
    text: str
    suggested_classification: Classification = Classification.INTERNAL


class ProjectRunResponse(BaseModel):
    project_id: str
    output: str
    used_memory_ids: List[str]
    used_memories: List[UsedMemory]
    suggested_new_memories: List[SuggestedMemory]
    fallback_used: bool = False
