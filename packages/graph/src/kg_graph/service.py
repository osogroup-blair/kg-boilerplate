"""GraphService: owns the (private) Graphiti instance and the Neo4j driver. Hands out ScopedGraph facades, never the raw object."""

from __future__ import annotations

import re

from graphiti_core import Graphiti
from kg_common.settings import Settings
from kg_domain.principal import WorkspaceContext
from kg_modelgw.config import ModelConfig
from kg_modelgw.transport import RouterTransport
from kg_obs.logging import get_logger
from neo4j.exceptions import ClientError

from kg_graph.factory import build_graphiti
from kg_graph.scoped import ScopedGraph

log = get_logger("kg.graph")

# Graphiti MERGEs nodes and edges on `uuid` alone and creates no uniqueness constraint, and MERGE without one is not atomic: two writers saving the same uuid
# at once create TWO nodes (found in P3, when the outbox started projecting a row while the request's own fast path did the same). The constraints make
# the second MERGE wait and match instead; a rare constraint violation under contention is retryable (kg_knowledge.projection.classify_error).
# Graphiti already creates a plain RANGE index on each of these properties, and Neo4j will not create a constraint over an existing index, so that index is
# dropped first (the constraint brings its own backing index of the same kind, which makes Graphiti's `CREATE INDEX IF NOT EXISTS` a no-op afterwards).
UNIQUENESS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("kg_entity_uuid", "CREATE CONSTRAINT kg_entity_uuid IF NOT EXISTS FOR (n:Entity) REQUIRE n.uuid IS UNIQUE", "NODE", ("Entity",)),
    (
        "kg_relates_to_uuid",
        "CREATE CONSTRAINT kg_relates_to_uuid IF NOT EXISTS FOR ()-[e:RELATES_TO]-() REQUIRE e.uuid IS UNIQUE",
        "RELATIONSHIP",
        ("RELATES_TO",),
    ),
)
_IDENT = re.compile(r"^[A-Za-z0-9_]{1,128}$")


class GraphService:
    def __init__(self, settings: Settings, transport: RouterTransport, config: ModelConfig):
        self._graphiti: Graphiti = build_graphiti(settings, transport, config)

    @property
    def _driver(self):  # type: ignore[no-untyped-def]
        return self._graphiti.driver

    async def ping(self) -> bool:
        records, _, _ = await self._driver.client.execute_query("RETURN 1 AS ok", database_=self._driver._database)
        return bool(records and records[0]["ok"] == 1)

    async def ensure_indices(self) -> None:
        """Build range + fulltext indices once (called by the worker at startup; the API never builds indices, L12)."""
        try:
            await self._graphiti.build_indices_and_constraints()
        except ClientError as exc:
            if "already exists" not in str(exc).lower() and "equivalent" not in str(exc).lower():
                raise
            log.info("graph_indices_already_present")
        await self._ensure_uniqueness()

    async def _ensure_uniqueness(self) -> None:
        client, database = self._driver.client, self._driver._database
        for name, create, entity_type, labels in UNIQUENESS:
            records, _, _ = await client.execute_query(
                "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, owningConstraint "
                "WHERE type = 'RANGE' AND entityType = $et AND labelsOrTypes = $lt AND properties = ['uuid'] AND owningConstraint IS NULL RETURN name",
                parameters_={"et": entity_type, "lt": list(labels)},
                database_=database,
            )
            for record in records:
                index = str(record["name"])
                if _IDENT.match(index):  # the name comes from the database, but it is interpolated into DDL: accept plain identifiers only
                    await client.execute_query(f"DROP INDEX {index} IF EXISTS", database_=database)
            try:
                await client.execute_query(create, database_=database)
            except ClientError as exc:
                if "already exists" in str(exc).lower() or "equivalent" in str(exc).lower():
                    continue
                # existing duplicates block the constraint: report it loudly (code only) instead of hiding it; the projection runbook's rebuild clears them
                log.error("graph_uniqueness_constraint_failed", constraint=name, error_code=getattr(exc, "code", "unknown"))
                raise

    def scoped(self, ctx: WorkspaceContext) -> ScopedGraph:
        return ScopedGraph(self._graphiti, ctx)

    async def aclose(self) -> None:
        await self._graphiti.close()  # type: ignore[no-untyped-call]
