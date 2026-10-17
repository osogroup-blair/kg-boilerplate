"""Legal holds and re-collection suppression (phase-8 plan §5.4.3, §5.4.5, ADR-045).

A HOLD stops a purge, never a freeze: a held target is hidden from every read but not destroyed, the request waits (`blocked_legal_hold`) with a visible exception, and
releasing the hold lets it resume. Only workspace admins create or release one, and both are audited.

A SUPPRESSION is the HMAC of a deleted subject's strong identifiers (external ids). While it stands, an entity carrying the identifier cannot be created again, and an
interaction naming it as a subject is refused (`409 subject_suppressed`): the data a person asked to forget does not come back through the same door. Names are NOT
suppressed (a name is not an identity, KGR-02), and an admin can lift a suppression, audited, when the subject returns with consent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable

from kg_common.errors import Conflict, NotFound, SubjectSuppressed
from kg_common.ids import uuid7
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_governance.audit import AuditMetadata, AuditSink
from kg_governance.deletion.models import LegalHoldIn, LegalHoldOut, SuppressionOut
from kg_governance.deletion.ports import RefHasher


class HoldNotFound(NotFound):
    pass


async def blocking_holds(
    conn: AsyncConnection, ws: uuid.UUID, *, entities: Iterable[uuid.UUID], interactions: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """Active holds that cover any of these records (or the whole workspace)."""
    rows = (
        await conn.execute(
            text(
                "SELECT id FROM legal_holds WHERE workspace_id = :ws AND released_at IS NULL AND (scope_kind = 'workspace' "
                "OR (scope_kind = 'subject' AND target_id = ANY(:e)) OR (scope_kind = 'interaction' AND target_id = ANY(:i))) ORDER BY created_at, id"
            ),
            {"ws": ws, "e": list(entities), "i": list(interactions)},
        )
    ).all()
    return [r.id for r in rows]


class Holds:
    async def create(self, conn: AsyncConnection, ws: uuid.UUID, by: uuid.UUID, body: LegalHoldIn, audit: AuditSink) -> LegalHoldOut:
        if body.target_id is not None:
            table = "entities" if body.scope_kind == "subject" else "interactions"
            found = (
                await conn.execute(text(f"SELECT 1 FROM {table} WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": body.target_id})  # noqa: S608
            ).first()  # noqa: S608
            if found is None:
                raise NotFound()
        hid = uuid7()
        await conn.execute(
            text(
                "INSERT INTO legal_holds (id, workspace_id, scope_kind, target_id, reason_code, note, created_by) VALUES (:id, :ws, :k, :t, :rc, :n, :by)"
            ),
            {"id": hid, "ws": ws, "k": body.scope_kind, "t": body.target_id, "rc": body.reason_code, "n": body.note, "by": by},
        )
        await audit.record(
            "legal_hold.created",
            resource_type="legal_hold",
            resource_id=hid,
            metadata=AuditMetadata(reason_code=body.reason_code, target_id=body.target_id),
        )
        return await self.get(conn, ws, hid)

    async def get(self, conn: AsyncConnection, ws: uuid.UUID, hold_id: uuid.UUID) -> LegalHoldOut:
        row = (
            await conn.execute(text("SELECT * FROM legal_holds WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": hold_id})
        ).first()
        if row is None:
            raise NotFound()
        return LegalHoldOut(**{k: getattr(row, k) for k in LegalHoldOut.model_fields})

    async def list(self, conn: AsyncConnection, ws: uuid.UUID, *, active: bool | None = None) -> list[LegalHoldOut]:
        where = {None: "TRUE", True: "released_at IS NULL", False: "released_at IS NOT NULL"}[active]
        rows = (
            await conn.execute(
                text(f"SELECT * FROM legal_holds WHERE workspace_id = :ws AND {where} ORDER BY created_at DESC, id DESC"),  # noqa: S608
                {"ws": ws},  # noqa: S608
            )
        ).all()  # noqa: S608
        return [LegalHoldOut(**{k: getattr(r, k) for k in LegalHoldOut.model_fields}) for r in rows]

    async def release(self, conn: AsyncConnection, ws: uuid.UUID, by: uuid.UUID, hold_id: uuid.UUID, audit: AuditSink) -> LegalHoldOut:
        res = await conn.execute(
            text(
                "UPDATE legal_holds SET released_at = now(), released_by = :by WHERE workspace_id = :ws AND id = :id AND released_at IS NULL RETURNING reason_code"
            ),
            {"ws": ws, "id": hold_id, "by": by},
        )
        row = res.first()
        if row is None:
            await self.get(conn, ws, hold_id)  # NotFound, or already released
            raise Conflict("The hold was already released")
        await audit.record(
            "legal_hold.released", resource_type="legal_hold", resource_id=hold_id, metadata=AuditMetadata(reason_code=row.reason_code)
        )
        return await self.get(conn, ws, hold_id)


class Suppressions:
    def __init__(self, ref_hmac: RefHasher, ref_hmac_all: Callable[[uuid.UUID, str, str, str], list[str]] | None = None):
        self._hmac = ref_hmac
        self._hmac_all = (
            ref_hmac_all  # P9: every version of the content-HMAC ring, so a suppression recorded before a rotation still blocks after it
        )

    def digest(self, ws: uuid.UUID, type_key: str, system: str, value: str) -> str:
        return self._hmac(ws, type_key, system, value)

    async def record(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, ids: Iterable[tuple[str, str, str]]) -> list[str]:
        """`ids`: (type_key, system, value). Returns the HMACs written."""
        out: list[str] = []
        for type_key, system, value in ids:
            h = self.digest(ws, type_key, system, value)
            await conn.execute(
                text(
                    "INSERT INTO deletion_suppressions (workspace_id, kind, value_hmac, request_id) VALUES (:ws, 'external_id', :h, :r) "
                    "ON CONFLICT (workspace_id, kind, value_hmac) DO UPDATE SET lifted_at = NULL, lifted_by = NULL"
                ),
                {"ws": ws, "h": h, "r": request_id},
            )
            out.append(h)
        return out

    async def check(self, conn: AsyncConnection, ws: uuid.UUID, ids: Iterable[tuple[str, str, str]]) -> None:
        """Raises `SubjectSuppressed` when any of these identifiers stands suppressed."""
        digests = [d for t, s, v in ids for d in (self._hmac_all(ws, t, s, v) if self._hmac_all else [self.digest(ws, t, s, v)])]
        if not digests:
            return
        hit = (
            await conn.execute(
                text(
                    "SELECT 1 FROM deletion_suppressions WHERE workspace_id = :ws AND kind = 'external_id' AND value_hmac = ANY(:h) AND lifted_at IS NULL LIMIT 1"
                ),
                {"ws": ws, "h": digests},
            )
        ).first()
        if hit is not None:
            raise SubjectSuppressed()

    async def list(self, conn: AsyncConnection, ws: uuid.UUID, *, active: bool | None = None) -> list[SuppressionOut]:
        where = {None: "TRUE", True: "lifted_at IS NULL", False: "lifted_at IS NOT NULL"}[active]
        rows = (
            await conn.execute(
                text(f"SELECT * FROM deletion_suppressions WHERE workspace_id = :ws AND {where} ORDER BY created_at DESC"),  # noqa: S608
                {"ws": ws},  # noqa: S608
            )
        ).all()  # noqa: S608
        return [
            SuppressionOut(
                kind=r.kind,
                value_hmac_prefix=r.value_hmac[:8],
                request_id=r.request_id,
                created_at=r.created_at,
                lifted_at=r.lifted_at,
                lifted_by=r.lifted_by,
            )
            for r in rows
        ]

    async def lift(self, conn: AsyncConnection, ws: uuid.UUID, by: uuid.UUID, prefix: str, audit: AuditSink) -> int:
        """Lifts every suppression whose HMAC starts with `prefix` (at least 8 characters). Returns how many."""
        if len(prefix) < 8:
            raise NotFound()
        res = await conn.execute(
            text(
                "UPDATE deletion_suppressions SET lifted_at = now(), lifted_by = :by WHERE workspace_id = :ws AND lifted_at IS NULL AND value_hmac LIKE :p || '%' RETURNING request_id"
            ),
            {"ws": ws, "by": by, "p": prefix.lower()},
        )
        rows = res.all()
        if not rows:
            raise NotFound()
        await audit.record(
            "suppression.lifted", resource_type="suppression", resource_id=rows[0].request_id, metadata=AuditMetadata(count=len(rows))
        )
        return len(rows)
