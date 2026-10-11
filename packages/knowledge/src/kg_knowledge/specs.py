"""Loading ontology specs from the registry. Published and retired versions are immutable (a trigger guarantees it), so they are cached by id."""

from __future__ import annotations

import uuid
from collections import OrderedDict

from kg_common.errors import NotFound
from kg_ontology import OntologySpec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class SpecCache:
    def __init__(self, max_size: int = 256):
        self._max = max_size
        self._items: OrderedDict[tuple[uuid.UUID, uuid.UUID], OntologySpec] = OrderedDict()

    def _put(self, key: tuple[uuid.UUID, uuid.UUID], spec: OntologySpec) -> None:
        self._items[key] = spec
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    async def get(self, conn: AsyncConnection, ws: uuid.UUID, version_id: uuid.UUID) -> OntologySpec:
        """The spec of a published or retired version in this workspace (a draft or another workspace's version is NotFound)."""
        key = (ws, version_id)
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        row = (
            await conn.execute(
                text("SELECT spec FROM ontology_versions WHERE workspace_id = :ws AND id = :id AND state IN ('published','retired')"),
                {"ws": ws, "id": version_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        spec = OntologySpec.model_validate(row.spec)
        self._put(key, spec)
        return spec

    async def active(self, conn: AsyncConnection, ws: uuid.UUID) -> tuple[uuid.UUID, OntologySpec] | None:
        row = (await conn.execute(text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"), {"ws": ws})).first()
        if row is None or row.active_version_id is None:
            return None
        return row.active_version_id, await self.get(conn, ws, row.active_version_id)
