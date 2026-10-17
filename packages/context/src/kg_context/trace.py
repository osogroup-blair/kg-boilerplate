"""Traces, usage and debug snapshots (phase-6 plan §5.12, ADR-034). METADATA ONLY.

A trace explains a build without being able to repeat it: ids, scores, features, tiers, exclusion reasons, token accounting, versions, watermarks and latencies. The task and
the query are keyed hashes (a sentinel test finds neither in the database, the logs or the metrics). Only authorised candidates are ever recorded (CTX-03): the rows are
written from the same list the packer saw, and that list was produced by the scoped sources.

A debug snapshot is the one place the rendered text and the task are kept: opt-in, admin-only, encrypted with the payload cipher (AAD `workspace|context|debug`), and it
expires; reads honour the expiry on the database clock, and `ops.purge_expired_context_debug()` removes the rows."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_ingestion.crypto import ContentHasher, PayloadCipher, Sealed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_context.models import Candidate

_INSERT_TRACE = text("""
    INSERT INTO context_traces (id, workspace_id, principal_id, act_as_by, profile, profile_source, ontology_version_id, status, compression, task_hmac, task_tokens,
                                query_hmac, request, plan, versions, counts, unavailable, watermarks, latencies, max_tokens, used_tokens, required_tokens)
    VALUES (:id, :ws, :principal, :act_as, :profile, :psource, :ontology, :status, :compression, :task_hmac, :task_tokens, :query_hmac,
            CAST(:request AS jsonb), CAST(:plan AS jsonb), CAST(:versions AS jsonb), CAST(:counts AS jsonb), CAST(:unavailable AS jsonb),
            CAST(:watermarks AS jsonb), CAST(:latencies AS jsonb), :max_tokens, :used_tokens, :required_tokens)""")
_INSERT_ITEM = text("""
    INSERT INTO context_items (context_id, position, workspace_id, handle, item_kind, item_id, section, tier, score, features, tokens, included, exclusion_reason, duplicate_of)
    VALUES (:cid, :pos, :ws, :handle, :kind, :item, :section, :tier, :score, CAST(:features AS jsonb), :tokens, :included, :reason, :dup)""")


_LIST_COLUMNS = "id, principal_id, act_as_by, profile, status, compression, max_tokens, used_tokens, created_at"


def item_ref(c: Candidate) -> str | None:
    """What identifies the item across builds: the fact or memory id; for an unknown or a conflict, the key that derives it."""
    if c.kind in ("fact", "memory"):
        return c.id
    return c.key.split(":", 1)[1][:128] if ":" in c.key else c.key[:128]


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class TraceStore:
    def __init__(self, hasher: ContentHasher, cipher: PayloadCipher):
        self._hasher, self._cipher = hasher, cipher

    def hmac(self, workspace_id: uuid.UUID, kind: str, value: str) -> str:
        return self._hasher.digest(workspace_id, f"ctx|{kind}|{value}".encode())

    async def write(
        self,
        conn: AsyncConnection,
        *,
        context_id: uuid.UUID,
        workspace_id: uuid.UUID,
        principal_id: uuid.UUID,
        act_as_by: uuid.UUID | None,
        profile: str,
        profile_source: str,
        ontology_version_id: uuid.UUID | None,
        status: str,
        compression: str,
        task: str,
        query: str,
        task_tokens: int,
        request: dict[str, Any],
        plan: dict[str, Any],
        versions: dict[str, Any],
        counts: dict[str, Any],
        unavailable: list[dict[str, Any]],
        watermarks: dict[str, Any],
        latencies: dict[str, Any],
        max_tokens: int,
        used_tokens: int,
        required_tokens: int,
        candidates: list[Candidate],
    ) -> None:
        await conn.execute(
            _INSERT_TRACE,
            {
                "id": context_id,
                "ws": workspace_id,
                "principal": principal_id,
                "act_as": act_as_by,
                "profile": profile,
                "psource": profile_source,
                "ontology": ontology_version_id,
                "status": status,
                "compression": compression,
                "task_hmac": self.hmac(workspace_id, "task", task),
                "task_tokens": task_tokens,
                "query_hmac": self.hmac(workspace_id, "query", query),
                "request": _dumps(request),
                "plan": _dumps(plan),
                "versions": _dumps(versions),
                "counts": _dumps(counts),
                "unavailable": _dumps(unavailable),
                "watermarks": _dumps(watermarks),
                "latencies": _dumps(latencies),
                "max_tokens": max_tokens,
                "used_tokens": used_tokens,
                "required_tokens": required_tokens,
            },
        )
        rows = [
            {
                "cid": context_id,
                "pos": i,
                "ws": workspace_id,
                "handle": c.handle or None,
                "kind": c.kind,
                "item": item_ref(c),
                "section": c.section,
                "tier": c.tier,
                "score": round(c.score, 9),
                "features": _dumps(c.features),
                "tokens": c.tokens,
                "included": c.exclusion is None,
                "reason": c.exclusion,
                "dup": c.duplicate_of,
            }
            for i, c in enumerate(candidates)
        ]
        if rows:
            await conn.execute(_INSERT_ITEM, rows)

    # ---- debug snapshots ------------------------------------------------------------------------------------------------------------

    async def write_debug(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        context_id: uuid.UUID,
        created_by: uuid.UUID,
        payload: dict[str, Any],
        ttl_hours: float,
    ) -> dt.datetime:
        sealed = self._cipher.encrypt(_dumps(payload).encode(), workspace_id=workspace_id, interaction_id=context_id, form="debug")
        row = (
            await conn.execute(
                text(
                    "INSERT INTO context_debug_snapshots (context_id, workspace_id, kid, nonce, ciphertext, created_by, expires_at) "
                    "VALUES (:cid, :ws, :kid, :nonce, :ct, :by, now() + make_interval(secs => :ttl)) RETURNING expires_at"
                ),
                {
                    "cid": context_id,
                    "ws": workspace_id,
                    "kid": sealed.kid,
                    "nonce": sealed.nonce,
                    "ct": sealed.ciphertext,
                    "by": created_by,
                    "ttl": ttl_hours * 3600.0,
                },
            )
        ).one()
        return row.expires_at  # type: ignore[no-any-return]

    async def read_debug(self, conn: AsyncConnection, workspace_id: uuid.UUID, context_id: uuid.UUID) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "SELECT kid, nonce, ciphertext, expires_at FROM context_debug_snapshots WHERE workspace_id = :ws AND context_id = :cid AND expires_at > now()"
                ),
                {"ws": workspace_id, "cid": context_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        plain = self._cipher.decrypt(
            Sealed(row.kid, bytes(row.nonce), bytes(row.ciphertext)), workspace_id=workspace_id, interaction_id=context_id, form="debug"
        )
        return {"expires_at": row.expires_at, **json.loads(plain)}


class TraceReads:
    async def get(self, conn: AsyncConnection, workspace_id: uuid.UUID, context_id: uuid.UUID) -> Any:
        row = (
            await conn.execute(
                text("SELECT * FROM context_traces WHERE workspace_id = :ws AND id = :id"), {"ws": workspace_id, "id": context_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        return row

    async def items(self, conn: AsyncConnection, workspace_id: uuid.UUID, context_id: uuid.UUID, *, hide_frozen: bool = False) -> list[Any]:
        """The recorded candidates of a build. `hide_frozen` (the trace a person reads) drops those about a record whose deletion has been accepted (P8): the id is a
        plain id or `kind:<id>` (an unknown), so the match is on the id's text."""
        frozen = (
            " AND NOT EXISTS (SELECT 1 FROM (SELECT id FROM entities WHERE workspace_id = :ws AND status = 'deletion_pending' "
            "UNION ALL SELECT id FROM facts WHERE workspace_id = :ws AND status = 'deletion_pending' "
            "UNION ALL SELECT id FROM curated_memories WHERE workspace_id = :ws AND status = 'deletion_pending') z WHERE position(z.id::text in context_items.item_id) > 0)"
            if hide_frozen
            else ""
        )
        return list(
            (
                await conn.execute(
                    text(f"SELECT * FROM context_items WHERE workspace_id = :ws AND context_id = :id{frozen} ORDER BY position"),  # noqa: S608
                    {"ws": workspace_id, "id": context_id},
                )
            ).all()
        )

    async def list(
        self, conn: AsyncConnection, workspace_id: uuid.UUID, *, principal_id: uuid.UUID | None, limit: int | None, cursor: str | None
    ) -> Page[dict[str, Any]]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["workspace_id = :ws"], {"ws": workspace_id, "n": size + 1}
        if principal_id is not None:
            where.append("principal_id = :p")
            params["p"] = principal_id
        if before:
            where.append("(created_at, id) < (:c_at, :c_id)")
            params["c_at"], params["c_id"] = before[0], before[1]
        sql = f"SELECT {_LIST_COLUMNS} FROM context_traces WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :n"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        return Page[dict[str, Any]](
            items=[dict(r._mapping) for r in page], next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None
        )

    async def usage(self, conn: AsyncConnection, workspace_id: uuid.UUID, item_kind: str, item_id: str) -> dict[str, Any]:
        """How many builds INCLUDED this fact or memory and when last, with the most recent build ids (admin only: it reveals agent activity)."""
        agg = (
            await conn.execute(
                text(
                    "SELECT count(*) AS n, max(t.created_at) AS last_used FROM context_items i JOIN context_traces t ON t.workspace_id = i.workspace_id AND t.id = i.context_id "
                    "WHERE i.workspace_id = :ws AND i.item_kind = :k AND i.item_id = :i AND i.included"
                ),
                {"ws": workspace_id, "k": item_kind, "i": item_id},
            )
        ).one()
        recent = (
            await conn.execute(
                text(
                    "SELECT t.id FROM context_items i JOIN context_traces t ON t.workspace_id = i.workspace_id AND t.id = i.context_id "
                    "WHERE i.workspace_id = :ws AND i.item_kind = :k AND i.item_id = :i AND i.included ORDER BY t.created_at DESC, t.id DESC LIMIT 5"
                ),
                {"ws": workspace_id, "k": item_kind, "i": item_id},
            )
        ).all()
        return {"builds": int(agg.n), "last_used_at": agg.last_used, "recent_context_ids": [r.id for r in recent]}
