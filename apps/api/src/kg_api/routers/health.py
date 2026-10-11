from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from kg_db.session import system_session
from kg_domain.api import LiveOut, ReadyOut
from kg_governance.authz.policy import public
from kg_governance.repositories.reads import worker_status
from kg_modelgw.health import RouterHealth, router_health
from kg_obs import metrics
from sqlalchemy import text

from kg_api.deps import StateDep

router = APIRouter(tags=["health"])


async def check_postgres(state: StateDep) -> str:
    try:
        async with system_session(state.engine) as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "down"


async def check_neo4j(state: StateDep) -> str:
    try:
        return "ok" if await state.graph.ping() else "down"
    except Exception:
        return "down"


async def check_router(state: StateDep) -> RouterHealth:
    cached = state.cached_router_health()
    if cached is not None:
        return cached
    health = await router_health(state.settings.router_origin, state.router.http, state.router.config)
    state.store_router_health(health)
    return health


async def check_worker(state: StateDep) -> str:
    try:
        async with system_session(state.engine) as conn:
            return (await worker_status(conn, state.settings.worker_stale_seconds)).status
    except Exception:
        return "none"


@router.get("/health", response_model=LiveOut)
@public("liveness probe; makes no dependency calls")
async def live() -> LiveOut:
    return LiveOut()


@router.get("/health/ready", response_model=ReadyOut, responses={503: {"model": ReadyOut}})
@public("readiness probe; reports dependency state only (no versions or hostnames)")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state.kg
    postgres, neo4j = await check_postgres(state), await check_neo4j(state)
    router_h, worker = await check_router(state), await check_worker(state)
    metrics.dependency_up.labels(dependency="postgres").set(1 if postgres == "ok" else 0)
    metrics.dependency_up.labels(dependency="neo4j").set(1 if neo4j == "ok" else 0)
    metrics.dependency_up.labels(dependency="router").set(1 if router_h.status == "ok" else 0)
    checks = {"postgres": postgres, "neo4j": neo4j, "router": router_h.status, "worker": worker}
    if postgres != "ok" or neo4j != "ok":
        status, code = "down", 503
    elif router_h.status != "ok" or worker != "ok":
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return JSONResponse(ReadyOut(status=status, checks=checks).model_dump(mode="json"), status_code=code)
