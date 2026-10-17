"""Candidates from what the sources returned (pure; phase-6 plan §5.3 steps 3-5). One function per source row type; nothing here touches a database.

Relevance is the lexical-and-rank half of `ctx-rank.v1`'s first feature, computed once, here:
    a subject's own current fact   `current_facts: always` -> at least 0.6; `relevant` -> the term coverage of the task (a fact the task does not touch is below threshold)
    a graph hit that is not the subject's   0.7 * term coverage + 0.3 * source rank, and 0 without any term overlap (the graph always returns its top N; overlap is what makes it relevant)
    a memory hit   0.7 * its search score relative to the best hit + 0.3 * term coverage (a hit is already lexically or semantically matched, inside its scopes)"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from kg_knowledge.models import FactOut
from kg_memory.models import HitComponents, MemoryOut, SearchHit
from kg_memory.reads import ContextInfo
from kg_ontology.template import render_fact

from kg_context.models import Candidate, Evidence
from kg_context.planner import Plan
from kg_context.text import coverage, terms


@dataclass(frozen=True)
class SubjectInfo:
    id: str
    type: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    classification: str = "internal"


def _date(value: dt.datetime | None) -> str:
    return value.date().isoformat() if value else ""


def _paren(*parts: str) -> str:
    kept = [p for p in parts if p]
    return f" ({'; '.join(kept)})" if kept else ""


def phase(f: FactOut, ref: dt.datetime, known_at: dt.datetime | None) -> str:
    """current | historical | future | proposed | disputed | none, for a fact at `ref` (the as_of, or the database's now)."""
    status = f.status
    if known_at is not None and status == "superseded" and f.superseded_at is not None and f.superseded_at > known_at:
        status = "active"  # at known_at the platform still held it as current
    if status == "proposed":
        return "proposed"
    if status == "disputed":
        return "disputed"
    if status in ("rejected", "deleted"):
        return "none"
    if status == "superseded":
        return "historical"
    if f.valid_from is not None and f.valid_from > ref:
        return "future"
    if f.valid_to is not None and f.valid_to <= ref:
        return "historical"
    return "current"


def fact_statement(f: FactOut, template: str | None = None) -> str:
    """The fact in words: the text its source gave (an extracted fact), else the ontology's own deterministic template, as the graph renders it (no model)."""
    if f.fact_text:
        return f.fact_text
    public = {
        k: v for k, v in f.attributes.items() if isinstance(v, str | int | float | bool)
    }  # a redacted attribute is a mapping and is left out
    return render_fact(template, f.rel_type, f.source.name, f.target.name, public)


def attribute_text(f: FactOut, statement: str) -> str:
    """The fact's scalar attributes that its text does not already say (`role: Engineering Manager`), sorted by name. A redacted attribute (a mapping) is never shown."""
    shown = [
        f"{k.replace('_', ' ')}: {v}"
        for k, v in sorted(f.attributes.items())
        if isinstance(v, str | int | float | bool) and not isinstance(v, bool) and str(v) and str(v).lower() not in statement.lower()
    ]
    return "; ".join(shown)


def fact_evidence(f: FactOut) -> list[Evidence]:
    out: list[Evidence] = []
    for e in f.evidence:
        if e.kind == "command" and e.command_id is not None:
            out.append(Evidence("command", str(e.command_id), e.source_system, e.source_ref))
        elif e.interaction_id is not None:
            out.append(Evidence("episode" if e.episode_uuid else "interaction", str(e.interaction_id), e.source_system, e.source_ref))
    if not out and f.source_command_id is not None:
        out.append(Evidence("command", str(f.source_command_id), f.source_system, f.source_ref))
    elif not out and f.source_interaction_id is not None:
        out.append(Evidence("interaction", str(f.source_interaction_id), f.source_system, f.source_ref))
    seen: dict[tuple[str, str], Evidence] = {}
    for ev in out:
        seen.setdefault((ev.kind, ev.id), ev)
    return list(seen.values())


def fact_candidate(
    f: FactOut,
    *,
    plan: Plan,
    requested: list[str],
    ref: dt.datetime,
    description_terms: set[str],
    source: str,
    rank_hint: float = 0.0,
    template: str | None = None,
) -> Candidate:
    """A fact as a candidate. `requested` are the visible subject ids, in request order; a fact touching one is that subject's (incident) fact."""
    src, tgt = str(f.source.id), str(f.target.id)
    owners = [s for s in requested if s in (src, tgt)]
    others = [e for e in (src, tgt) if e not in owners]
    incident = bool(owners)
    temporal = phase(f, ref, plan.known_at)
    verified = f.evidence_status == "verified"
    statement = fact_statement(f, template)
    extra = attribute_text(f, statement)
    if temporal == "historical":
        section, label = "history", "historical"
        span = (
            f"valid {_date(f.valid_from) or 'earlier'} to {_date(f.valid_to) or 'later'}"
            if f.valid_from or f.valid_to
            else "no longer current"
        )
        qualifier = _paren(extra, span)
    elif incident and verified:
        section, label = "current_facts", "verified"
        qualifier = _paren(extra, f"since {_date(f.valid_from)}" if f.valid_from and not f.valid_from_unknown else "")
    elif incident:
        section, label = "unverified_facts", "unverified"
        qualifier = _paren(extra, f"reported {_date(f.recorded_at)}")
    else:
        section, label = "relevant_facts", "verified" if verified else "unverified"
        qualifier = _paren(extra, "" if verified else "unverified")
    fact_terms = set(
        terms(f"{statement} {f.rel_type.replace('_', ' ')} {' '.join(str(v) for v in f.attributes.values() if isinstance(v, str))}")
    )
    fact_terms |= description_terms
    cov = coverage(list(plan.query_terms), fact_terms)
    if incident:
        always = plan.profile.current_facts == "always" and temporal == "current"
        relevance = max(cov, 0.6) if always else cov
    else:
        relevance = 0.7 * cov + 0.3 * rank_hint if cov > 0 else 0.0
    evidence = fact_evidence(f)
    return Candidate(
        key=f"fact:{f.id}",
        kind="fact",
        id=str(f.id),
        section=section,
        text=statement,
        raw_text=statement,
        qualifier=qualifier,
        label=label,
        status=f.status,
        temporal=temporal,
        evidence_status=f.evidence_status,
        authority_class=f.authority_class,
        classification=f.classification,
        rel_type=f.rel_type,
        subject_ids=(*owners, *others),
        valid_from=f.valid_from,
        valid_to=f.valid_to,
        recorded_at=f.recorded_at,
        evidence=evidence,
        evidence_n=len(evidence),
        assertion=(src, f.rel_type.lower(), f.target.name.lower()),
        source=source,
        incident=incident,
        relevance=round(relevance, 6),
        importance=0.5,
        proximity=1.0 if incident else 0.5,
        age_days=max(0.0, (ref - f.recorded_at).total_seconds() / 86400.0),
        rank_hint=rank_hint,
    )


def memory_candidate(
    hit: SearchHit,
    info: ContextInfo | None,
    *,
    plan: Plan,
    requested: list[str],
    ref: dt.datetime,
    best_score: float,
    standing: bool = False,
) -> Candidate:
    """`standing` marks a memory that was not FOUND by the task but belongs to its working set (a subject's approved guidance, the project's shared memory): it is
    relevant by membership, so its relevance never falls below 0.5."""
    cov = coverage(list(plan.query_terms), set(terms(f"{hit.summary} {hit.content}")))
    relevance = 0.7 * (hit.score / best_score if best_score > 0 else 0.0) + 0.3 * cov
    if standing:
        relevance = max(relevance, 0.5)
    subjects = tuple(str(s) for s in hit.subject_ids)
    evidence = [Evidence(kind, ident) for kind, ident, _ in (info.evidence if info else ())]
    assertion = None
    if info is not None and info.assertion and info.assertion.get("predicate") is not None and info.assertion.get("subject_id"):
        assertion = (str(info.assertion["subject_id"]), str(info.assertion["predicate"]), str(info.assertion.get("object", "")).lower())
    verified = hit.evidence_status == "verified"
    guidance = hit.type == "procedural"
    agent_made = hit.authority_class == "agent_generated"
    agent_finding = agent_made and info is not None and info.origin == "proposal"
    # P7 (AGT-01): an agent's output says so, and says whether a reviewer has verified it
    qualifier = (
        ""
        if (verified and not agent_made) or guidance
        else f" ({'agent_generated, ' if agent_made else ''}{'verified' if verified else 'unverified'})"
    )
    return Candidate(
        key=f"memory:{hit.memory_id}",
        kind="memory",
        id=str(hit.memory_id),
        section={"episodic": "episodic", "semantic": "semantic", "procedural": "procedural", "working": "working"}[hit.type],
        text=hit.content
        if len(hit.content) <= plan.profile.item_chars
        else hit.summary,  # the whole statement when it fits: a derived summary is only its first sentence
        raw_text=hit.content,
        qualifier=qualifier,
        label="guidance" if guidance else "note" if hit.type == "working" else ("verified" if verified else "unverified"),
        status=hit.status,
        temporal="current",
        evidence_status=hit.evidence_status,
        authority_class=hit.authority_class,
        agent_finding=agent_finding,
        classification=hit.classification,
        memory_type=hit.type,
        subject_ids=subjects,
        valid_from=hit.effective_from,
        valid_to=hit.effective_until,
        recorded_at=hit.effective_from,
        evidence=evidence,
        related_fact_ids=tuple(str(x) for x in (info.related_fact_ids if info else ())),
        assertion=assertion,
        source="memory",
        incident=bool(set(subjects) & set(requested)),
        relevance=round(relevance, 6),
        importance=info.importance if info else hit.components.importance,
        evidence_n=hit.components.evidence_count,
        proximity=1.0 if set(subjects) & set(requested) else 0.5,
        age_days=max(0.0, (ref - hit.effective_from).total_seconds() / 86400.0),
        rank_hint=hit.score / best_score if best_score > 0 else 0.0,
    )


def conflict_candidate(
    key: str,
    text: str,
    *,
    fact_id: str | None,
    review_id: str | None,
    evidence: list[Evidence],
    subject_ids: tuple[str, ...],
    memory_id: str | None = None,
) -> Candidate:
    """A material disagreement: an unverified claim under review, a disputed fact, or two conflicting memories. Required tier: it is never silently dropped (CTX-05)."""
    return Candidate(
        key=key,
        kind="conflict",
        id=fact_id or memory_id,
        section="uncertainty",
        text=text,
        raw_text=text,
        label="conflict",
        status="open",
        temporal="none",
        evidence_status=None,
        evidence=evidence,
        review_ids=(review_id,) if review_id else (),
        subject_ids=subject_ids,
        source="conflicts",
        relevance=1.0,
        proximity=1.0,
        tier="required",
    )


def unknown_candidate(signal_key: str, label: str, subject: SubjectInfo) -> Candidate:
    text = f"No verified {label} for {subject.name}"
    return Candidate(
        key=f"unknown:{signal_key}:{subject.id}",
        kind="unknown",
        id=None,
        section="uncertainty",
        text=text,
        raw_text=text,
        label="unknown",
        status="open",
        temporal="none",
        evidence_status=None,
        subject_ids=(subject.id,),
        source="derived",
        relevance=1.0,
        proximity=1.0,
        tier="required",
    )


def notice_unknown(text: str) -> Candidate:
    """The single line an `insufficient_evidence` package says instead of inventing an answer (AT-09)."""
    return Candidate(
        key="unknown:no_authorized_records",
        kind="unknown",
        id=None,
        section="uncertainty",
        text=text,
        raw_text=text,
        label="unknown",
        status="open",
        temporal="none",
        evidence_status=None,
        source="derived",
        relevance=1.0,
        proximity=1.0,
        tier="required",
    )


def hit_from_memory(m: MemoryOut, score: float) -> tuple[SearchHit, ContextInfo]:
    """A memory read by id (re-materialisation) in the shape a search hit has, so ONE function (`memory_candidate`) builds every memory candidate."""
    hit = SearchHit(
        memory_id=m.id,
        type=m.type,
        status=m.status,
        summary=m.summary,
        content=m.content,
        evidence_status=m.evidence_status,
        authority_class=m.authority_class,
        classification=m.classification,
        scopes=m.scopes,
        subject_ids=m.subject_ids,
        effective_from=m.effective_from,
        effective_until=m.effective_until,
        score=score,
        matched_by="lexical",
        components=HitComponents(lex_rank=None, vec_rank=None, cosine=None, importance=m.importance, evidence_count=len(m.evidence)),
    )
    pointers: list[tuple[str, str, str | None]] = []
    facts: list[uuid.UUID] = []
    for e in m.evidence:
        if e.source_kind == "fact" and e.fact_id is not None:
            facts.append(e.fact_id)
            pointers.append(("fact", str(e.fact_id), None))
        elif e.source_kind == "interaction" and e.interaction_id is not None:
            pointers.append(("interaction", str(e.interaction_id), None))
        elif e.source_kind == "command" and e.command_id is not None:
            pointers.append(("command", str(e.command_id), None))
        elif e.source_kind == "memory" and e.memory_id is not None:
            pointers.append(("memory", str(e.memory_id), None))
        elif e.source_kind == "episode" and e.episode_uuid is not None:
            pointers.append(("episode", str(e.episode_uuid), None))
        elif e.source_kind == "external_ref" and e.external_ref is not None:
            pointers.append(("external", e.external_ref, e.external_ref))
    info = ContextInfo(
        evidence=tuple(pointers),
        related_fact_ids=tuple(dict.fromkeys(facts)),
        assertion=m.assertion,
        importance=m.importance,
        created_by=m.created_by,
        origin=m.origin,
    )
    return hit, info
