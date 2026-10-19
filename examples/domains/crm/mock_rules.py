"""Mock-router answers for the CRM conversations (offline runs only), generated from events/timeline.json so the data stays in one place.

Every structured request the pipeline can make for these conversations is covered (lesson X3); a gap fails loudly under the strict mock."""

from __future__ import annotations

from typing import Any

ALIAS = "oso/general"


def type_ids(entity_types: list[str]) -> dict[str, int]:
    """Graphiti's numbering of the ontology's entity types: alphabetical, from 1 (0 is the generic Entity)."""
    return {name: i for i, name in enumerate(sorted(entity_types), start=1)}


def rules_for(conversations: list[dict[str, Any]], entity_type_of: dict[str, str], entity_types: list[str]) -> list[dict[str, Any]]:
    ids = type_ids(entity_types)
    out: list[dict[str, Any]] = []
    for c in conversations:
        marker = c["content"][:60]  # a stretch of the text that only this conversation contains
        out.append(
            {
                "when": {"alias": ALIAS, "schema_name": "ExtractedEntities", "contains": [marker]},
                "response": {
                    "extracted_entities": [
                        {"name": n, "entity_type_id": ids[entity_type_of[n]], "episode_indices": [0]} for n in c["entities"]
                    ]
                },
            }
        )
        names = c["entities"]  # node resolution (the pipeline asks when a workspace already has nodes) and node summaries
        out.append(
            {
                "when": {"alias": ALIAS, "schema_name": "NodeResolutions", "contains": [marker]},
                "response": {"entity_resolutions": [{"id": i, "name": n, "duplicate_candidate_id": -1} for i, n in enumerate(names)]},
            }
        )
        out.append(
            {
                "when": {"alias": ALIAS, "schema_name": "SummarizedEntities", "contains": [marker]},
                "response": {"summaries": [{"name": n, "summary": f"{n}."} for n in names]},
            }
        )
        out.append({"when": {"alias": ALIAS, "schema_name": "ExtractedEdges", "contains": [marker]}, "response": {"edges": []}})
        for type_name, attrs in c.get(
            "attributes", {}
        ).items():  # entity types with attributes beyond the name get one attribute-extraction call per node
            out.append({"when": {"alias": ALIAS, "schema_name": type_name, "contains": [marker]}, "response": attrs})
        m = c["memory"]
        if m is None:  # a note the agent submits as evidence: no memory of its own
            out.append({"when": {"alias": ALIAS, "schema_name": "MemoryCandidates", "contains": [marker]}, "response": {"candidates": []}})
            continue
        out.append(
            {
                "when": {"alias": ALIAS, "schema_name": "MemoryCandidates", "contains": [marker]},
                "response": {
                    "candidates": [
                        {
                            "type": m["type"],
                            "content": m["content"],
                            "summary": m["summary"],
                            "subject_names": m["subject_names"],
                            "importance": 0.8,
                            "reason_code": "notable_discussion",
                            "occurred_at": c["occurred_at"],
                            "effective_from": None,
                            "related_fact_ids": [],
                        }
                    ]
                },
            }
        )
    return out
