"""P6 test rig: candidate builders for the pure-core tests (no database)."""

from __future__ import annotations

import datetime as dt
import uuid

from kg_common.tokens import TokenCounter
from kg_context.models import Candidate, Evidence
from kg_context.profiles import EffectiveProfile, resolve
from kg_context.rank import assign_tiers, rank
from kg_context.render import assign_handles

COUNTER = TokenCounter()
NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


def profile(name: str = "planning") -> EffectiveProfile:
    return resolve(name)


def fact(
    text: str = "Sarah Johnson works at Acme as Engineering Manager",
    *,
    section: str = "current_facts",
    owner: str = "s1",
    evidence: bool = True,
    relevance: float = 0.8,
    temporal: str = "current",
    rel: str = "WORKS_AT",
    fid: str | None = None,
    verified: bool = True,
    qualifier: str = "",
) -> Candidate:
    fid = fid or str(uuid.uuid4())
    return Candidate(
        key=f"fact:{fid}",
        kind="fact",
        id=fid,
        section=section,
        text=text,
        raw_text=text,
        qualifier=qualifier,
        label="verified" if verified else "unverified",
        temporal=temporal,
        evidence_status="verified" if verified else "unverified",
        authority_class="admin_asserted",
        rel_type=rel,
        subject_ids=(owner,),
        evidence=[Evidence("command", str(uuid.uuid5(uuid.NAMESPACE_OID, fid)), "hr", "evt_002")] if evidence else [],
        evidence_n=1 if evidence else 0,
        incident=True,
        relevance=relevance,
        source="facts",
        assertion=(owner, rel.lower(), "acme"),
    )


def memory(
    text: str = "Sarah discussed a possible software business with Michael.",
    *,
    mtype: str = "episodic",
    relevance: float = 0.7,
    evidence: bool = True,
    mid: str | None = None,
    verified: bool = False,
    importance: float = 0.5,
    content: str | None = None,
    related: tuple[str, ...] = (),
) -> Candidate:
    mid = mid or str(uuid.uuid4())
    return Candidate(
        key=f"memory:{mid}",
        kind="memory",
        id=mid,
        section={"episodic": "episodic", "semantic": "semantic", "procedural": "procedural", "working": "working"}[mtype],
        text=text,
        raw_text=content or text,
        qualifier="" if verified or mtype == "procedural" else " (unverified)",
        label="guidance" if mtype == "procedural" else ("verified" if verified else "unverified"),
        temporal="current",
        evidence_status="verified" if verified else "unverified",
        authority_class="conversation_report",
        memory_type=mtype,
        subject_ids=("s1",),
        evidence=[Evidence("interaction", str(uuid.uuid5(uuid.NAMESPACE_OID, mid)))] if evidence else [],
        evidence_n=1 if evidence else 0,
        relevance=relevance,
        importance=importance,
        related_fact_ids=related,
        source="memory",
    )


def prepare(cands: list[Candidate], prof: EffectiveProfile | None = None):  # type: ignore[no-untyped-def]
    """Rank, tier and give handles, as the service does after merging."""
    prof = prof or profile()
    ranked = rank(cands, prof)
    assign_tiers(ranked, prof)
    book = assign_handles(ranked)
    return ranked, book
