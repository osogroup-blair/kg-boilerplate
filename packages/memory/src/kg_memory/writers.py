"""MemoryWriter: every way a curated memory is born or changes (phase-5 plan §5.4): explicit save, proposal, working note, publish, supersede, expire,
dispute, and the review decisions that activate or refuse a proposal.

Everything runs inside the CALLER's tenant transaction: the memory rows, the ledger command, the audit entry, the review item and the outbox jobs commit
together or not at all. The status of a new memory is never chosen by the caller: `policy.decide` does, from who wrote what, where (AT-14: an agent can never
activate a shared procedural memory). Evidence is checked against what the writer may see, and a failure is one answer for "missing" and "not yours"
(`422 invalid_evidence`, no oracle)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kg_common.errors import Conflict, Forbidden, NotFound, PreconditionFailed, PreconditionRequired, UnknownEntity, ValidationFailed
from kg_common.ids import uuid7
from kg_common.settings import Settings
from kg_common.versions import PIPELINE_VERSION
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType
from kg_governance.authz.policy import INTERACTION_READ_ALL_ROLES, Action, enforce
from kg_governance.grants import SubjectFilter, entity_visible_sql, fact_visible_sql
from kg_ingestion.crypto import ContentHasher
from kg_jobs.outbox import enqueue
from kg_knowledge.idempotency import lock_key
from kg_knowledge.specs import SpecCache
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_memory import ledger, store
from kg_memory.consolidate_schedule import CONSOLIDATABLE
from kg_memory.consolidate_schedule import schedule as schedule_consolidation
from kg_memory.models import (
    AUTHORITY_RANK,
    CLASSIFICATION_ORDER,
    MemoryPublishIn,
    MemorySaveIn,
    ProposalIn,
    ReasonIn,
    ScopeIn,
    SupersedeIn,
    WriteOut,
)
from kg_memory.policy import (
    Actor,
    Decision,
    EffectivePolicy,
    authority_for,
    decide,
    default_expiry,
    evidence_status_for,
    resolve_policy,
)
from kg_memory.reads import MemoryReads, Viewer, visible_where
from kg_memory.scopes import InvalidScope, ScopeGrants, is_shared
from kg_memory.store import EvidenceRef, NewMemory

DEFAULT_IMPORTANCE = {"explicit": 0.7, "proposal": 0.5, "published": 0.5, "working": 0.3, "extracted": 0.5}
EXPIRE_MARGIN_SECONDS = 0.5  # the delayed job fires just after the DB clock passes expires_at


class InvalidEvidence(ValidationFailed):
    """One answer for evidence that does not exist and evidence the caller may not cite."""

    code, default_message = "invalid_evidence", "A cited source does not exist or is not available to you"


class AttributionRequired(ValidationFailed):
    code, default_message = "attribution_required", "A shared memory needs its source (source_interaction_id or source_ref) and its purpose"


class ContentTooLong(ValidationFailed):
    code, default_message = "content_too_long", "The content is longer than this workspace's memory policy allows"


class WorkingMustBePrivate(ValidationFailed):
    code, default_message = "working_must_be_private", "Working memory lives in the agent or session scope only"


class UseProposal(Forbidden):
    code, default_message = "use_proposal", "Agents propose shared memory (POST /v1/memories/proposals); they do not save it"


class InvalidTransition(Conflict):
    code, default_message = "invalid_transition", "The memory is not in a state that allows this"


class MemoryFrozen(Conflict):
    """The memory's owner was revoked (P7): an agent-private memory is frozen until erasure (P8). No write, supersession or publication goes through."""

    code, default_message = "memory_frozen", "The memory belongs to a revoked principal and is frozen"


class DuplicateMemory(Conflict):
    code, default_message = "duplicate_memory", "An identical live memory already exists"


class AuditSink(Protocol):
    async def record(
        self,
        action: str,
        *,
        outcome: Any = ...,
        resource_type: str | None = ...,
        resource_id: Any = ...,
        metadata: AuditMetadata | None = ...,
    ) -> uuid.UUID: ...


@dataclass
class WriteContext:
    conn: AsyncConnection
    ctx: WorkspaceContext
    principal: Principal
    audit: AuditSink
    grants: ScopeGrants
    curator: bool = False  # memory.curate
    subjects: SubjectFilter = field(
        default_factory=SubjectFilter.unrestricted
    )  # P7: what the writer may cite or name (the API sets the caller's own filter)

    @property
    def viewer(self) -> Viewer:
        return Viewer(self.principal, self.grants, curator=self.curator, subjects=self.subjects)


@dataclass
class Written:
    out: WriteOut
    http_status: int
    replayed: bool = False


def actor_of(principal: Principal) -> Actor:
    if Role.WORKSPACE_ADMIN in principal.roles:
        return "admin"
    return "agent" if principal.kind is PrincipalKind.AGENT else "app"


def _max_class(*values: str) -> str:
    return max(values, key=CLASSIFICATION_ORDER.index)


def _check_revision(current: int, if_match: int | None) -> None:
    if if_match is None:
        raise PreconditionRequired()
    if if_match != current:
        raise PreconditionFailed()


def _first_kind(scopes: list[tuple[str, str]]) -> Any:
    shared = [k for k, _ in scopes if k in ("workspace", "subject", "project")]
    ordered: list[str | None] = [*shared, *(k for k, _ in scopes), None]
    return ordered[0]


_CITABLE_FACTS = (
    "SELECT f.id, f.classification, f.source_entity_id, f.target_entity_id FROM facts f "
    "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
    "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.id = ANY(:ids)"
)


class MemoryWriter:
    def __init__(self, hasher: ContentHasher, reads: MemoryReads, specs: SpecCache, settings: Settings, *, embedding_version: int):
        self._hasher, self._reads, self._specs, self._settings, self._ver = hasher, reads, specs, settings, embedding_version

    # ---- policy and validation ------------------------------------------------------------------------------------------------------------

    async def policy_for(self, conn: AsyncConnection, ws: uuid.UUID, memory_type: str) -> tuple[uuid.UUID | None, EffectivePolicy]:
        active = await self._specs.active(conn, ws)
        if active is None:
            return None, resolve_policy(None, memory_type)
        version_id, spec = active
        return version_id, resolve_policy(spec.memory_policies, memory_type)

    async def _interactions(self, conn: AsyncConnection, principal: Principal, ids: list[uuid.UUID]) -> list[str]:
        """Classifications of the cited interactions. An agent may cite only its own submissions; anything else answers `invalid_evidence`."""
        if not ids:
            return []
        own_only = not (principal.roles & INTERACTION_READ_ALL_ROLES)
        rows = (
            await conn.execute(
                text("SELECT id, classification, submitted_by FROM interactions WHERE workspace_id = :ws AND id = ANY(:ids)"),
                {"ws": principal.workspace_id, "ids": list(ids)},
            )
        ).all()
        found = {r.id: r for r in rows if not own_only or r.submitted_by == principal.principal_id}
        if len(found) != len(set(ids)):
            raise InvalidEvidence()
        return [r.classification for r in found.values()]

    async def _facts(self, conn: AsyncConnection, g: ScopeGrants, sf: SubjectFilter, ws: uuid.UUID, ids: list[uuid.UUID]) -> list[str]:
        """A fact is citable when it is VISIBLE to the writer (both endpoints, under the subject filter: AGT-04) and one of its endpoints is a subject the writer is
        granted. Missing, foreign, hidden and not-yours are one answer: `invalid_evidence`."""
        if not ids:
            return []
        acl, acl_params = fact_visible_sql(sf, source="s", target="t")
        sql = f"{_CITABLE_FACTS} AND {acl}"  # noqa: S608 (constant fragments; the filter is bound parameters)
        rows = (await conn.execute(text(sql), {"ws": ws, "ids": list(ids), **acl_params})).all()
        ok = [r for r in rows if g.can_see_subject(r.source_entity_id) or g.can_see_subject(r.target_entity_id)]
        if len(ok) != len(set(ids)):
            raise InvalidEvidence()
        return [r.classification for r in ok]

    async def _memories(self, conn: AsyncConnection, v: Viewer, ids: list[uuid.UUID]) -> list[str]:
        out: list[str] = []
        for mid in dict.fromkeys(ids):
            try:
                row = await self._reads.visible_row(conn, v, mid)
            except NotFound:
                raise InvalidEvidence() from None
            out.append(row.classification)
        return out

    async def _subjects(
        self,
        conn: AsyncConnection,
        g: ScopeGrants,
        sf: SubjectFilter,
        ws: uuid.UUID,
        subject_ids: list[uuid.UUID],
        topic_ids: list[uuid.UUID],
    ) -> None:
        """Aboutness: the entities must exist here, be visible to the writer, and the writer must be granted the subjects. One answer for all (UnknownEntity)."""
        wanted = list(dict.fromkeys([*subject_ids, *topic_ids]))
        if not wanted:
            return
        acl, acl_params = entity_visible_sql(sf, alias="e")
        sql = f"SELECT e.id FROM entities e WHERE e.workspace_id = :ws AND e.id = ANY(:ids) AND e.status NOT IN ('deleted','merged','deletion_pending') AND {acl}"  # noqa: S608
        rows = (await conn.execute(text(sql), {"ws": ws, "ids": wanted, **acl_params})).all()
        if len(rows) != len(wanted) or any(not g.can_see_subject(s) for s in subject_ids):
            raise UnknownEntity()

    async def _higher_authority_conflict(self, w: WriteContext, assertion: dict[str, Any] | None, authority: str) -> uuid.UUID | None:
        """An ACTIVE memory the writer can see, about the same (subject, predicate), with a different object and a higher authority (ADR-027)."""
        if not assertion:
            return None
        where, params = visible_where(w.viewer, prefix="cv")
        row = (
            await w.conn.execute(
                text(
                    f"SELECT m.id FROM curated_memories m WHERE m.status = 'active' AND {where} AND m.assertion->>'subject_id' = :subj "  # noqa: S608
                    "AND m.assertion->>'predicate' = :pred AND m.assertion->'object' IS DISTINCT FROM CAST(:obj AS jsonb) "
                    "AND (CASE m.authority_class WHEN 'agent_generated' THEN 1 WHEN 'conversation_report' THEN 2 WHEN 'user_asserted' THEN 3 "
                    "WHEN 'admin_asserted' THEN 4 ELSE 5 END) > :rank ORDER BY m.recorded_at, m.id LIMIT 1"
                ),
                {
                    **params,
                    "subj": str(assertion["subject_id"]),
                    "pred": assertion["predicate"],
                    "obj": json.dumps(assertion["object"]),
                    "rank": AUTHORITY_RANK[authority],
                },
            )
        ).first()
        return None if row is None else row.id

    # ---- the shared persist step ----------------------------------------------------------------------------------------------------------

    async def persist(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        audit: AuditSink,
        *,
        command_type: CommandType,
        key: str,
        digest: str,
        nm: NewMemory,
        decision: Decision,
        ontology_version_id: uuid.UUID | None,
        conflict_with: uuid.UUID | None = None,
        memory_id: uuid.UUID | None = None,
        on_duplicate: Literal["return", "raise"] = "return",
        correlation_id: str | None = None,
        audit_action: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> Written:
        """Dedupe, insert, ledger, review, audit and jobs for ONE new memory, in the caller's transaction."""
        all_hmacs = store.candidate_hmacs(
            self._hasher, ws, nm.type, nm.content, nm.subject_ids, nm.scopes
        )  # the active version first; older ones still count as duplicates
        nm.hmac = all_hmacs[0]
        nm.status = decision.status
        await lock_key(conn, ws, "memhmac", nm.hmac)  # two writers of the same words serialise here, so the dedupe check below cannot race
        if decision.status == "rejected":
            metrics.memory_writes_total.labels(origin=nm.origin, outcome="rejected").inc()
            raise ValidationFailed("the memory is not acceptable", details={"code": decision.reason})
        existing = (
            await conn.execute(
                text(
                    "SELECT id, status, revision FROM curated_memories WHERE workspace_id = :ws AND candidate_hmac = ANY(:h) AND status IN ('proposed','active') FOR UPDATE"
                ),
                {"ws": ws, "h": all_hmacs},
            )
        ).first()
        command_id = uuid7()
        if existing is not None:
            if on_duplicate == "raise":
                raise DuplicateMemory()
            for ev in nm.evidence:  # an independent source reinforces the memory; it never creates a second one
                await store.add_evidence(conn, ws, existing.id, ev, nm.created_by)
            await conn.execute(
                text("UPDATE curated_memories SET last_confirmed_at = now() WHERE workspace_id = :ws AND id = :id"),
                {"ws": ws, "id": existing.id},
            )
            out = WriteOut(
                memory_id=existing.id, status=existing.status, deduplicated=True, revision=existing.revision, command_id=command_id
            )
            await ledger.insert_command(
                conn,
                ws,
                command_id=command_id,
                command_type=command_type,
                key=key,
                digest=digest,
                actor_principal_id=nm.created_by,
                authority=nm.authority,
                classification=nm.classification,
                ontology_version_id=ontology_version_id,
                payload={"memory_id": str(existing.id), "deduplicated": True, "content_hmac": nm.hmac},
                result={"out": out.model_dump(mode="json"), "http_status": 200},
                http_status=200,
            )
            await audit.record(
                audit_action,
                resource_type="memory",
                resource_id=existing.id,
                metadata=AuditMetadata(
                    memory_id=existing.id, command_type=command_type, reason_code="deduplicated", count=len(nm.evidence)
                ),
            )
            metrics.memory_writes_total.labels(origin=nm.origin, outcome="deduplicated").inc()
            return Written(out, 200)

        mid = memory_id or uuid7()
        review_id = uuid7() if decision.review else None
        nm.embedding_status = "skipped_by_policy" if nm.classification == "local_only" else "pending"
        http = 201 if decision.status == "active" else 202
        if nm.ttl_seconds is not None:  # now() is the transaction's start time: the ledger row and the memory agree on the deadline
            nm.expires_at = (await conn.execute(text("SELECT now() + (:t * interval '1 second')"), {"t": nm.ttl_seconds})).scalar_one()
        # the ledger row first (the memory points at it), then the memory; the result is written once, with the final shape
        out = WriteOut(
            memory_id=mid,
            status=decision.status,
            revision=1,
            review_id=review_id,
            command_id=command_id,
            expires_at=nm.expires_at,
            embedding_status=nm.embedding_status,
        )
        await ledger.insert_command(
            conn,
            ws,
            command_id=command_id,
            command_type=command_type,
            key=key,
            digest=digest,
            actor_principal_id=nm.created_by,
            authority=nm.authority,
            classification=nm.classification,
            ontology_version_id=ontology_version_id,
            payload={
                "memory_id": str(mid),
                "type": nm.type,
                "origin": nm.origin,
                "scope_kinds": sorted({k for k, _ in nm.scopes}),
                "content_hmac": nm.hmac,
                "reason": decision.reason,
                **(extra_payload or {}),
            },
            result={"out": out.model_dump(mode="json"), "http_status": http},
            http_status=http,
        )
        await store.insert_new(conn, ws, mid, command_id, nm)
        if decision.status == "active":
            await self.schedule_consolidation(conn, ws, nm, correlation_id)
        if decision.review is not None:
            assert review_id is not None
            await self._open_review(
                conn,
                ws,
                audit,
                review_id=review_id,
                item_type=decision.review,
                memory_id=mid,
                competing=conflict_with,
                reason=decision.reason,
                command_id=command_id,
                interaction_id=next((e.interaction_id for e in nm.evidence if e.interaction_id), None),
                subject_id=nm.subject_ids[0] if nm.subject_ids else None,
                created_by=nm.created_by,
                origin=nm.origin,
            )
        await audit.record(
            audit_action,
            resource_type="memory",
            resource_id=mid,
            metadata=AuditMetadata(
                memory_id=mid,
                command_type=command_type,
                memory_type=nm.type,
                memory_status=decision.status,
                scope_kind=_first_kind(nm.scopes),
                reason_code=decision.reason,
                classification=nm.classification,
                count=len(nm.evidence),
                review_id=review_id,
            ),
        )
        await self._enqueue_jobs(conn, ws, mid, nm, nm.expires_at, ontology_version_id, correlation_id)
        metrics.memory_writes_total.labels(origin=nm.origin, outcome=decision.status).inc()
        return Written(out, http)

    async def schedule_consolidation(self, conn: AsyncConnection, ws: uuid.UUID, nm: NewMemory, correlation_id: str | None = None) -> None:
        """MEM-06: a write that leaves enough alike memories under one (type, scope set, subject set) enqueues their consolidation (debounced, a delayed job)."""
        if nm.type not in CONSOLIDATABLE:
            return
        _, policy = await self.policy_for(conn, ws, nm.type)
        await schedule_consolidation(
            conn,
            ws,
            mem_type=nm.type,
            scopes=nm.scopes,
            subjects=nm.subject_ids,
            policy=policy,
            debounce_minutes=self._settings.consolidation_debounce_minutes,
            correlation_id=correlation_id,
        )

    async def _open_review(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        audit: AuditSink,
        *,
        review_id: uuid.UUID,
        item_type: str,
        memory_id: uuid.UUID,
        competing: uuid.UUID | None,
        reason: str,
        command_id: uuid.UUID | None,
        interaction_id: uuid.UUID | None,
        subject_id: uuid.UUID | None,
        created_by: uuid.UUID,
        origin: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, command_id, memory_id, competing_memory_id, interaction_id, "
                "subject_entity_id, detail, due_at) VALUES (:id, :ws, :t, 'memory_policy', :rc, :cmd, :m, :cm, :i, :s, CAST(:d AS jsonb), now() + interval '7 days')"
            ),
            {
                "id": review_id,
                "ws": ws,
                "t": item_type,
                "rc": reason,
                "cmd": command_id,
                "m": memory_id,
                "cm": competing,
                "i": interaction_id,
                "s": subject_id,
                "d": json.dumps({"origin": origin, "created_by": str(created_by)}),
            },
        )
        await audit.record(
            "review.created",
            resource_type="review_item",
            resource_id=review_id,
            metadata=AuditMetadata(
                review_id=review_id, review_type=item_type, reason_code=reason, memory_id=memory_id, interaction_id=interaction_id
            ),
        )
        metrics.reviews_created_total.labels(item_type=item_type).inc()

    async def _enqueue_jobs(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        mid: uuid.UUID,
        nm: NewMemory,
        expires_at: dt.datetime | None,
        ontology_version_id: uuid.UUID | None,
        correlation_id: str | None,
    ) -> None:
        if nm.embedding_status == "pending":
            await enqueue(
                conn,
                workspace_id=ws,
                stage="memory_embed",
                subject_type="memory",
                subject_id=mid,
                dedupe_key=f"memory:{mid}:embed:v{self._ver}:g0",
                payload={"memory_id": str(mid), "generation": 0},
                pipeline_version=PIPELINE_VERSION,
                ontology_version_id=ontology_version_id,
                correlation_id=correlation_id,
            )
        if expires_at is not None and nm.ttl_seconds is not None:
            await enqueue(
                conn,
                workspace_id=ws,
                stage="memory_expire",
                subject_type="memory",
                subject_id=mid,
                dedupe_key=f"memory:{mid}:expire:g1",
                payload={"memory_id": str(mid), "generation": 0},
                pipeline_version=PIPELINE_VERSION,
                ontology_version_id=ontology_version_id,
                delay_seconds=nm.ttl_seconds + EXPIRE_MARGIN_SECONDS,
                correlation_id=correlation_id,
            )

    # ---- scopes ---------------------------------------------------------------------------------------------------------------------------

    def _scopes(
        self, g: ScopeGrants, given: list[ScopeIn], subject_ids: list[uuid.UUID], *, working: bool, run_id: str | None, actor: Actor
    ) -> list[tuple[str, str]]:
        if given:
            scopes = g.writable(given)
        elif working:
            scopes = [("session", f"{g.self_ref}/{run_id}")] if run_id else [("agent", g.self_ref)]
        elif subject_ids:
            scopes = g.writable([ScopeIn(kind="subject", ref=str(s)) for s in subject_ids])
        elif actor == "agent":
            scopes = [("agent", g.self_ref)]
        else:
            scopes = [("workspace", "*")]
        if working and is_shared(scopes):
            raise WorkingMustBePrivate()
        return scopes

    # ---- explicit save / working note -----------------------------------------------------------------------------------------------------

    async def save(self, w: WriteContext, body: MemorySaveIn, key: str) -> Written:
        conn, ws, p = w.conn, w.ctx.workspace_id, w.principal
        digest = ledger.digest_of(CommandType.MEMORY_SAVE, p.principal_id, body.model_dump(mode="json"))
        prior = await ledger.replay_or_none(conn, ws, key, CommandType.MEMORY_SAVE, digest)
        if prior is not None:
            return Written(WriteOut.model_validate(prior["out"]), int(prior["http_status"]), True)
        actor = actor_of(p)
        working = body.type == "working"
        if body.run_id and not working:
            raise ValidationFailed("run_id only applies to working memory", details={"fields": ["run_id"]})
        scopes = self._scopes(w.grants, body.scopes, body.subject_ids, working=working, run_id=body.run_id, actor=actor)
        shared = is_shared(scopes)
        if shared:
            if actor == "agent":
                raise UseProposal()
            enforce(p, Action.MEMORY_SAVE)
            if not ((body.source_interaction_id or body.source_ref) and body.purpose):
                raise AttributionRequired()
        else:
            enforce(p, Action.MEMORY_WRITE_OWN)
        version_id, policy = await self.policy_for(conn, ws, body.type)
        if len(body.content) > policy.max_content_chars:
            raise ContentTooLong()
        await self._subjects(conn, w.grants, w.subjects, ws, body.subject_ids, body.topic_ids)
        evidence: list[EvidenceRef] = []
        classes: list[str] = [body.classification or "internal"]
        if body.source_interaction_id:
            classes += await self._interactions(conn, p, [body.source_interaction_id])
            evidence.append(EvidenceRef("interaction", interaction_id=body.source_interaction_id))
        if body.source_ref:
            evidence.append(EvidenceRef("external_ref", external_ref=body.source_ref))
        authority = authority_for("working" if working else "explicit", actor)
        assertion = _assertion(body.assertion, body.subject_ids)
        conflict = await self._higher_authority_conflict(w, assertion, authority) if shared else None
        decision = decide(
            origin="working" if working else "explicit",
            memory_type=body.type,
            actor=actor,
            shared=shared,
            conflict=conflict is not None,
            policy=policy,
        )
        nm = NewMemory(
            type=body.type,
            origin="working" if working else "explicit",
            content=body.content,
            summary=body.summary or store.derive_summary(body.content),
            status=decision.status,
            scopes=scopes,
            created_by=p.principal_id,
            authority=authority,
            evidence_status=evidence_status_for("explicit", decision.status, actor) if not working else "unverified",
            classification=_max_class(*classes),
            importance=body.importance if body.importance is not None else DEFAULT_IMPORTANCE["working" if working else "explicit"],
            pipeline_version=PIPELINE_VERSION,
            subject_ids=body.subject_ids,
            topic_ids=body.topic_ids,
            evidence=evidence,
            assertion=assertion,
            occurred_at=body.occurred_at,
            effective_from=body.effective_from,
            effective_until=body.effective_until,
            ttl_seconds=default_expiry(policy, body.ttl_hours).total_seconds() if working else None,
            run_id=body.run_id,
            purpose=body.purpose,
        )
        return await self.persist(
            conn,
            ws,
            w.audit,
            command_type=CommandType.MEMORY_SAVE,
            key=key,
            digest=digest,
            nm=nm,
            decision=decision,
            ontology_version_id=version_id,
            conflict_with=conflict,
            audit_action="memory.saved",
        )

    # ---- proposal -------------------------------------------------------------------------------------------------------------------------

    async def propose(self, w: WriteContext, body: ProposalIn, key: str) -> Written:
        conn, ws, p = w.conn, w.ctx.workspace_id, w.principal
        digest = ledger.digest_of(CommandType.MEMORY_PROPOSE, p.principal_id, body.model_dump(mode="json"))
        prior = await ledger.replay_or_none(conn, ws, key, CommandType.MEMORY_PROPOSE, digest)
        if prior is not None:
            return Written(WriteOut.model_validate(prior["out"]), int(prior["http_status"]), True)
        if body.type == "working":
            raise ValidationFailed("working memory is not proposed; save it, then publish it", details={"fields": ["type"]})
        actor = actor_of(p)
        given = [*([body.requested_scope] if body.requested_scope else []), *body.scopes]
        # a proposal with no scope at all defaults to the workspace (an agent's private notes are saved, not proposed), so the agent is treated like an app here
        scopes = self._scopes(w.grants, given, body.subject_ids, working=False, run_id=None, actor="app" if actor == "agent" else actor)
        shared = is_shared(scopes)
        version_id, policy = await self.policy_for(conn, ws, body.type)
        if len(body.content) > policy.max_content_chars:
            raise ContentTooLong()
        await self._subjects(conn, w.grants, w.subjects, ws, body.subject_ids, body.topic_ids)
        classes: list[str] = [body.classification or "internal"]
        evidence: list[EvidenceRef] = []
        classes += await self._interactions(conn, p, body.source_event_ids)
        evidence += [EvidenceRef("interaction", interaction_id=i) for i in dict.fromkeys(body.source_event_ids)]
        classes += await self._facts(conn, w.grants, w.subjects, ws, body.fact_ids)
        evidence += [EvidenceRef("fact", fact_id=f) for f in dict.fromkeys(body.fact_ids)]
        classes += await self._memories(conn, w.viewer, body.memory_ids)
        evidence += [EvidenceRef("memory", memory_id=m) for m in dict.fromkeys(body.memory_ids)]
        authority = authority_for("proposal", actor)
        assertion = _assertion(body.assertion, body.subject_ids)
        conflict = await self._higher_authority_conflict(w, assertion, authority) if shared else None
        decision = decide(
            origin="proposal",
            memory_type=body.type,
            actor=actor,
            shared=shared,
            conflict=conflict is not None,
            policy=policy,
            can_publish=w.grants.may_publish_all(scopes),
        )
        nm = NewMemory(
            type=body.type,
            origin="proposal",
            content=body.content,
            summary=body.summary or store.derive_summary(body.content),
            status=decision.status,
            scopes=scopes,
            created_by=p.principal_id,
            authority=authority,
            evidence_status="unverified",
            classification=_max_class(*classes),
            importance=body.importance if body.importance is not None else DEFAULT_IMPORTANCE["proposal"],
            pipeline_version=PIPELINE_VERSION,
            subject_ids=body.subject_ids,
            topic_ids=body.topic_ids,
            evidence=evidence,
            assertion=assertion,
            occurred_at=body.occurred_at,
            effective_from=body.effective_from,
            effective_until=body.effective_until,
            purpose=body.purpose,
        )
        written = await self.persist(
            conn,
            ws,
            w.audit,
            command_type=CommandType.MEMORY_PROPOSE,
            key=key,
            digest=digest,
            nm=nm,
            decision=decision,
            ontology_version_id=version_id,
            conflict_with=conflict,
            audit_action="memory.proposed",
        )
        written.http_status = 200 if written.out.deduplicated else 202
        return written

    # ---- publish (MEM-02: working memory leaves its scope only as a NEW proposal) ----------------------------------------------------------

    async def publish(
        self, w: WriteContext, memory_id: uuid.UUID, body: MemoryPublishIn, key: str, if_match: int | None, *, as_type: str | None = None
    ) -> Written:
        conn, ws, p = w.conn, w.ctx.workspace_id, w.principal
        digest = ledger.digest_of(CommandType.MEMORY_PUBLISH, p.principal_id, [str(memory_id), body.model_dump(mode="json"), as_type])
        prior = await ledger.replay_or_none(conn, ws, key, CommandType.MEMORY_PUBLISH, digest)
        if prior is not None:
            return Written(WriteOut.model_validate(prior["out"]), int(prior["http_status"]), True)
        row = await self._reads.visible_row(conn, w.viewer, memory_id, lock=True)
        if row.frozen_at is not None:
            raise MemoryFrozen()
        scopes_now = (await store.load_scopes(conn, ws, [memory_id]))[memory_id]
        if row.created_by != p.principal_id or is_shared(scopes_now) or row.status != "active":
            raise NotFound()  # only the owner publishes, only from a private scope; anything else is not found
        _check_revision(row.revision, if_match)
        subjects = (await store.load_subjects(conn, ws, [memory_id]))[memory_id]
        new_type = as_type or ("semantic" if row.type == "working" else row.type)
        if new_type == "working":
            raise ValidationFailed("a published memory is episodic, semantic or procedural", details={"fields": ["as_type"]})
        actor = actor_of(p)
        scopes = w.grants.writable(body.scopes)
        if not is_shared(scopes):
            raise InvalidScope()
        version_id, policy = await self.policy_for(conn, ws, new_type)
        authority = authority_for("published", actor)
        decision = decide(
            origin="published",
            memory_type=new_type,
            actor=actor,
            shared=True,
            conflict=False,
            policy=policy,
            can_publish=w.grants.may_publish_all(scopes),
        )
        evidence = [EvidenceRef("memory", memory_id=memory_id)]
        nm = NewMemory(
            type=new_type,
            origin="published",
            content=row.content,
            summary=row.summary,
            status=decision.status,
            scopes=scopes,
            created_by=p.principal_id,
            authority=authority,
            evidence_status="unverified",
            classification=row.classification,
            importance=float(row.importance),
            pipeline_version=PIPELINE_VERSION,
            subject_ids=subjects["subject"],
            topic_ids=subjects["topic"],
            evidence=evidence,
            lineage=[(memory_id, "published_from")],
            occurred_at=row.occurred_at,
        )
        written = await self.persist(
            conn,
            ws,
            w.audit,
            command_type=CommandType.MEMORY_PUBLISH,
            key=key,
            digest=digest,
            nm=nm,
            decision=decision,
            ontology_version_id=version_id,
            audit_action="memory.published",
            extra_payload={"published_from": str(memory_id), "reason_code": body.reason_code},
        )
        written.http_status = 200 if written.out.deduplicated else 202
        return written

    # ---- lifecycle operations on an existing memory ---------------------------------------------------------------------------------------

    def _may_modify(self, w: WriteContext, row: Any, scopes: list[tuple[str, str]]) -> None:
        """Shared memory is curated (memory.curate); private memory belongs to its owner. Anything else is not found, not forbidden."""
        if is_shared(scopes):
            enforce(w.principal, Action.MEMORY_CURATE)
        elif row.created_by != w.principal.principal_id:
            raise NotFound()
        else:
            enforce(w.principal, Action.MEMORY_WRITE_OWN)

    async def supersede(self, w: WriteContext, memory_id: uuid.UUID, body: SupersedeIn, key: str, if_match: int | None) -> Written:
        conn, ws, p = w.conn, w.ctx.workspace_id, w.principal
        digest = ledger.digest_of(CommandType.MEMORY_SUPERSEDE, p.principal_id, [str(memory_id), body.model_dump(mode="json")])
        prior = await ledger.replay_or_none(conn, ws, key, CommandType.MEMORY_SUPERSEDE, digest)
        if prior is not None:
            return Written(WriteOut.model_validate(prior["out"]), int(prior["http_status"]), True)
        row = await self._reads.visible_row(conn, w.viewer, memory_id, lock=True)
        if row.frozen_at is not None:
            raise MemoryFrozen()
        scopes_old = (await store.load_scopes(conn, ws, [memory_id]))[memory_id]
        self._may_modify(w, row, scopes_old)
        _check_revision(row.revision, if_match)
        if row.status not in ("active", "disputed"):
            raise InvalidTransition()
        version_id, policy = await self.policy_for(conn, ws, row.type)
        if len(body.content) > policy.max_content_chars:
            raise ContentTooLong()
        keep = [(k, r) for k, r in scopes_old if w.grants.can_use(k, r) or k == "workspace"]
        if not keep:
            raise NotFound()
        subjects = (await store.load_subjects(conn, ws, [memory_id]))[memory_id]
        old_evidence = await store.load_evidence(conn, ws, memory_id)
        actor = actor_of(p)
        ttl = None
        if row.type == "working":
            remaining = (
                await conn.execute(
                    text(
                        "SELECT GREATEST(EXTRACT(EPOCH FROM (expires_at - now())), 60)::float8 AS s FROM curated_memories WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"ws": ws, "id": memory_id},
                )
            ).scalar_one()
            ttl = float(remaining)
        nm = NewMemory(
            type=row.type,
            origin="working" if row.type == "working" else "explicit",
            content=body.content,
            summary=body.summary or store.derive_summary(body.content),
            status="active",
            scopes=keep,
            created_by=p.principal_id,
            authority=authority_for("explicit", actor),
            evidence_status="unverified" if row.type == "working" else evidence_status_for("explicit", "active", actor),
            classification=row.classification,
            importance=body.importance if body.importance is not None else float(row.importance),
            pipeline_version=PIPELINE_VERSION,
            subject_ids=subjects["subject"],
            topic_ids=subjects["topic"],
            evidence=[
                *(
                    EvidenceRef(
                        e.source_kind,
                        interaction_id=e.interaction_id,
                        episode_uuid=e.episode_uuid,
                        fact_id=e.fact_id,
                        command_id=e.command_id,
                        memory_id=e.source_memory_id,
                        external_ref=e.external_ref,
                    )
                    for e in old_evidence
                ),
                EvidenceRef("memory", memory_id=memory_id),
            ],
            lineage=[(memory_id, "replaces")],
            occurred_at=row.occurred_at,
            effective_from=body.effective_from,
            effective_until=body.effective_until,
            ttl_seconds=ttl,
            run_id=row.run_id,
            purpose=row.purpose,
        )
        await store.set_status(conn, ws, memory_id, "superseded", close_validity=True)  # first: the old row frees its dedupe slot
        written = await self.persist(
            conn,
            ws,
            w.audit,
            command_type=CommandType.MEMORY_SUPERSEDE,
            key=key,
            digest=digest,
            nm=nm,
            decision=Decision("active", "superseded_by_curator"),
            ontology_version_id=version_id,
            on_duplicate="raise",
            audit_action="memory.superseded",
            extra_payload={"replaces": str(memory_id), "reason_code": body.reason_code},
        )
        await w.audit.record(
            "memory.superseded_old",
            resource_type="memory",
            resource_id=memory_id,
            metadata=AuditMetadata(
                memory_id=memory_id, target_id=written.out.memory_id, memory_status="superseded", reason_code=body.reason_code
            ),
        )
        return written

    async def _transition(
        self,
        w: WriteContext,
        memory_id: uuid.UUID,
        body: ReasonIn,
        key: str,
        if_match: int | None,
        *,
        command_type: CommandType,
        to_status: str,
        from_statuses: tuple[str, ...],
        audit_action: str,
        close_validity: bool,
        require_curator: bool = False,
    ) -> Written:
        conn, ws, p = w.conn, w.ctx.workspace_id, w.principal
        digest = ledger.digest_of(command_type, p.principal_id, [str(memory_id), body.model_dump(mode="json")])
        prior = await ledger.replay_or_none(conn, ws, key, command_type, digest)
        if prior is not None:
            return Written(WriteOut.model_validate(prior["out"]), int(prior["http_status"]), True)
        row = await self._reads.visible_row(conn, w.viewer, memory_id, lock=True)
        if row.frozen_at is not None and to_status != "expired":
            raise MemoryFrozen()  # a frozen memory can still be expired by a curator; nothing else changes it
        scopes = (await store.load_scopes(conn, ws, [memory_id]))[memory_id]
        self._may_modify(w, row, scopes)
        if require_curator:
            enforce(p, Action.MEMORY_CURATE)
        _check_revision(row.revision, if_match)
        if row.status not in from_statuses:
            raise InvalidTransition()
        revision = await store.set_status(conn, ws, memory_id, to_status, close_validity=close_validity)
        version_id, _ = await self.policy_for(conn, ws, row.type)
        command_id = uuid7()
        review_id: uuid.UUID | None = None
        if to_status == "disputed":
            review_id = uuid7()
            await self._open_review(
                conn,
                ws,
                w.audit,
                review_id=review_id,
                item_type="memory_conflict",
                memory_id=memory_id,
                competing=None,
                reason="disputed_by_curator",
                command_id=None,
                interaction_id=None,
                subject_id=None,
                created_by=p.principal_id,
                origin=row.origin,
            )
        out = WriteOut(memory_id=memory_id, status=to_status, revision=revision, review_id=review_id, command_id=command_id)
        await ledger.insert_command(
            conn,
            ws,
            command_id=command_id,
            command_type=command_type,
            key=key,
            digest=digest,
            actor_principal_id=p.principal_id,
            authority=authority_for("explicit", actor_of(p)),
            classification=row.classification,
            ontology_version_id=version_id,
            payload={"memory_id": str(memory_id), "reason_code": body.reason_code, "to": to_status},
            result={"out": out.model_dump(mode="json"), "http_status": 200},
            http_status=200,
        )
        await w.audit.record(
            audit_action,
            resource_type="memory",
            resource_id=memory_id,
            metadata=AuditMetadata(
                memory_id=memory_id,
                memory_type=row.type,
                memory_status=to_status,
                reason_code=body.reason_code,
                command_type=command_type,
                review_id=review_id,
            ),
        )
        metrics.memory_writes_total.labels(origin=row.origin, outcome=to_status).inc()
        return Written(out, 200)

    async def expire(self, w: WriteContext, memory_id: uuid.UUID, body: ReasonIn, key: str, if_match: int | None) -> Written:
        return await self._transition(
            w,
            memory_id,
            body,
            key,
            if_match,
            command_type=CommandType.MEMORY_EXPIRE,
            to_status="expired",
            from_statuses=("active", "proposed", "disputed"),
            audit_action="memory.expired",
            close_validity=True,
        )

    async def reject(self, w: WriteContext, memory_id: uuid.UUID, body: ReasonIn, key: str, if_match: int | None) -> Written:
        """A curator's correction: the memory was never right. It leaves search and context at once; the row and its evidence stay (only deletion removes)."""
        return await self._transition(
            w,
            memory_id,
            body,
            key,
            if_match,
            command_type=CommandType.CORRECTION_MEMORY,
            to_status="rejected",
            from_statuses=("active", "proposed", "disputed"),
            audit_action="memory.rejected",
            close_validity=True,
            require_curator=True,
        )

    async def dispute(self, w: WriteContext, memory_id: uuid.UUID, body: ReasonIn, key: str, if_match: int | None) -> Written:
        return await self._transition(
            w,
            memory_id,
            body,
            key,
            if_match,
            command_type=CommandType.MEMORY_DISPUTE,
            to_status="disputed",
            from_statuses=("active",),
            audit_action="memory.disputed",
            close_validity=False,
            require_curator=True,
        )


def _assertion(a: Any, subject_ids: list[uuid.UUID]) -> dict[str, Any] | None:
    if a is None:
        return None
    if a.subject_id not in subject_ids:
        raise ValidationFailed("the assertion's subject_id must be one of subject_ids", details={"fields": ["assertion"]})
    return {"subject_id": str(a.subject_id), "predicate": a.predicate, "object": a.object}
