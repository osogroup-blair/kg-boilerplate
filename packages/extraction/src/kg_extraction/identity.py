"""Identity resolution (KGR-01/02, ADR-028): who is this mention?

Deterministic evidence first, and only that links automatically:
  * an entity the interaction names (subject / actor refs, which already include external-id matches), or
  * a name VARIANT of such an entity ("S. Johnson" for "Sarah Johnson") when the interaction names exactly one entity of that type.
A name alone NEVER links. Everything else that merely resembles an existing entity becomes a review candidate (trigram name similarity, or the cosine of the
E5 name embeddings Graphiti already stored), never a silent merge.

The scoring helpers are pure (fixtures under packages/extraction/tests/fixtures/identity); only `find_candidates` reads Postgres and the graph.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from kg_graph.scoped import ScopedGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Case, accents and punctuation do not distinguish two spellings of one name."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(_NON_WORD.sub(" ", folded).split())


def _tokens(name: str) -> list[str]:
    return normalize(name).split()


def _initial_of(short: str, long: str) -> bool:
    return len(short) == 1 and long.startswith(short)


def names_compatible(a: str, b: str) -> bool:
    """True when two names can be the same person's name: equal after normalising; or the same family name with first names equal or one an initial of the
    other ("S. Johnson" ~ "Sarah Johnson"); or a single token that is the other's first or last token ("Sarah" ~ "Sarah Johnson"). Only ever used together
    with a subject anchor: on its own it links nothing."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if len(ta) == 1 or len(tb) == 1:
        single, other = (ta, tb) if len(ta) == 1 else (tb, ta)
        return len(other) >= 2 and single[0] in (other[0], other[-1])
    if ta[-1] != tb[-1]:
        return False
    fa, fb = ta[0], tb[0]
    return fa == fb or _initial_of(fa, fb) or _initial_of(fb, fa)


@dataclass(frozen=True)
class Anchor:
    """An entity the interaction itself names."""

    id: uuid.UUID
    type_key: str
    names: tuple[str, ...]  # display name and aliases


def anchor_for(name: str, type_key: str, anchors: Iterable[Anchor]) -> Anchor | None:
    """The one anchor of this type whose name or alias the mention's name is compatible with, or None (no anchor, or more than one: ambiguity is a review)."""
    same_type = [a for a in anchors if a.type_key == type_key]
    hits = [a for a in same_type if any(names_compatible(name, n) for n in a.names)]
    return hits[0] if len(hits) == 1 and len(same_type) == 1 else None


def conflicting_strong_ids(a: Iterable[tuple[str, str]], b: Iterable[tuple[str, str]]) -> bool:
    """Two entities that carry DIFFERENT values of the same strong identifier (same system) are different things, whatever their names say."""
    left, right = dict(a), dict(b)
    return any(system in right and right[system] != value for system, value in left.items())


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Candidate:
    entity_id: uuid.UUID
    trigram: float | None = None
    cosine: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return max(self.trigram or 0.0, self.cosine or 0.0)

    def as_detail(self) -> dict[str, object]:
        return {"entity_id": str(self.entity_id), "trigram": self.trigram, "cosine": self.cosine, "reasons": self.reasons}


def merge_scores(by_trigram: dict[uuid.UUID, float], by_cosine: dict[uuid.UUID, float], *, limit: int = 5) -> list[Candidate]:
    """Unions the two signals into at most `limit` candidates, best first (ties broken by id so the order is stable)."""
    out: dict[uuid.UUID, Candidate] = {}
    for eid, score in by_trigram.items():
        out.setdefault(eid, Candidate(eid)).trigram = round(score, 4)
        out[eid].reasons.append("name_similarity")
    for eid, score in by_cosine.items():
        out.setdefault(eid, Candidate(eid)).cosine = round(score, 4)
        out[eid].reasons.append("embedding_similarity")
    return sorted(out.values(), key=lambda c: (-c.score, str(c.entity_id)))[:limit]


async def find_candidates(
    conn: AsyncConnection,
    graph: ScopedGraph,
    ws: uuid.UUID,
    *,
    name: str,
    type_key: str,
    candidate_id: uuid.UUID | None,
    exclude: Sequence[uuid.UUID],
    embedding: Sequence[float] | None,
    trgm_min: float,
    cos_min: float,
    limit: int = 5,
) -> list[Candidate]:
    """Same-type entities of THIS workspace that resemble a mention. Pairs a reviewer already declared different are never candidates again."""
    key = normalize(name)
    rows = (
        await conn.execute(
            text(
                "SELECT e.id, GREATEST(similarity(lower(e.display_name), :n), COALESCE((SELECT max(similarity(lower(a), :n)) FROM unnest(e.aliases) a), 0)) AS s "
                "FROM entities e WHERE e.workspace_id = :ws AND e.type_key = :t AND e.status IN ('active','candidate') AND e.id <> ALL(:ex) "
                "AND NOT EXISTS (SELECT 1 FROM identity_non_matches nm WHERE nm.workspace_id = e.workspace_id AND nm.entity_a = LEAST(e.id, CAST(:cand AS uuid)) "
                "AND nm.entity_b = GREATEST(e.id, CAST(:cand AS uuid))) "
                "AND (similarity(lower(e.display_name), :n) >= :min OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE similarity(lower(a), :n) >= :min)) "
                "ORDER BY s DESC, e.id LIMIT :lim"
            ),
            {"ws": ws, "t": type_key, "n": key, "ex": list(exclude), "cand": candidate_id, "min": trgm_min, "lim": limit},
        )
    ).all()
    by_trigram = {r.id: float(r.s) for r in rows}
    by_cosine: dict[uuid.UUID, float] = {}
    if embedding is not None and _label_ok(type_key):
        hits = await graph.similar_nodes(type_key, embedding, exclude=[str(x) for x in exclude], min_score=cos_min, limit=limit)
        ids = [uuid.UUID(u) for u, _ in hits]
        live = {
            r.id
            for r in (
                await conn.execute(
                    text(
                        "SELECT e.id FROM entities e WHERE e.workspace_id = :ws AND e.id = ANY(:ids) AND e.status IN ('active','candidate') "
                        "AND NOT EXISTS (SELECT 1 FROM identity_non_matches nm WHERE nm.workspace_id = e.workspace_id AND nm.entity_a = LEAST(e.id, CAST(:cand AS uuid)) "
                        "AND nm.entity_b = GREATEST(e.id, CAST(:cand AS uuid)))"
                    ),
                    {"ws": ws, "ids": ids, "cand": candidate_id},
                )
            ).all()
        }
        by_cosine = {uuid.UUID(u): s for u, s in hits if uuid.UUID(u) in live}
    return merge_scores(by_trigram, by_cosine, limit=limit)


def _label_ok(type_key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", type_key)) and type_key != "Entity"
