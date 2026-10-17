# ADR-038: The publish model and the `agent_generated` / `verified` labels

**Status:** Accepted  **Decision refs:** AGT-01, AGT-02, AGT-04, AT-14, MEM-03, phase-7 plan §5.3, decision 4  **Evidence:** `tests/integration/test_p7_publish.py`, `packages/memory/tests/fixtures/policy/decisions.yaml`, `scripts/demo_p7.py` steps 3–7

## Context
Research Agent finds something; Planning Agent should be able to use it, knowing where it came from, without a person approving every sentence, and without an agent ever being able to rewrite policy or the way other agents behave.

## Decision
1. **A grant carries privileges** (`read`, `propose`, `publish`, a subset; the migration backfills `{read, propose}`; `publish` is never implicit). `read` is required with the others. Reading a scope needs `read`; writing into it needs `propose`; letting a shared finding take effect without review needs `publish` **on every target shared scope**.
2. **Validation (AGT-04).** A proposal's evidence must exist **and be visible to the proposer**: its own interactions; facts visible under the subject filter and touching a subject it is granted; memories it may read. Missing, someone else's, hidden and foreign evidence all return the **same `422 invalid_evidence`**. The classification is at least that of the evidence. Every scope must be writable with `propose`. The candidate HMAC deduplicates.
3. **Decision.** `active` when the ontology policy lets an agent's proposal through (`agent_shared_requires_review: false` for the type and the type auto-activates) **and** the agent holds `publish` on every target scope; otherwise `proposed` with a `memory_proposal` review item (reason `agent_shared_requires_review` or `publish_privilege_required`). **Procedural memory from an agent is always `proposed`** (AT-14), whatever the policy or the privileges; approving it makes it *guidance*, never policy.
4. **A finding that policy activated still gets a review item**, because it is `unverified`. A reviewer's `approve` is what **verifies** it (`evidence_status = verified`, `verified_by`, `verified_at`); `reject` withdraws it. A database trigger allows exactly one change of `evidence_status`: `unverified → verified`, with a named reviewer, on an agent finding. Authority stays `agent_generated` (ADR-027): verified evidence is not a higher authority.
5. **Labels.** Every agent-originated memory has `origin = proposal`, `authority_class = agent_generated` (`agent_generated: true` in API bodies) and an `evidence_status`. Memory search hits and context items carry both; the rendered text says `(agent_generated, unverified)` or `(agent_generated, verified)` for such a memory in both structured and extractive modes.
6. **`evidence_required`** (a request field that can only *narrow* a profile, or the profile's own setting) **excludes unverified agent findings** with the trace reason `unverified_agent_finding`; ordinary evidence still satisfies it. After a reviewer verifies the finding it is included, labelled `verified`.
7. **Private working notes never cross** (AGT-02): scopes `agent`/`session` are visible to their owner only (and the admin's audited reveal), on every surface (search, list, context, usage).

## Consequences
- The privilege is per scope, so one agent can publish to a project and merely propose to a person.
- An agent can be useful immediately and still be distinguishable from verified knowledge, by people and by `evidence_required`.
- A compromised publisher's findings are findable by `origin = proposal` + `created_by`, which is what ADR-039 uses.
