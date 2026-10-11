"""Every router the app mounts. A router missing from this list is not served; a route without a policy fails startup (check_route_policies)."""

from fastapi import APIRouter

from kg_api.routers import commands, entities, health, identity, interactions, ontology, operations, operations_jobs, relationships

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
]
