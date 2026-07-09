"""Agent vault catalog endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.core import pack_io
from app.models.pack_schema import CatalogResponse

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get(
    "/{agent_id}/catalog",
    response_model=CatalogResponse,
    operation_id="getAgentCatalog",
    summary="List an agent's Safe Memory Packs",
    description=(
        "Return the catalog of Safe Memory Packs owned by an agent, including "
        "each pack's id, title, classification, and entry count. Use the "
        "returned pack_id values with the query, export, and verify endpoints."
    ),
)
def get_agent_catalog(agent_id: str) -> CatalogResponse:
    """Return the catalog of Safe Memory Packs for an agent."""
    packs = pack_io.scan_agent_catalog(agent_id)
    return CatalogResponse(agent_id=agent_id, packs=packs)
