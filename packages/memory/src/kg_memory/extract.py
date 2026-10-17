"""The `memory_propose` stage (phase-5 plan §5.5, ADR-030): a model PROPOSES candidate memories from one interaction; the platform decides.

    normalize + graph_ingest done ─► memory_propose job
      tx 1  gate (consent, egress, classification, policy), load the normalised content, the linked subjects and the facts this episode created; stage -> running
      model  `ModelGateway.complete(purpose="memory_extraction", response_schema=MemoryCandidates)`, one hard timeout
      pure   `select_candidates`: drop what the policy, the subject list or the escalation heuristic refuses (every drop has a reason code)
      tx 2  one `memory.extract` ledger command per kept candidate (HMAC dedupe), the stage result, `Orchestrator.advance`: ONE transaction

A crash before tx 2 re-runs the model and the HMAC dedupe keeps the store free of duplicates (FI-11); after tx 2 the stage is terminal at this generation and a
re-run only acks, without a model call (FI-12). Nothing a model says reaches the store unchecked: subjects come only from entities already linked to the
episode (R7), scopes come from policy (never from the content), and an extracted procedural memory is always proposed for review (AT-14).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kg_common.settings import Settings
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType, PipelineStage, SystemAudit
from kg_ingestion.crypto import PayloadCipher
from kg_ingestion.normalize import TERMINAL_OK, lock_stage, stage_unavailable
from kg_ingestion.pipeline import DEAD, FAILED, SKIPPED_POLICY, SUCCEEDED, Orchestrator
from kg_ingestion.store import read_content
from kg_jobs import faults
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.policy import Decision as JobDecision
from kg_knowledge.specs import SpecCache
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import Correlation, ModelGateway
from kg_obs import metrics
from kg_obs.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_memory import ledger, store
from kg_memory.errors import job_error_for
from kg_memory.policy import EffectivePolicy, authority_for, decide, resolve_policy
from kg_memory.prompts import mem_v1
from kg_memory.store import EvidenceRef, NewMemory
from kg_memory.writers import MemoryWriter

log = get_logger("kg.memory.propose")
STAGE = "memory_propose"
RESULT_ID_CAP = 200
PURPOSE = "memory_extraction"

# A candidate that ASSERTS a decision the source never made is an escalation (blueprint: "do not turn a temporary consideration into a firm decision").
# A decision word that is negated or hedged just before it ("had not decided", "considering whether to resign") does not assert anything.
_NEGATION_HEDGE = frozenset(
    "not no never nor without yet undecided neither cannot cant wont isnt hasnt hadnt didnt dont doesnt wasnt whether if might may could would should possibly maybe perhaps "
    "considering contemplating thinking wondering weighing deciding pondering toying unsure unclear".split()
)
_WORD = re.compile(r"[a-z]+")
DECISION_WORDS = re.compile(
    r"\b(decided|has decided|had decided|decision (?:was |has been )?made|resign(?:ed|s|ing)?|quit|quits|will leave|is leaving|has left|left (?:acme|the company)|"
    r"accepted (?:the|an) offer|signed (?:the|an)|committed to|is going to (?:leave|start|found))\b",
    re.IGNORECASE,
)


# ---- the model's answer (strict: every field is required, optional ones are nullable) --------------------------------------------------------


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["working", "episodic", "semantic", "procedural"]
    content: str
    summary: str
    subject_names: list[str]
    importance: float
    reason_code: str
    occurred_at: str | None
    effective_from: str | None
    related_fact_ids: list[str]


class MemoryCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[MemoryCandidate]


# ---- selection (pure) ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drop:
    index: int
    reason: str


@dataclass
class Kept:
    index: int
    candidate: MemoryCandidate
    subject_ids: list[uuid.UUID]
    fact_ids: list[uuid.UUID]
    policy: EffectivePolicy


@dataclass
class Selection:
    kept: list[Kept] = field(default_factory=list)
    drops: list[Drop] = field(default_factory=list)

    def drop_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.drops:
            out[d.reason] = out.get(d.reason, 0) + 1
        return out


def asserts_decision(text: str) -> bool:
    """True when the text states that a decision was made or an irreversible step taken. Negations and hedges within the six words before a match cancel it."""
    for m in DECISION_WORDS.finditer(text):
        before = _WORD.findall(text[: m.start()].lower().replace("'", ""))[-6:]
        if not (set(before) & _NEGATION_HEDGE):
            return True
    return False


def _name_key(name: str) -> str:
    return " ".join(name.casefold().split())


def select_candidates(
    candidates: list[MemoryCandidate],
    *,
    source_text: str,
    subjects: dict[str, uuid.UUID],
    episode_facts: dict[str, uuid.UUID],
    policy_for: Callable[[str], EffectivePolicy],
) -> Selection:
    """Which candidates become memories. `subjects` maps a casefolded name (or alias) of an entity ALREADY linked to the episode to its id: the model cannot add one
    (R7). `episode_facts` maps a fact handle (F1…) and the fact's own id to a fact this episode created or confirmed. Pure and deterministic, so the gold set
    and the unit tests pin every reason code."""
    sel = Selection()
    seen: set[tuple[str, str]] = set()
    source_decides = asserts_decision(source_text)
    pool: list[Kept] = []
    for i, c in enumerate(candidates):
        policy = policy_for(c.type)
        content = c.content.strip()
        if c.type == "working":
            sel.drops.append(Drop(i, "working_type"))
        elif not content:
            sel.drops.append(Drop(i, "empty"))
        elif len(content) > policy.max_content_chars:
            sel.drops.append(Drop(i, "too_long"))
        elif not (0.0 <= c.importance <= 1.0) or c.importance < policy.extraction_min_importance:
            sel.drops.append(Drop(i, "below_importance"))
        else:
            ids: list[uuid.UUID] = []
            missing = False
            for name in c.subject_names:
                found = subjects.get(_name_key(name))
                if found is None:
                    missing = True
                    break
                if found not in ids:
                    ids.append(found)
            fact_ids: list[uuid.UUID] = []
            foreign = False
            for raw in c.related_fact_ids:
                fid = episode_facts.get(raw.strip().upper()) or episode_facts.get(raw.strip().lower())  # a handle (F1) or the fact's own id
                if fid is None:
                    foreign = True
                    break
                if fid not in fact_ids:
                    fact_ids.append(fid)
            if missing:
                sel.drops.append(Drop(i, "unmapped_subject"))  # R7: a name the platform did not link is never turned into a subject
            elif foreign:
                sel.drops.append(Drop(i, "foreign_fact"))
            elif not ids and policy.default_extracted_scope == "subject":
                sel.drops.append(Drop(i, "no_subject"))
            elif asserts_decision(content) and not source_decides:
                sel.drops.append(Drop(i, "escalated_claim"))
            elif (c.type, _name_key(content)) in seen:
                sel.drops.append(Drop(i, "duplicate_candidate"))
            else:
                seen.add((c.type, _name_key(content)))
                pool.append(Kept(i, c, ids, fact_ids, policy))
    pool.sort(key=lambda k: (-k.candidate.importance, k.index))
    cap = min((k.policy.max_memories_per_interaction for k in pool), default=0)
    for rank, k in enumerate(pool):
        if rank < cap:
            sel.kept.append(k)
        else:
            sel.drops.append(Drop(k.index, "over_limit"))
    sel.kept.sort(key=lambda k: k.index)
    return sel


def _parse_instant(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


class MemoryProposeResult(BaseModel):
    """The closed shape of `interaction_stages.result` for this stage: ids, counts and codes, never text (EVD-03)."""

    model_config = ConfigDict(extra="forbid")
    code: str | None = None
    prompt_version: str | None = None
    alias: str | None = None
    candidates: int = 0
    kept: int = 0
    statuses: dict[str, int] = {}
    drops: dict[str, int] = {}
    memory_ids: list[uuid.UUID] = []
    review_ids: list[uuid.UUID] = []
    chat_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---- the job --------------------------------------------------------------------------------------------------------------------------------


class MemoryProposeJob:
    stage = STAGE

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: PayloadCipher,
        gateway: ModelGateway,
        writer: MemoryWriter,
        specs: SpecCache,
        config: ModelConfig,
        settings: Settings,
        *,
        flush_calls: Callable[[], Awaitable[None]],
        orchestrator: Orchestrator | None = None,
        service: str = "kg-worker",
    ):
        self._engine, self._cipher, self._gateway, self._writer, self._specs = engine, cipher, gateway, writer, specs
        self._config, self._settings, self._flush, self._service = config, settings, flush_calls, service
        self._orchestrator = orchestrator or Orchestrator()

    async def handle(self, job: ClaimedJob) -> None:
        started, outcome = time.perf_counter(), "error"
        try:
            outcome = await self._handle(job)
        finally:
            metrics.outbox_job_duration_seconds.labels(stage=STAGE).observe(time.perf_counter() - started)
            log.info("memory_propose_attempt", outcome=outcome, attempt=job.attempts)

    async def _handle(self, job: ClaimedJob) -> str:
        ws, iid, gen = job.workspace_id, job.subject_id, job.generation
        ctx = WorkspaceContext.for_system_task(ws, reason="memory_propose stage")
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None:
                raise await stage_unavailable(conn, ws, iid)
            if st.generation > gen:
                return "stale"
            if st.generation < gen:
                raise JobError(Category.CONFIG_TERMINAL, "generation_mismatch")
            if st.status == DEAD:
                raise JobError(Category.CONFIG_TERMINAL, st.last_error_code or "stage_dead")
            if st.status in TERMINAL_OK:
                return "noop"  # the ack was lost after the commit: the result stands, the model is not asked again (FI-12)
            head = (
                await conn.execute(
                    text(
                        "SELECT i.classification, i.consent_external, i.occurred_at, i.ontology_version_id, i.submitted_by, p.kind AS submitter_kind, "
                        "(p.status = 'revoked' AND p.revoke_reason = 'compromised') AS submitter_compromised "
                        "FROM interactions i JOIN principals p ON p.workspace_id = i.workspace_id AND p.id = i.submitted_by "
                        "WHERE i.workspace_id = :ws AND i.id = :id"
                    ),
                    {"ws": ws, "id": iid},
                )
            ).first()
            if head is None:
                raise JobError(Category.CONFIG_TERMINAL, "interaction_missing")
            pinned = st.ontology_version_id or head.ontology_version_id
            specs_policies = None
            if pinned is not None:
                try:
                    specs_policies = (await self._specs.get(conn, ws, pinned)).memory_policies
                except Exception as exc:
                    raise JobError(Category.CONFIG_TERMINAL, "ontology_unreadable") from exc

            def policy_for(memory_type: str) -> EffectivePolicy:
                return resolve_policy(specs_policies, memory_type)

            base = policy_for("episodic")
            skip = (
                "submitter_compromised"  # P7: nothing is derived from a compromised principal's submission
                if head.submitter_compromised
                else "extraction_disabled"
                if base.extraction == "disabled"
                else "consent_denied"
                if not head.consent_external
                else "egress_denied"
                if not self._config.egress_allows(head.classification)
                else "classification_excluded"
                if head.classification in base.excluded_classifications
                else None
            )
            if skip is not None:  # ING-06: nothing leaves, no model is called
                await self._finish(conn, ws, iid, status=SKIPPED_POLICY, code=skip, result=MemoryProposeResult(code=skip))
                return "skipped"
            sealed = await read_content(conn, ws, iid, "normalized")
            if sealed is None:
                raise JobError(Category.CONFIG_TERMINAL, "normalized_content_missing")
            graph = (
                await conn.execute(
                    text(
                        "SELECT result FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :i AND stage = 'graph_ingest'"
                    ),
                    {"ws": ws, "i": iid},
                )
            ).first()
            episode_uuid = (graph.result or {}).get("episode_uuid") if graph else None
            entity_rows = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT e.id, e.type_key, e.display_name, e.aliases FROM entities e WHERE e.workspace_id = :ws AND e.status = 'active' AND e.id IN ("
                        "SELECT entity_id FROM entity_mentions WHERE workspace_id = :ws AND interaction_id = :i AND status = 'accepted' "
                        "UNION SELECT entity_id FROM interaction_subjects WHERE workspace_id = :ws AND interaction_id = :i AND entity_id IS NOT NULL) "
                        "ORDER BY e.display_name, e.id"
                    ),
                    {"ws": ws, "i": iid},
                )
            ).all()
            fact_rows = (
                await conn.execute(
                    text(
                        "SELECT f.id, f.rel_type, COALESCE(f.fact_text, f.rel_type) AS fact_text FROM facts f WHERE f.workspace_id = :ws AND f.id IN "
                        "(SELECT fact_id FROM fact_evidence WHERE workspace_id = :ws AND interaction_id = :i) AND f.status IN ('active','proposed') "
                        "ORDER BY f.recorded_at, f.id"
                    ),
                    {"ws": ws, "i": iid},
                )
            ).all()
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = 'running', attempts = :n, started_at = COALESCE(started_at, now()), finished_at = NULL "
                    "WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {"n": job.attempts, "ws": ws, "i": iid, "s": STAGE},
            )

        normalized = self._cipher.decrypt(sealed, workspace_id=ws, interaction_id=iid, form="normalized").decode()
        subjects: dict[str, uuid.UUID] = {}
        for e in entity_rows:
            for name in (e.display_name, *list(e.aliases or [])):
                subjects.setdefault(_name_key(name), e.id)
        # handles are stable for the same facts (sorted by what they say, not by ids or times), so a scenario fixture can name F1
        ordered = sorted(fact_rows, key=lambda f: (f.rel_type, f.fact_text, str(f.id)))
        episode_facts: dict[str, uuid.UUID] = {}
        for n, f in enumerate(ordered, start=1):
            episode_facts[f"F{n}"] = f.id
            episode_facts[str(f.id)] = f.id
        messages = mem_v1.build_messages(
            content=normalized,
            subjects=[(e.type_key, e.display_name) for e in entity_rows],
            facts=[(f"F{n}", f.rel_type, f.fact_text) for n, f in enumerate(ordered, start=1)],
            max_memories=base.max_memories_per_interaction,
            min_importance=base.extraction_min_importance,
        )
        correlation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memory-propose:{iid}:g{gen}"))  # a UUID: the router reuses it as the request id
        try:
            async with asyncio.timeout(self._settings.memory_propose_timeout_seconds):
                completion = await self._gateway.complete(
                    PURPOSE,
                    messages,
                    classification=head.classification,
                    correlation=Correlation(correlation, str(ws), "", self._service),
                    response_schema=MemoryCandidates,
                )
        except Exception as exc:
            if type(exc).__name__ == "CancelledError":
                raise
            raise job_error_for(exc, attempt=job.attempts) from exc
        faults.current().hit(STAGE, "after_side_effect")  # FI-11: a crash after the model answered, before the platform committed anything
        await self._flush()
        assert isinstance(completion.parsed, MemoryCandidates)
        selection = select_candidates(
            completion.parsed.candidates,
            source_text=normalized,
            subjects=subjects,
            episode_facts=episode_facts,
            policy_for=policy_for,
        )

        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None or st.generation != gen or st.status in TERMINAL_OK or st.status == DEAD:
                return "noop"
            audit = SystemAudit(conn, ws, correlation_id=correlation)
            memory_ids: list[uuid.UUID] = []
            review_ids: list[uuid.UUID] = []
            statuses: dict[str, int] = {}
            for k in selection.kept:
                written = await self._write(conn, ws, audit, job, head, iid, gen, episode_uuid, completion.alias, k)
                if written is None:
                    continue
                memory_ids.append(written.memory_id)
                statuses[written.status] = statuses.get(written.status, 0) + 1
                if written.review_id:
                    review_ids.append(written.review_id)
            for d in selection.drops:
                metrics.memory_extraction_candidates_total.labels(outcome=d.reason).inc()
            metrics.memory_extraction_candidates_total.labels(outcome="kept").inc(len(memory_ids))
            usage = await self._usage(conn, ws, correlation)
            result = MemoryProposeResult(
                prompt_version=mem_v1.PROMPT_VERSION,
                alias=completion.alias,
                candidates=len(completion.parsed.candidates),
                kept=len(memory_ids),
                statuses=statuses,
                drops=selection.drop_counts(),
                memory_ids=memory_ids[:RESULT_ID_CAP],
                review_ids=review_ids[:RESULT_ID_CAP],
                chat_calls=usage[0],
                prompt_tokens=usage[1],
                completion_tokens=usage[2],
            )
            await audit.record(
                "memory.extraction_completed",
                resource_type="interaction",
                resource_id=iid,
                metadata=AuditMetadata(
                    stage=PipelineStage.MEMORY_PROPOSE,
                    generation=gen,
                    interaction_id=iid,
                    count_memories=len(memory_ids),
                    count=len(completion.parsed.candidates),
                ),
            )
            await self._finish(conn, ws, iid, status=SUCCEEDED, code=None, result=result)
        return "succeeded"

    async def _write(self, conn, ws, audit, job, head, iid, gen, episode_uuid, alias, k: Kept):  # type: ignore[no-untyped-def]
        c, policy = k.candidate, k.policy
        scopes = [("subject", str(s)) for s in k.subject_ids] if k.subject_ids else [("workspace", "*")]
        submitter_is_agent = head.submitter_kind == "agent"
        authority = authority_for("extracted", "system", submitter_is_agent=submitter_is_agent)
        decision = decide(origin="extracted", memory_type=c.type, actor="system", shared=True, conflict=False, policy=policy)
        evidence = [EvidenceRef("interaction", interaction_id=iid)]
        if episode_uuid:
            evidence.append(EvidenceRef("episode", interaction_id=iid, episode_uuid=episode_uuid))
        evidence += [EvidenceRef("fact", fact_id=f) for f in k.fact_ids]
        occurred = _parse_instant(c.occurred_at) or head.occurred_at
        nm = NewMemory(
            type=c.type,
            origin="extracted",
            content=c.content.strip(),
            summary=(c.summary.strip() or store.derive_summary(c.content))[:280],
            status=decision.status,
            scopes=scopes,
            created_by=head.submitted_by,
            authority=authority,
            evidence_status="unverified",
            classification=head.classification,
            importance=float(c.importance),
            pipeline_version=job.pipeline_version,
            subject_ids=k.subject_ids,
            evidence=evidence,
            occurred_at=occurred,
            effective_from=_parse_instant(c.effective_from) or occurred,
            derived_by_alias=alias if alias and len(alias) <= 64 else None,
            prompt_version=mem_v1.PROMPT_VERSION,
        )
        key = f"memory-extract:{iid}:g{gen}:{k.index}"
        digest = ledger.digest_of(CommandType.MEMORY_EXTRACT, head.submitted_by, [str(iid), gen, k.index, nm.hmac or nm.content])
        written = await self._writer.persist(
            conn,
            ws,
            audit,
            command_type=CommandType.MEMORY_EXTRACT,
            key=key,
            digest=digest,
            nm=nm,
            decision=decision,
            ontology_version_id=job.ontology_version_id,
            correlation_id=job.correlation_id,
            audit_action="memory.extracted",
            extra_payload={"interaction_id": str(iid), "generation": gen},
        )
        return written.out

    @staticmethod
    async def _usage(conn: Any, ws: uuid.UUID, correlation: str) -> tuple[int, int, int]:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) AS n, COALESCE(sum(prompt_tokens), 0) AS pt, COALESCE(sum(completion_tokens), 0) AS ct FROM model_calls "
                    "WHERE workspace_id = :ws AND correlation_id = :c AND operation = 'chat'"
                ),
                {"ws": ws, "c": correlation},
            )
        ).one()
        return int(row.n), int(row.pt), int(row.ct)

    async def _finish(
        self, conn: Any, ws: uuid.UUID, iid: uuid.UUID, *, status: str, code: str | None, result: MemoryProposeResult
    ) -> None:
        await conn.execute(
            text(
                "UPDATE interaction_stages SET status = :st, finished_at = now(), last_error_category = :cat, last_error_code = :code, "
                "result = CAST(:r AS jsonb), started_at = COALESCE(started_at, now()) WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
            ),
            {
                "st": status,
                "cat": "policy_terminal" if status == SKIPPED_POLICY else None,
                "code": code,
                "r": result.model_dump_json(exclude_none=True),
                "ws": ws,
                "i": iid,
                "s": STAGE,
            },
        )
        await self._orchestrator.advance(conn, ws, iid)

    # ---- failure bookkeeping (called by the dispatcher before the ack) ------------------------------------------------------------------------

    async def on_failure(self, job: ClaimedJob, decision: JobDecision) -> None:
        ws, iid = job.workspace_id, job.subject_id
        ctx = WorkspaceContext.for_system_task(ws, reason="memory_propose stage failure")
        status = {"retry": FAILED, "dead": DEAD, "skipped": SKIPPED_POLICY}.get(decision.outcome)
        if status is None:
            return
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None or st.generation != job.generation or st.status in TERMINAL_OK:
                return
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = :status, attempts = :n, last_error_category = :cat, last_error_code = :code, "
                    "finished_at = CASE WHEN :final THEN now() END WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {
                    "status": status,
                    "n": job.attempts,
                    "cat": decision.category.value if decision.category else None,
                    "code": decision.code,
                    "final": decision.outcome != "retry",
                    "ws": ws,
                    "i": iid,
                    "s": STAGE,
                },
            )
            if decision.outcome != "retry":
                await self._orchestrator.advance(conn, ws, iid)
        log.info("stage_failed", stage=STAGE, outcome=decision.outcome, error_code=decision.code, attempt=job.attempts)
