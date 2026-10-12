"""Reconciliation: Graphiti's output -> platform truth (P4 §5.2, ADR-027).

Everything below runs in ONE tenant transaction and reads/writes Postgres only. Graphiti said what a conversation contained; the platform decides what that
means for what it already knows:

* a node Graphiti resolved to an existing entity is accepted only if the interaction NAMES that entity; otherwise the mention is forked into a new candidate
  entity with an identity review (names alone never link, KGR-02);
* a new node is linked to the entity the interaction names when its name is a variant of that entity's name; otherwise it becomes an entity, flagged for
  review when it resembles another one;
* a new edge becomes an extracted fact (unverified) through the rule catalog and the conflict policy;
* an existing edge Graphiti invalidated, expired or edited is NOT accepted when its fact outranks the episode: it is counted as a revert, tied to the claim
  that caused it (one contradiction review), and the projector puts it back;
* attribute values a model proposed for an entity are recorded as claims, never written.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from kg_common.ids import uuid7
from kg_governance.audit import CommandType
from kg_graph.scoped import EpisodeResult, GEdge, GNode, ScopedGraph
from kg_knowledge.extracted import EpisodeSource, ExtractedKnowledge, FactCandidate, FactOutcome
from kg_knowledge.merge import ledger_command
from kg_knowledge.specs import SpecCache
from kg_obs import metrics
from kg_ontology import OntologySpec
from kg_ontology.conflicts import Invalidation, rank
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_extraction import identity
from kg_extraction.identity import Anchor, Candidate


@dataclass
class Revert:
    kind: str  # invalidation | attributes
    fact_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None


@dataclass
class ReconcileSummary:
    new_entities: list[uuid.UUID] = field(default_factory=list)
    linked_entities: list[uuid.UUID] = field(default_factory=list)  # existing entities the interaction itself names
    forked_entities: list[uuid.UUID] = field(default_factory=list)  # mentions Graphiti merged by name that the platform kept apart
    name_variants: int = 0  # new nodes folded into the entity the interaction names
    fact_ids: list[uuid.UUID] = field(default_factory=list)
    facts_by_status: dict[str, int] = field(default_factory=dict)
    evidence_added: int = 0
    review_ids: list[uuid.UUID] = field(default_factory=list)
    reverts: list[Revert] = field(default_factory=list)
    claims: int = 0
    skipped_edges: int = 0

    def count_status(self, status: str) -> None:
        self.facts_by_status[status] = self.facts_by_status.get(status, 0) + 1


@dataclass
class Inputs:
    """What the interaction itself names (resolved at acceptance) and who submitted it."""

    subject_ids: list[uuid.UUID]
    actor_ids: list[uuid.UUID]
    submitter_id: uuid.UUID


class Reconciler:
    def __init__(self, extracted: ExtractedKnowledge, specs: SpecCache, *, trgm_min: float, cos_min: float):
        self._x, self._specs, self._trgm, self._cos = extracted, specs, trgm_min, cos_min

    # ---- entities -------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    async def _entity(conn: AsyncConnection, ws: uuid.UUID, entity_id: uuid.UUID) -> Row[Any] | None:
        """The entity row, following a merge to the canonical entity (a merged id still resolves, KGR-01)."""
        for _ in range(5):
            row = (
                await conn.execute(
                    text(
                        "SELECT id, type_key, display_name, aliases, attributes, status, origin, merged_into_id, ontology_version_id FROM entities "
                        "WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"ws": ws, "id": entity_id},
                )
            ).first()
            if row is None or row.merged_into_id is None:
                return row
            entity_id = row.merged_into_id
        return None

    async def _anchors(self, conn: AsyncConnection, ws: uuid.UUID, inputs: Inputs) -> list[Anchor]:
        out: dict[uuid.UUID, Anchor] = {}
        for eid in [*inputs.subject_ids, *inputs.actor_ids]:
            row = await self._entity(conn, ws, eid)
            if row is not None and row.status not in ("deleted",) and row.type_key != "Entity":
                out[row.id] = Anchor(row.id, row.type_key, (row.display_name, *row.aliases))
        return list(out.values())

    # ---- the pass -------------------------------------------------------------------------------------------------------------------------

    async def reconcile(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        spec: OntologySpec,
        result: EpisodeResult,
        inputs: Inputs,
        graph: ScopedGraph,
        *,
        audit: Any,
    ) -> ReconcileSummary:
        out = ReconcileSummary()
        anchors = await self._anchors(conn, ws, inputs)
        anchor_ids = {a.id for a in anchors}
        embeddings = await graph.read_name_embeddings([n.uuid for n in result.nodes])
        node_map: dict[str, uuid.UUID] = {}
        forked: set[str] = set()

        for node in sorted(result.nodes, key=lambda n: n.uuid):
            entity_id, kind = await self._resolve_node(
                conn, ws, src, spec, node, anchors, anchor_ids, embeddings.get(node.uuid), inputs, graph, out, audit
            )
            node_map[node.uuid] = entity_id
            if kind == "fork":
                forked.add(node.uuid)
            await self._claims(conn, ws, src, spec, node, entity_id, out)

        await self._edges(conn, ws, src, spec, result, node_map, forked, out, audit)
        for revert in out.reverts:
            metrics.extraction_reverts_total.labels(kind=revert.kind).inc()
        return out

    async def _resolve_node(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        spec: OntologySpec,
        node: GNode,
        anchors: list[Anchor],
        anchor_ids: set[uuid.UUID],
        embedding: tuple[float, ...] | None,
        inputs: Inputs,
        graph: ScopedGraph,
        out: ReconcileSummary,
        audit: Any,
    ) -> tuple[uuid.UUID, str]:
        node_id = uuid.UUID(node.uuid)
        label = next((x for x in sorted(node.labels) if x in spec.entity_types), None)
        existing = await self._entity(conn, ws, node_id)

        if existing is not None:  # Graphiti resolved this mention to an entity the platform already has
            if existing.id in anchor_ids:
                await self._x.add_mention(
                    conn, ws, src, entity_id=existing.id, node_uuid=node.uuid, resolution="subject_ref", status="accepted"
                )
                out.linked_entities.append(existing.id)
                metrics.extraction_entities_total.labels(resolution="subject_ref").inc()
                return existing.id, "linked"
            return await self._fork(conn, ws, src, node, existing, embedding, graph, out, audit)

        type_key = label or "Entity"
        anchor = identity.anchor_for(node.name, type_key, anchors) if label else None
        if anchor is not None:  # a spelling of an entity the interaction names: deterministic, with the entity as lineage
            await self._x.add_alias(conn, ws, anchor.id, node.name)
            await self._x.add_mention(conn, ws, src, entity_id=anchor.id, node_uuid=node.uuid, resolution="subject_ref", status="accepted")
            await ledger_command(
                conn,
                ws,
                command_type=CommandType.ENTITY_MERGE,
                key=f"merge:{node_id}:{anchor.id}",
                actor_principal_id=inputs.submitter_id,
                ontology_version_id=src.ontology_version_id,
                payload={"from": node.uuid, "into": str(anchor.id), "reason": "subject_ref"},
                result={"entity_id": str(anchor.id), "merged_node_uuid": node.uuid, "entity_ids": [str(anchor.id)], "fact_ids": []},
                source_system="platform",
                source_ref="subject_ref",
                projection="not_applicable",
            )
            out.name_variants += 1
            out.linked_entities.append(anchor.id)
            metrics.extraction_entities_total.labels(resolution="name_variant").inc()
            return anchor.id, "variant"

        candidates: list[Candidate] = []
        if label is not None:
            candidates = await identity.find_candidates(
                conn,
                graph,
                ws,
                name=node.name,
                type_key=label,
                candidate_id=node_id,
                exclude=[node_id],
                embedding=embedding,
                trgm_min=self._trgm,
                cos_min=self._cos,
            )
        status = "untyped" if label is None else ("candidate" if candidates else "active")
        await self._x.insert_entity(conn, ws, src, entity_id=node_id, type_key=type_key, name=node.name, status=status)
        out.new_entities.append(node_id)
        if candidates:
            await self._identity_review(
                conn, ws, src, subject=candidates[0].entity_id, candidate=node_id, candidates=candidates, out=out, audit=audit
            )
            await self._x.add_mention(conn, ws, src, entity_id=node_id, node_uuid=node.uuid, resolution="new", status="needs_review")
            metrics.extraction_entities_total.labels(resolution="candidate").inc()
        else:
            await self._x.add_mention(conn, ws, src, entity_id=node_id, node_uuid=node.uuid, resolution="new", status="accepted")
            metrics.extraction_entities_total.labels(resolution="new").inc()
        return node_id, "new"

    async def _fork(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        node: GNode,
        existing: Row[Any],
        embedding: tuple[float, ...] | None,
        graph: ScopedGraph,
        out: ReconcileSummary,
        audit: Any,
    ) -> tuple[uuid.UUID, str]:
        """Graphiti merged this mention into `existing` because the names match. A name is not an identity: the mention becomes its own CANDIDATE entity
        and a reviewer decides. An open candidate with the same name that already waits against the same entity is reused (no second item)."""
        reuse = (
            await conn.execute(
                text(
                    "SELECT c.id FROM entities c JOIN review_items r ON r.workspace_id = c.workspace_id AND r.candidate_entity_id = c.id AND r.status = 'open' "
                    "AND r.item_type = 'identity_match' AND r.subject_entity_id = :s WHERE c.workspace_id = :ws AND c.status = 'candidate' "
                    "AND lower(c.display_name) = lower(:n) AND c.type_key = :t ORDER BY c.recorded_at LIMIT 1"
                ),
                {"ws": ws, "s": existing.id, "n": node.name, "t": existing.type_key},
            )
        ).first()
        if reuse is not None:
            await self._x.add_mention(
                conn, ws, src, entity_id=reuse.id, node_uuid=node.uuid, resolution="graphiti_auto", status="needs_review"
            )
            metrics.extraction_entities_total.labels(resolution="candidate").inc()
            return reuse.id, "fork"
        fork_id = uuid7()
        await self._x.insert_entity(conn, ws, src, entity_id=fork_id, type_key=existing.type_key, name=node.name, status="candidate")
        target_emb = (await graph.read_name_embeddings([str(existing.id)])).get(str(existing.id))
        base = Candidate(
            existing.id,
            trigram=1.0 if identity.normalize(node.name) == identity.normalize(existing.display_name) else None,
            cosine=round(identity.cosine(embedding, target_emb), 4) if embedding and target_emb else None,
            reasons=["graphiti_name_match"],
        )
        others = await identity.find_candidates(
            conn,
            graph,
            ws,
            name=node.name,
            type_key=existing.type_key,
            candidate_id=fork_id,
            exclude=[fork_id, existing.id],
            embedding=embedding,
            trgm_min=self._trgm,
            cos_min=self._cos,
            limit=4,
        )
        await self._identity_review(conn, ws, src, subject=existing.id, candidate=fork_id, candidates=[base, *others], out=out, audit=audit)
        await self._x.add_mention(conn, ws, src, entity_id=fork_id, node_uuid=node.uuid, resolution="new", status="needs_review")
        out.new_entities.append(fork_id)
        out.forked_entities.append(fork_id)
        metrics.extraction_entities_total.labels(resolution="fork").inc()
        return fork_id, "fork"

    async def _identity_review(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        *,
        subject: uuid.UUID,
        candidate: uuid.UUID,
        candidates: list[Candidate],
        out: ReconcileSummary,
        audit: Any,
    ) -> None:
        review_id, _ = await self._x.open_review(
            conn,
            ws,
            item_type="identity_match",
            rule_kind="identity_resolution",
            reason_code="similar_entity",
            audit=audit,
            subject_entity_id=subject,
            candidate_entity_id=candidate,
            interaction_id=src.interaction_id,
            detail={"candidates": [c.as_detail() for c in candidates]},
        )
        out.review_ids.append(review_id)

    async def _claims(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        spec: OntologySpec,
        node: GNode,
        entity_id: uuid.UUID,
        out: ReconcileSummary,
    ) -> None:
        """Attribute values Graphiti attached to the node. Only declared, non-sensitive, scalar attributes that differ from what Postgres holds; always
        claims (ADR-027). A claim on a COMMAND-origin entity is a thing Graphiti tried to change: counted as a revert."""
        row = await self._entity(conn, ws, entity_id)
        if row is None:
            return
        declared = spec.entity_types.get(row.type_key)
        if declared is None:
            return
        held = dict(row.attributes or {})
        claims = {
            k: v
            for k, v in node.attributes.items()
            if k in declared.attributes and k != "name" and not declared.attributes[k].sensitive and v is not None and held.get(k) != v
        }
        if not claims:
            return
        added = await self._x.add_claims(conn, ws, src, entity_id=row.id, claims=claims)
        out.claims += added
        if row.origin == "command" and added:
            out.reverts.append(Revert("attributes", entity_id=row.id))

    # ---- edges ----------------------------------------------------------------------------------------------------------------------------

    async def _edges(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        src: EpisodeSource,
        spec: OntologySpec,
        result: EpisodeResult,
        node_map: dict[str, uuid.UUID],
        forked: set[str],
        out: ReconcileSummary,
        audit: Any,
    ) -> None:
        ids = [uuid.UUID(e.uuid) for e in result.edges]
        known = {
            r.id: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT id, rel_type, status, source_type, authority_class, source_entity_id, valid_from, valid_to FROM facts "
                        "WHERE workspace_id = :ws AND id = ANY(:ids)"
                    ),
                    {"ws": ws, "ids": ids},
                )
            ).all()
        }
        new_edges: list[GEdge] = []
        invalidated: list[tuple[Row[Any], GEdge]] = []
        for e in sorted(result.edges, key=lambda x: x.uuid):
            row = known.get(uuid.UUID(e.uuid))
            if row is None:
                new_edges.append(e)
            elif (
                src.episode_uuid in e.episodes
            ):  # Graphiti recognised the statement as one the platform already holds: one more independent source
                if await self._x.add_evidence(conn, ws, row.id, src):
                    out.evidence_added += 1
            elif e.invalid_at != row.valid_to or (e.expired_at is not None and row.status == "active"):
                invalidated.append((row, e))  # Graphiti ended, expired or edited a fact this episode did not assert

        covered: set[uuid.UUID] = set()
        for e in new_edges:
            source, target = node_map.get(e.source_uuid), node_map.get(e.target_uuid)
            if source is None or target is None:
                out.skipped_edges += 1
                continue
            mine = tuple(
                Invalidation(str(row.id), g.invalid_at)
                for row, g in invalidated
                if row.rel_type == e.name and row.source_entity_id == source and e.source_uuid not in forked
            )
            outcome = await self._x.record_fact(
                conn,
                ws,
                spec,
                src,
                FactCandidate(
                    fact_id=uuid.UUID(e.uuid),
                    rel_type=e.name,
                    source_id=source,
                    target_id=target,
                    fact_text=e.fact,
                    valid_from=e.valid_at,
                    valid_to=e.invalid_at,
                    attributes=dict(e.attributes),
                    invalidated=mine,
                ),
                audit=audit,
            )
            self._tally(out, outcome)
            covered.update(outcome.competing)
            covered.update(outcome.ended)
            covered.update(outcome.disputed)

        for row, g in invalidated:  # what Graphiti changed that the platform does not accept
            authoritative = rank(row.authority_class) > rank(src.authority)
            if row.id in covered and not authoritative:
                continue  # a dated succession or dispute handled by the policy: the new state is already in the rows
            out.reverts.append(Revert("invalidation", fact_id=row.id))
            if authoritative and row.id not in covered and g.source_uuid not in forked:
                review_id, _ = await self._x.open_review(
                    conn,
                    ws,
                    item_type="graphiti_invalidation",
                    rule_kind="conflict_policy",
                    reason_code="graphiti_invalidation",
                    audit=audit,
                    fact_id=row.id,
                    interaction_id=src.interaction_id,
                    detail={"invalid_at": g.invalid_at.isoformat() if g.invalid_at else None},
                )
                out.review_ids.append(review_id)

    @staticmethod
    def _tally(out: ReconcileSummary, outcome: FactOutcome) -> None:
        out.fact_ids.append(outcome.fact_id)
        out.count_status(outcome.status)
        if outcome.review_id is not None:
            out.review_ids.append(outcome.review_id)
