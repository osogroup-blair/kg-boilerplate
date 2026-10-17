"""FastAPI app factory. Lifespan fails fast on an unsafe DB role or an unpoliced route."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from kg_common.settings import Settings, get_settings
from kg_context.services import build_context
from kg_db.engine import assert_role_is_safe, create_app_engine
from kg_domain.envelope import ErrorEnvelope
from kg_extraction.simulation import SimulationService
from kg_governance.authn.jwt import LocalSigner
from kg_governance.authn.oidc import OidcConfig, OidcVerifier
from kg_governance.authn.resolver import Authenticator
from kg_governance.authz.policy import unpoliced_routes
from kg_governance.limiter import SharedLimiter
from kg_governance.quotas import Quotas
from kg_governance.runtime import build_invalidator, namespace_overrides
from kg_governance.settings import SettingsService
from kg_graph.service import GraphService
from kg_ingestion.services import build_ingestion
from kg_knowledge.services import build_knowledge
from kg_memory.deletion import build_deletion
from kg_memory.services import build_memory
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import build_router_stack
from kg_obs import metrics
from kg_obs.logging import configure_logging, get_logger
from kg_obs.tracing import setup_tracing
from kg_reconcile import Reconcile
from prometheus_client import start_http_server

from kg_api.deps import guard
from kg_api.errors import install_error_handlers
from kg_api.middleware import MaintenanceMiddleware, RequestMiddleware
from kg_api.routers import ALL_ROUTERS
from kg_api.state import AppState

API_VERSION = "0.1.0"
_metrics_started = threading.Event()
log = get_logger("kg.api")

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope, "description": desc}
    for code, desc in {
        401: "Unauthenticated",
        403: "Forbidden",
        404: "Not found",
        413: "Payload too large",
        422: "Validation failed",
        500: "Internal error",
        503: "Dependency unavailable",
    }.items()
}


def _unique_id(route: APIRoute) -> str:
    return route.name


def all_routes(app: FastAPI) -> list[Any]:
    """FastAPI (0.14x) wraps included routers lazily, so enumerate our own router list plus any route added directly to the app."""
    direct = [r for r in app.routes if hasattr(r, "endpoint")]
    return direct + [route for router in ALL_ROUTERS for route in router.routes]


def check_route_policies(app: FastAPI) -> None:
    missing = unpoliced_routes(all_routes(app))
    if missing:
        raise RuntimeError(f"routes without an authorization policy: {missing}")


def build_app(settings: Settings, *, serve_metrics: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, service="kg-api", json_output=True)
        setup_tracing("kg-api", settings.otel_exporter_otlp_endpoint)
        engine = create_app_engine(settings)
        await assert_role_is_safe(engine)  # L1: refuse to start if RLS could be bypassed
        sink = DbCallSink(engine)
        router = build_router_stack(settings, service="kg-api", sink=sink)
        graph = GraphService(settings, router.transport, router.config, namespace_loader=lambda: namespace_overrides(engine))
        try:
            await graph.namespaces.refresh()
        except Exception:
            log.warning("graph_namespaces_unreadable_at_startup")  # the default namespace until the first refresh succeeds
        shared = SharedLimiter(engine) if settings.limiter_backend == "postgres" else None
        try:
            ok = await graph.ping()
        except Exception:
            ok = False
        metrics.dependency_up.labels(dependency="neo4j").set(1 if ok else 0)
        if not ok:
            log.warning("neo4j_unreachable_at_startup")  # readiness reports it; the API still starts (no index build here, L12)
        sink.start()
        signer = LocalSigner(settings.auth_jwt_secret.get_secret_value(), settings.auth_jwt_ttl_seconds)
        oidc = None
        if settings.auth_oidc_enabled:
            oidc = OidcVerifier(
                OidcConfig(
                    issuer=settings.oidc_issuer,
                    audience=settings.oidc_audience or settings.oidc_client_id,
                    algorithms=tuple(a.strip() for a in settings.oidc_algorithms.split(",") if a.strip()),
                    groups_claim=settings.oidc_groups_claim,
                    workspace_claim=settings.oidc_workspace_claim,
                    jwks_url=settings.oidc_jwks_url,
                    jwks_ttl_seconds=settings.oidc_jwks_ttl_seconds,
                )
            )
        authenticator = Authenticator(
            engine,
            pepper=settings.auth_key_pepper.get_secret_value(),
            signer=signer,
            oidc=oidc,
            production=settings.is_production,
            allow_breakglass=settings.allow_breakglass_keys,
        )
        knowledge = build_knowledge(engine, graph, router.config, service="kg-api", settings=settings)
        memory = build_memory(settings, knowledge, router.gateway, router.config, service="kg-api")
        ingestion = build_ingestion(settings)
        deletion = build_deletion(settings, engine, knowledge, graph, ingestion.cipher, ingestion.hasher, memory.writer)
        context = build_context(
            settings,
            engine,
            knowledge,
            memory,
            router.gateway,
            router.config,
            ingestion.cipher,
            ingestion.hasher,
            service="kg-api",
            shared_limiter=shared,
        )
        quotas = Quotas(
            engine,
            default_requests_per_minute=settings.principal_requests_per_minute,
            flush_seconds=settings.principal_usage_flush_seconds,
            shared=shared,
        )
        quotas.start()
        invalidator = build_invalidator(settings, egress=router.narrowing, quotas=quotas, namespaces=graph.namespaces)
        if invalidator is not None:
            invalidator.start()
        app.state.kg = AppState(
            settings=settings,
            engine=engine,
            authenticator=authenticator,
            router=router,
            call_sink=sink,
            graph=graph,
            knowledge=knowledge,
            ingestion=ingestion,
            memory=memory,
            context=context,
            quotas=quotas,
            reconcile=Reconcile(engine, knowledge, graph, embedding_version=settings.memory_embedding_version),
            shared_limiter=shared,
            invalidator=invalidator,
            deletion=deletion,
            settings_service=SettingsService(list(router.config.egress.denied_classifications)),
            simulation=SimulationService(
                engine,
                graph,
                knowledge.specs,
                knowledge.ontology,
                ingestion.cipher,
                router.config,
                settings,
                flush_calls=sink.flush,
                service="kg-api",
            ),
        )
        check_route_policies(app)
        if serve_metrics and not _metrics_started.is_set():
            start_http_server(settings.metrics_port_api, registry=metrics.REGISTRY)
            _metrics_started.set()
        log.info("api_started", env=settings.env, router_mode=settings.oso_router_mode)
        try:
            yield
        finally:
            if invalidator is not None:
                await invalidator.aclose()
            if oidc is not None:
                await oidc.aclose()
            await quotas.aclose()
            await sink.aclose()
            await graph.aclose()
            await router.aclose()
            await engine.dispose()

    production = settings.is_production
    app = FastAPI(
        title="Knowledge & Memory Platform API",
        version=API_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(guard)],
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
        generate_unique_id_function=_unique_id,
        responses=ERROR_RESPONSES,
        description="Versioned /v1 API. Every route declares an authorization policy; errors use one envelope.",
    )
    app.add_middleware(MaintenanceMiddleware, mode=settings.maintenance_mode)
    app.add_middleware(RequestMiddleware, max_body_bytes=settings.max_body_bytes)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["authorization", "content-type", "idempotency-key", "if-match"],
            expose_headers=["etag", "location", "idempotent-replay", "x-request-id"],
        )
    install_error_handlers(app)
    for r in ALL_ROUTERS:
        app.include_router(r)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        comps = schema.setdefault("components", {})
        comps.setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "API key (kgk_…) or short-lived JWT",
        }
        comps.setdefault("parameters", {})["IdempotencyKey"] = {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "maxLength": 255},
            "description": "Required on every mutation of the ontology, entity, relationship, command, interaction and operations APIs. A repeat with the same body replays the stored result; the same key with a different body is 409 idempotency_mismatch.",
        }
        for path, ops in schema.get("paths", {}).items():
            for op in ops.values():
                if path.startswith("/v1") and path != "/v1/auth/token":
                    op["security"] = [{"bearerAuth": []}]
                elif path == "/v1/auth/token":
                    op["security"] = [{"bearerAuth": []}]
                else:
                    op["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def create_app() -> FastAPI:
    """uvicorn --factory entrypoint."""
    return build_app(get_settings(), serve_metrics=True)
