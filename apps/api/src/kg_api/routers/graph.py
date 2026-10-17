"""Diagnostic graph search (P4; P6 builds context on the same path): Graphiti's hybrid search through the platform recipe, joined to Postgres."""

from __future__ import annotations

from fastapi import APIRouter
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_knowledge.models import GraphSearchIn, GraphSearchOut

from kg_api.deps import DbDep, PrincipalDep, StateDep, SubjectsDep, WorkspaceDep

router = APIRouter(prefix="/v1/graph", tags=["graph"])


@router.post("/search", response_model=GraphSearchOut)
@requires(Action.GRAPH_SEARCH)
async def graph_search(
    body: GraphSearchIn, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep, subjects: SubjectsDep
) -> GraphSearchOut:
    """Only facts Postgres holds as active (or, with known_at, as they were) come back, each with its status, authority and evidence. A graph hit the platform
    has not reconciled yet is dropped and counted in `unreconciled_hits`."""
    return await state.knowledge.search.run(
        db, ws, body, can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE), subjects=subjects
    )
