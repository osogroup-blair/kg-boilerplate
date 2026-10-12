"""Extracted knowledge in Postgres (P4, ADR-027): what the platform does with a model's proposal.

Graphiti's output is evidence of what a conversation SAID. This module records it with its provenance and decides, with the rule catalog and the
authority-aware conflict policy (kg_ontology.conflicts), whether it becomes an active (unverified) fact, waits as `proposed`, is `disputed` against an
equal-authority fact, or is `rejected` and kept for audit. It never touches the graph or a model: the projector makes the graph equal to these rows.

The same assessment runs again when a human changes the picture (a merge, a `different_entity` decision, an `accept_claim`), so a decision and an
extraction can never disagree about the rules.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kg_common.ids import uuid7
from kg_governance.audit import AuditMetadata
from kg_obs import metrics
from kg_ontology import OntologySpec
from kg_ontology.conflicts import EndEffect, FactView, Incoming, Invalidation, PolicyInputs, Resolution, evidence_status, resolve
from kg_ontology.rules.context import EntityRef, FactSnap, RuleCommand, RuleContext
from kg_ontology.rules.engine import evaluate
from kg_ontology.values import coerce_value
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

NON_ACTIVE = frozenset({"proposed", "rejected", "disputed", "superseded", "deleted"})
IDENTITY_PENDING = "identity_pending"
SCHEMA_STAGES = frozenset({"schema_validation", "source_classification"})


@dataclass(frozen=True)
class EpisodeSource:
    """Where a batch of extracted knowledge came from: one interaction, one episode."""

    interaction_id: uuid.UUID
    episode_uuid: str
    ontology_version_id: uuid.UUID
    classification: str
    recorded_at: dt.datetime  # the platform's record time of the interaction (Q2), not Graphiti's wall clock
    source_system: str
    source_ref: str
    authority: str = "conversation_report"  # `agent_generated` when an agent submitted the interaction


@dataclass
class FactCandidate:
    fact_id: uuid.UUID  # the Graphiti edge uuid becomes the fact id, so the edge and the row are the same thing
    rel_type: str
    source_id: uuid.UUID
    target_id: uuid.UUID
    fact_text: str
    valid_from: dt.datetime | None
    valid_to: dt.datetime | None
    attributes: dict[str, Any] = field(default_factory=dict)
    invalidated: tuple[Invalidation, ...] = ()


@dataclass
class Assessment:
    resolution: Resolution
    attributes: dict[str, Any]
    valid_from: dt.datetime | None
    valid_to: dt.datetime | None
    valid_from_unknown: bool
    violation_codes: list[str]
    rule_kind: str = "conflict_policy"


@dataclass
class FactOutcome:
    fact_id: uuid.UUID
    status: str
    code: str
    review_id: uuid.UUID | None = None
    review_type: str | None = None
    ended: list[uuid.UUID] = field(default_factory=list)
    disputed: list[uuid.UUID] = field(default_factory=list)
    competing: list[uuid.UUID] = field(default_factory=list)  # existing facts the decision (and its review) is about


def _utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


async def touch(conn: AsyncConnection, ws: uuid.UUID, table: str, row_id: uuid.UUID, **set_columns: Any) -> None:
    """Changes a row AND makes the projector pick it up again (revision + 1, projection pending). `table` is a constant chosen by this module."""
    assert table in ("facts", "entities")
    assignments = "".join(f", {col} = :{col}" for col in set_columns)
    await conn.execute(
        text(
            f"UPDATE {table} SET revision = revision + 1, projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), "  # noqa: S608
            f"last_error_code = NULL{assignments} WHERE workspace_id = :ws AND id = :id"
        ),
        {"ws": ws, "id": row_id, **set_columns},
    )


class ExtractedKnowledge:
    # ---- entities, mentions, aliases, claims ------------------------------------------------------------------------------------------------

    async def insert_entity(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        *,
        entity_id: uuid.UUID,
        type_key: str,
        name: str,
        status: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO entities (id, workspace_id, type_key, display_name, aliases, attributes, classification, status, origin, ontology_version_id, "
                "recorded_at) VALUES (:id, :ws, :t, :name, '{}', '{}'::jsonb, :cls, :status, 'extracted', :ver, :rec) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": entity_id,
                "ws": ws,
                "t": type_key,
                "name": name.strip()[:500] or "?",
                "cls": src.classification,
                "status": status,
                "ver": src.ontology_version_id,
                "rec": src.recorded_at,
            },
        )

    async def add_mention(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        *,
        entity_id: uuid.UUID,
        node_uuid: str,
        resolution: str,
        status: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO entity_mentions (id, entity_id, workspace_id, interaction_id, episode_uuid, node_uuid, resolution, status) "
                "VALUES (:id, :e, :ws, :i, :ep, :n, :res, :st) ON CONFLICT (workspace_id, interaction_id, episode_uuid, node_uuid) DO NOTHING"
            ),
            {
                "id": uuid7(),
                "e": entity_id,
                "ws": ws,
                "i": src.interaction_id,
                "ep": src.episode_uuid,
                "n": node_uuid,
                "res": resolution,
                "st": status,
            },
        )

    async def add_alias(self, conn: AsyncConnection, ws: uuid.UUID, entity_id: uuid.UUID, alias: str) -> bool:
        """Adds `alias` to the entity's aliases unless it is already its name or an alias. True when the row changed."""
        row = (
            await conn.execute(
                text("SELECT display_name, aliases FROM entities WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                {"ws": ws, "id": entity_id},
            )
        ).first()
        clean = alias.strip()
        if row is None or not clean or clean.lower() == row.display_name.lower() or clean.lower() in {a.lower() for a in row.aliases}:
            return False
        await touch(conn, ws, "entities", entity_id, aliases=sorted({*row.aliases, clean}))
        return True

    async def add_claims(
        self, conn: AsyncConnection, ws: uuid.UUID, src: EpisodeSource, *, entity_id: uuid.UUID, claims: dict[str, Any]
    ) -> int:
        """Attribute values a model proposed. Always claims, never writes (ADR-027); the caller has already dropped sensitive and undeclared keys."""
        n = 0
        for key, value in sorted(claims.items()):
            res = await conn.execute(
                text(
                    "INSERT INTO attribute_claims (id, entity_id, workspace_id, attribute_key, value, interaction_id, episode_uuid) "
                    "VALUES (:id, :e, :ws, :k, CAST(:v AS jsonb), :i, :ep) ON CONFLICT (workspace_id, entity_id, attribute_key, interaction_id) DO NOTHING"
                ),
                {
                    "id": uuid7(),
                    "e": entity_id,
                    "ws": ws,
                    "k": key,
                    "v": json.dumps(value, default=str),
                    "i": src.interaction_id,
                    "ep": src.episode_uuid,
                },
            )
            n += res.rowcount or 0
        return n

    # ---- evidence -----------------------------------------------------------------------------------------------------------------------------

    async def add_evidence(self, conn: AsyncConnection, ws: uuid.UUID, fact_id: uuid.UUID, src: EpisodeSource) -> bool:
        """One row per (fact, interaction): a second statement from ANOTHER interaction is independent evidence; the same interaction twice is not."""
        res = await conn.execute(
            text(
                "INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, interaction_id, episode_uuid) VALUES (:id, :f, :ws, 'episode', :i, :ep) "
                "ON CONFLICT (fact_id, source_kind, COALESCE(command_id, interaction_id)) DO NOTHING"
            ),
            {"id": uuid7(), "f": fact_id, "ws": ws, "i": src.interaction_id, "ep": src.episode_uuid},
        )
        return bool(res.rowcount)

    # ---- review items -------------------------------------------------------------------------------------------------------------------------

    async def open_review(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        *,
        item_type: str,
        rule_kind: str,
        reason_code: str,
        audit: Any = None,
        fact_id: uuid.UUID | None = None,
        competing_fact_id: uuid.UUID | None = None,
        subject_entity_id: uuid.UUID | None = None,
        candidate_entity_id: uuid.UUID | None = None,
        interaction_id: uuid.UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Opens an item, or returns the open one for the same thing (a replay never opens a second). Detail holds ids and scores, never names."""
        new_id = uuid7()
        row = (
            await conn.execute(
                text(
                    "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, fact_id, competing_fact_id, subject_entity_id, "
                    "candidate_entity_id, interaction_id, detail, due_at) VALUES (:id, :ws, :t, :rk, :rc, :f, :cf, :s, :c, :i, CAST(:d AS jsonb), now() + interval '7 days') "
                    "ON CONFLICT DO NOTHING RETURNING id"
                ),
                {
                    "id": new_id,
                    "ws": ws,
                    "t": item_type,
                    "rk": rule_kind,
                    "rc": reason_code,
                    "f": fact_id,
                    "cf": competing_fact_id,
                    "s": subject_entity_id,
                    "c": candidate_entity_id,
                    "i": interaction_id,
                    "d": json.dumps(detail or {}, default=str),
                },
            )
        ).first()
        if row is not None:
            if audit is not None:
                await audit.record(
                    "review.created",
                    resource_type="review_item",
                    resource_id=new_id,
                    metadata=AuditMetadata(review_id=new_id, review_type=item_type, reason_code=reason_code, interaction_id=interaction_id),
                )
            metrics.reviews_created_total.labels(item_type=item_type).inc()
            return new_id, True
        where, params = (
            ("fact_id = :f", {"f": fact_id}) if candidate_entity_id is None else ("candidate_entity_id = :c", {"c": candidate_entity_id})
        )
        existing = (
            await conn.execute(
                text(f"SELECT id FROM review_items WHERE workspace_id = :ws AND item_type = :t AND status = 'open' AND {where}"),  # noqa: S608
                {"ws": ws, "t": item_type, **params},
            )
        ).first()
        assert existing is not None, "an open item must exist when the insert conflicted"
        return existing.id, False

    # ---- the assessment -----------------------------------------------------------------------------------------------------------------------

    async def assess(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        spec: OntologySpec,
        *,
        rel_type: str,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        authority: str,
        classification: str,
        valid_from: dt.datetime | None,
        valid_to: dt.datetime | None,
        attributes: dict[str, Any],
        source_system: str,
        source_ref: str,
        exclude_fact_id: uuid.UUID | None = None,
        invalidated: Sequence[Invalidation] = (),
        hold: bool = True,
    ) -> Assessment:
        """The rule catalog, then the conflict policy. Reads Postgres only; writes nothing."""
        valid_from, valid_to = _utc(valid_from), _utc(valid_to)
        rt = spec.relationship_types.get(rel_type)
        if rt is None:
            code = "out_of_ontology"
            res = Resolution("rejected", code) if spec.ontology.mode == "strict" else Resolution("proposed", code, "policy_violation")
            return Assessment(res, {}, valid_from, valid_to, valid_from is None, [code], "strict_types")
        rows = {
            r.id: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT id, type_key, display_name, classification, status FROM entities WHERE workspace_id = :ws AND id = ANY(:ids)"
                    ),
                    {"ws": ws, "ids": [source_id, target_id]},
                )
            ).all()
        }
        if source_id not in rows or target_id not in rows:
            return Assessment(
                Resolution("rejected", "unknown_endpoint"), {}, valid_from, valid_to, valid_from is None, ["unknown_endpoint"]
            )
        src_row, tgt_row = rows[source_id], rows[target_id]
        if not rt.temporal:  # a model cannot date a fact the ontology says has no valid time; the dates are dropped, not the fact
            valid_from = valid_to = None
        unknown = rt.temporal and valid_from is None
        clean: dict[str, Any] = {}
        for (
            key,
            spec_attr,
        ) in rt.attributes.items():  # best effort: an attribute the model got wrong is dropped, a missing required one is a violation
            if spec_attr.sensitive or key not in attributes or attributes[key] is None:
                continue
            norm, err = coerce_value(spec_attr, attributes[key])
            if err is None:
                clean[key] = norm
        facts = (
            await conn.execute(
                text(
                    "SELECT id, rel_type, source_entity_id, target_entity_id, valid_from, valid_to, valid_from_unknown, status, authority_class FROM facts "
                    "WHERE workspace_id = :ws AND source_entity_id = :s AND rel_type = :r AND status = 'active' AND (CAST(:x AS uuid) IS NULL OR id <> :x)"
                ),
                {"ws": ws, "s": source_id, "r": rel_type, "x": exclude_fact_id},
            )
        ).all()
        cmd = RuleCommand(
            kind="relationship.assert",
            rel_type=rel_type,
            source=EntityRef(str(src_row.id), src_row.type_key, src_row.display_name, src_row.classification),
            target=EntityRef(str(tgt_row.id), tgt_row.type_key, tgt_row.display_name, tgt_row.classification),
            attributes=clean,
            valid_from=valid_from,
            valid_to=valid_to,
            valid_from_unknown=unknown,
            classification=classification,
            authority=authority,
            source_system=source_system,
            source_ref=source_ref,
            source_type="extracted",
        )
        ev = evaluate(
            spec,
            cmd,
            RuleContext(
                facts=tuple(
                    FactSnap(
                        str(f.id),
                        f.rel_type,
                        str(f.source_entity_id),
                        str(f.target_entity_id),
                        f.valid_from,
                        f.valid_to,
                        f.valid_from_unknown,
                        f.status,
                    )
                    for f in facts
                )
            ),
        )
        live = [v for v in ev.violations if v.action != "warn"]
        hard = next((v for v in live if v.action == "reject" and v.stage in SCHEMA_STAGES), None)
        policy = PolicyInputs(
            hard_reject_code=hard.code if hard else None,
            review_codes=tuple(
                dict.fromkeys(
                    v.code for v in live if v.action in ("queue_review", "reject") and v.stage not in ("temporal_rules",) and v is not hard
                )
            ),
            hold_code=IDENTITY_PENDING if hold and "candidate" in (src_row.status, tgt_row.status) else None,
            rule_conflict=any(v.stage == "temporal_rules" for v in live),
            succession_effects=tuple(EndEffect(e.fact_id, e.valid_to) for e in ev.effects),
            invalidated=tuple(invalidated),
        )
        views = [FactView(str(f.id), f.authority_class, f.valid_from, f.valid_to, f.status) for f in facts]
        res = resolve(Incoming(authority, valid_from, valid_to), views, policy)
        return Assessment(
            res,
            ev.attributes or clean,
            valid_from,
            valid_to,
            unknown,
            [v.code for v in live],
            live[0].rule_kind if live else "conflict_policy",
        )

    # ---- recording and re-deciding ------------------------------------------------------------------------------------------------------------

    async def _apply_effects(
        self, conn: AsyncConnection, ws: uuid.UUID, fact_id: uuid.UUID, res: Resolution
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Historical succession ends a predecessor (it stays a true historical fact, ADR-023); a dispute flags the other side too (KGR-06)."""
        ended: list[uuid.UUID] = []
        disputed: list[uuid.UUID] = []
        if res.status == "active":
            for e in res.end_facts:
                await touch(conn, ws, "facts", uuid.UUID(e.fact_id), valid_to=e.valid_to, succeeded_by_fact_id=fact_id)
                ended.append(uuid.UUID(e.fact_id))
        for other in res.disputed:
            await touch(conn, ws, "facts", uuid.UUID(other), status="disputed", decision_code=res.code, decided_at=dt.datetime.now(dt.UTC))
            disputed.append(uuid.UUID(other))
        return ended, disputed

    async def record_fact(
        self, conn: AsyncConnection, ws: uuid.UUID, spec: OntologySpec, src: EpisodeSource, cand: FactCandidate, *, audit: Any = None
    ) -> FactOutcome:
        """A NEW extracted fact: assess, insert (id = the Graphiti edge uuid), add its evidence, apply succession/dispute effects, open the review."""
        a = await self.assess(
            conn,
            ws,
            spec,
            rel_type=cand.rel_type,
            source_id=cand.source_id,
            target_id=cand.target_id,
            authority=src.authority,
            classification=src.classification,
            valid_from=cand.valid_from,
            valid_to=cand.valid_to,
            attributes=cand.attributes,
            source_system=src.source_system,
            source_ref=src.source_ref,
            invalidated=cand.invalidated,
        )
        res = a.resolution
        now = dt.datetime.now(dt.UTC)
        await conn.execute(
            text(
                "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, attributes, valid_from, valid_from_unknown, valid_to, "
                "recorded_at, status, authority_class, classification, source_type, source_system, source_ref, ontology_version_id, fact_text, "
                "evidence_status, source_interaction_id, source_episode_uuid, decision_code, decided_at) "
                "VALUES (:id, :ws, :rel, :s, :t, CAST(:attrs AS jsonb), :vf, :vfu, :vt, :rec, :status, :auth, :cls, 'extracted', :ss, :sr, :ver, :text, "
                ":evs, :i, :ep, :code, :decided) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": cand.fact_id,
                "ws": ws,
                "rel": cand.rel_type,
                "s": cand.source_id,
                "t": cand.target_id,
                "attrs": json.dumps(a.attributes, default=str),
                "vf": a.valid_from,
                "vfu": a.valid_from_unknown,
                "vt": a.valid_to,
                "rec": src.recorded_at,
                "status": res.status,
                "auth": src.authority,
                "cls": src.classification,
                "ss": src.source_system,
                "sr": src.source_ref,
                "ver": src.ontology_version_id,
                "text": (cand.fact_text or f"{cand.rel_type}")[:2000],
                "evs": evidence_status(src.authority),
                "i": src.interaction_id,
                "ep": src.episode_uuid,
                "code": res.code,
                "decided": None if res.status == "active" else now,
            },
        )
        await self.add_evidence(conn, ws, cand.fact_id, src)
        ended, disputed = await self._apply_effects(conn, ws, cand.fact_id, res)
        out = FactOutcome(
            cand.fact_id, res.status, res.code, ended=ended, disputed=disputed, competing=[uuid.UUID(x) for x in res.competing]
        )
        if res.review is not None:
            out.review_type = res.review
            out.review_id, _ = await self.open_review(
                conn,
                ws,
                item_type=res.review,
                rule_kind=a.rule_kind if res.review == "policy_violation" else "conflict_policy",
                reason_code=res.code,
                audit=audit,
                fact_id=cand.fact_id,
                competing_fact_id=uuid.UUID(res.competing[0]) if res.competing else None,
                interaction_id=src.interaction_id,
                detail={"competing": list(res.competing), "disputed": list(res.disputed)},
            )
        metrics.extraction_facts_total.labels(status=res.status).inc()
        return out

    async def reevaluate_fact(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        spec: OntologySpec,
        fact_id: uuid.UUID,
        *,
        authority: str | None = None,
        invalidated: Sequence[Invalidation] = (),
        hold: bool = True,
    ) -> tuple[Resolution, FactOutcome]:
        """Runs a stored extracted fact through the same assessment again (after a merge, a `different_entity` decision or an `accept_claim`) and applies
        the new decision to the row. Reviews are the caller's business (it knows which item the change answers)."""
        row = (
            await conn.execute(
                text(
                    "SELECT id, rel_type, source_entity_id, target_entity_id, attributes, valid_from, valid_to, authority_class, classification, "
                    "source_system, source_ref, status FROM facts WHERE workspace_id = :ws AND id = :id FOR UPDATE"
                ),
                {"ws": ws, "id": fact_id},
            )
        ).one()
        use = authority or row.authority_class
        a = await self.assess(
            conn,
            ws,
            spec,
            rel_type=row.rel_type,
            source_id=row.source_entity_id,
            target_id=row.target_entity_id,
            authority=use,
            classification=row.classification,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            attributes=row.attributes or {},
            source_system=row.source_system or "platform",
            source_ref=row.source_ref or "review",
            exclude_fact_id=fact_id,
            invalidated=invalidated,
            hold=hold,
        )
        res = a.resolution
        now = dt.datetime.now(dt.UTC)
        changes: dict[str, Any] = {"status": res.status, "decision_code": res.code, "decided_at": None if res.status == "active" else now}
        if use != row.authority_class:
            changes.update(authority_class=use, evidence_status=evidence_status(use))
        if res.status == "active" and row.status != "active":
            changes["superseded_at"] = None
        await touch(conn, ws, "facts", fact_id, **changes)
        ended, disputed = await self._apply_effects(conn, ws, fact_id, res)
        return res, FactOutcome(
            fact_id, res.status, res.code, ended=ended, disputed=disputed, competing=[uuid.UUID(x) for x in res.competing]
        )


__all__ = [
    "IDENTITY_PENDING",
    "Assessment",
    "EpisodeSource",
    "ExtractedKnowledge",
    "FactCandidate",
    "FactOutcome",
    "Row",
    "touch",
]
