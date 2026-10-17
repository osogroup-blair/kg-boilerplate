"""Every router the app mounts. A router missing from this list is not served; a route without a policy fails startup (check_route_policies)."""

from fastapi import APIRouter

from kg_api.routers import (
    commands,
    context,
    corrections,
    deletion,
    entities,
    governance,
    graph,
    health,
    identity,
    interactions,
    memories,
    ontology,
    operations,
    operations_jobs,
    principals,
    relationships,
    reviews,
)

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    identity.router,
    operations.router,
    ontology.router,
    entities.router,
    relationships.router,
    commands.router,
    interactions.router,
    operations_jobs.router,
    reviews.router,
    graph.router,
    memories.router,
    memories.scopes_router,
    context.router,
    context.usage_router,
    principals.router,
    principals.me_router,
    corrections.router,
    deletion.router,
    deletion.holds_router,
    deletion.supp_router,
    governance.router,
]
