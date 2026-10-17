"""Scoring one memory scenario: what the platform recorded against the gold. Used by the evaluation and by the integration tests (the same judgement twice)."""

from __future__ import annotations

from typing import Any

from kg_memory.extract import asserts_decision


def _scope_of(scopes: list[tuple[str, str]]) -> str:
    kinds = {k for k, _ in scopes}
    return "subject" if kinds == {"subject"} else "workspace" if kinds == {"workspace"} else "+".join(sorted(kinds))


def matches(want: dict[str, Any], got: dict[str, Any]) -> bool:
    text = got["content"].lower()
    return (
        got["type"] == want["type"]
        and got["status"] == want["status"]
        and got["evidence_status"] == want["evidence_status"]
        and sorted(got["subjects"]) == sorted(want["subjects"])
        and _scope_of(got["scopes"]) == want["scope"]
        and all(p.lower() in text for p in want["must_contain"])
        and not any(p.lower() in text for p in want["must_not_contain"])
        and sorted(got["linked_facts"]) == sorted(want["linked_facts"])
    )


def score(
    gold: dict[str, Any], memories: list[dict[str, Any]], stage: dict[str, Any], source_text: str, linked: set[str], reviews: int
) -> dict[str, Any]:
    """`memories`: dicts with type, status, evidence_status, content, subjects (names), scopes [(kind, ref)], linked_facts (rel types), origin.
    `stage`: the memory_propose stage as the API reports it (status, last_error_code, result). `linked`: the names of the entities the interaction was linked to (subjects and accepted mentions)."""
    exp = gold["expect"]
    want = list(exp["memories"])
    unmatched = list(memories)
    tp = 0
    for w in want:
        hit = next((m for m in unmatched if matches(w, m)), None)
        if hit is not None:
            unmatched.remove(hit)
            tp += 1
    result = stage.get("result") or {}
    stage_ok = stage.get("status") == exp["stage"]
    drops_ok = dict(result.get("drops") or {}) == exp["drops"] if exp["stage"] == "succeeded" else True
    never_scope = set(exp.get("never_scope", []))
    escalations = sum(1 for m in memories if asserts_decision(m["content"]) and not asserts_decision(source_text))
    leaks = sum(1 for m in memories for s in m["subjects"] if s not in linked)
    procedural_active = sum(1 for m in memories if m["type"] == "procedural" and m["status"] == "active" and m["origin"] == "extracted")
    scope_violations = sum(1 for m in memories if {k for k, _ in m["scopes"]} & never_scope)
    return {
        "id": gold["id"],
        "stage_ok": stage_ok,
        "drops_ok": drops_ok,
        "memories": {"tp": tp, "fp": len(unmatched), "fn": len(want) - tp, "unexpected": [m["content"][:60] for m in unmatched]},
        "reviews_ok": reviews == exp["reviews"],
        "escalation_violations": escalations,
        "unmapped_subject_leaks": leaks,
        "procedural_auto_activations": procedural_active,
        "scope_violations": scope_violations,
        "error_code_ok": exp.get("error_code") is None or stage.get("last_error_code") == exp["error_code"],
        "chat_calls": int(result.get("chat_calls", 0)),
        "prompt_tokens": int(result.get("prompt_tokens", 0)),
        "completion_tokens": int(result.get("completion_tokens", 0)),
    }
