"""Generates eval/datasets/conflicts/v1/cases.json (seed 7: the matrix is exhaustive, so the seed only orders it).

The EXPECTED outcomes below are written from the documented rules (ADR-027, kg_ontology.conflicts' module docstring), NOT by calling `resolve`, so the suite is a check of the
implementation against the rules, not a snapshot of the implementation:

  existing fact of HIGHER authority  -> proposed / authority_conflict (a contradiction review)
  else a DATED succession flagged by Graphiti's invalidation (the new fact starts strictly later, the old one was open then) -> active, the old fact ends at the new start
  else EQUAL authority               -> disputed / equal_authority_conflict
  else LOWER authority               -> proposed / requires_correction
  no overlap, or no conflict at all  -> active / no_conflict"""

from __future__ import annotations

import json
from pathlib import Path

AUTHORITY = ["authoritative_system", "admin_asserted", "user_asserted", "conversation_report", "agent_generated"]
RANK = {a: len(AUTHORITY) - i for i, a in enumerate(AUTHORITY)}
OVERLAPS = ["rule_overlap", "invalidated_dated", "invalidated_undated", "disjoint", "invalidated_disjoint"]
OUT = Path(__file__).resolve().parents[1] / "datasets" / "conflicts" / "v1" / "cases.json"

# identity: (a, b, expected names_compatible)
NAMES = [
    ("Sarah Johnson", "Sarah Johnson", True),
    ("sarah johnson", "SARAH JOHNSON", True),
    ("Sarah Johnson", "S. Johnson", True),
    ("S. Johnson", "Sarah Johnson", True),
    ("Sarah", "Sarah Johnson", True),
    ("Johnson", "Sarah Johnson", True),
    ("José García", "Jose Garcia", True),
    ("Zoë Kravitz", "Zoe Kravitz", True),
    ("Müller", "Muller", True),
    ("Sarah  Johnson", "Sarah Johnson.", True),
    ("Sarah Johnson", "Sara Johnson", False),
    ("Sarah Johnson", "Sarah Jones", False),
    ("Michael", "Sarah Johnson", False),
    ("Sarah Johnson", "Johnson Sarah", False),
    ("Tom Reed", "Tim Reed", False),
    ("Priya Raman", "Priya Raman Iyer", False),
    ("A. Johnson", "Sarah Johnson", False),  # A is not an initial of Sarah
    (
        "S. Johnson",
        "Sam Johnson",
        True,
    ),  # an initial fits both: only a subject anchor links, and two candidates are an ambiguity (a review)
    ("", "Sarah", False),
    ("Sarah", "", False),
]
# identity: (a ids, b ids, expected conflicting_strong_ids)
IDS = [
    ({"hr": "E-1"}, {"hr": "E-2"}, True),
    ({"hr": "E-1"}, {"hr": "E-1"}, False),
    ({"hr": "E-1"}, {"crm": "C-9"}, False),
    ({}, {"hr": "E-1"}, False),
    ({"hr": "E-1", "crm": "C-1"}, {"hr": "E-1", "crm": "C-2"}, True),
    ({"hr": "E-1", "crm": "C-1"}, {"hr": "E-1"}, False),
]


def expected(incoming: str, existing: str, overlap: str) -> dict[str, object]:
    if overlap in ("disjoint", "invalidated_disjoint"):
        return {"status": "active", "code": "no_conflict", "review": None, "ends": 0}
    r_new, r_old = RANK[incoming], RANK[existing]
    if r_old > r_new:
        return {"status": "proposed", "code": "authority_conflict", "review": "contradiction", "ends": 0}
    if overlap == "invalidated_dated":
        return {"status": "active", "code": "no_conflict", "review": None, "ends": 1}
    if r_old == r_new:
        return {"status": "disputed", "code": "equal_authority_conflict", "review": "contradiction", "ends": 0}
    return {"status": "proposed", "code": "requires_correction", "review": "contradiction", "ends": 0}


def build() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    n = 0
    for incoming in AUTHORITY:
        for existing in AUTHORITY:
            for overlap in OVERLAPS:
                n += 1
                cases.append(
                    {
                        "id": f"conf-{n:03d}",
                        "kind": "authority",
                        "incoming": incoming,
                        "existing": existing,
                        "overlap": overlap,
                        "expect": expected(incoming, existing, overlap),
                    }
                )
    for i, (a, b, ok) in enumerate(NAMES, 1):
        cases.append({"id": f"name-{i:03d}", "kind": "name", "a": a, "b": b, "expect": {"compatible": ok}})
    for i, (a, b, bad) in enumerate(IDS, 1):
        cases.append({"id": f"ext-{i:03d}", "kind": "external_ids", "a": a, "b": b, "expect": {"conflict": bad}})
    return {"suite": "conflicts", "version": "v1", "seed": 7, "provenance": "synthetic only", "cases": cases}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    OUT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(data['cases'])} cases)")  # type: ignore[arg-type]
