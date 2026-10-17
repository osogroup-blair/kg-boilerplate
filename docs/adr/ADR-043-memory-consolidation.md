# ADR-043: Consolidation has two modes: deterministic reinforcement, and synthesis that a person approves

**Status:** Accepted  **Decision refs:** MEM-06, ADR-029 (memory model), ADR-030 (prompt safety), phase-8 plan §5.3  **Evidence:** `tests/integration/test_p8_consolidation.py`, `packages/memory/tests/test_consolidate_rules.py`, `make eval-consolidation`, `scripts/demo_p8.py` step 8

## Context
Agents and extraction write many near-identical memories ("prefers risks and trade-offs" five ways). Left alone, contexts fill with duplicates; folded naively, distinct claims are lost and hedges become decisions.

## Decision
A debounced job per (workspace, memory type, scope set) with two modes chosen by the ontology's `MemoryPolicy` (`consolidation: disabled | reinforce | synthesize`):
1. **Reinforce (no model).** Within one scope set, subject set and classification, memories are duplicates if normalised text is equal, or Jaccard ≥ `near_duplicate_jaccard` **and** cosine ≥ `near_duplicate_cosine` (both vectors required; without them only an exact duplicate folds). Memories whose assertions disagree (same subject and predicate, different object) never fold. The survivor is the highest authority, then the earliest, then the smallest id; the others are `superseded`, their evidence is carried over by reference, and **no new text is written**.
2. **Synthesize (model, always reviewed).** For an episodic bucket (within `episodic_window_days`) the model returns statements that each cite handles (`M1…`). `validate` refuses the whole answer for: fewer than two sources, a missing or foreign citation, a statement merging claims that share too few words, an invented number, date or name, an echoed instruction, or a decision asserted when no source decided (`escalated_claim`). A valid answer becomes a `proposed` memory with `consolidated_from` lineage and a `consolidation_proposal` review; approving it supersedes the sources, rejecting it changes nothing. Classification is the strictest of the sources and authority the weakest.
3. **Idempotent.** `consolidation_runs` is unique per (workspace, mode, input hash); a re-delivered job, or a second run over the same inputs, does nothing.
4. **Contained.** A private note and a shared memory are never in one group; egress policy applies to the synthesis call (`egress_denied` is a recorded outcome).

## Consequences
- Reinforcement is safe to leave on; synthesis never takes effect without a person.
- The validator is the contract: `make eval-consolidation` pins every drop reason; `make eval-consolidation-live` measures how often a real model passes it (user action).
