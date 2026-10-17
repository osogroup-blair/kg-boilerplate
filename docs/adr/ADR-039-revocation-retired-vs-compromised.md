# ADR-039: Revocation is final, with two reasons: `retired` and `compromised`

**Status:** Accepted  **Decision refs:** AGT-05, SEC-03, the memory-poisoning threat, phase-7 plan §5.4, decision 5  **Evidence:** `tests/integration/test_p7_revocation.py`, `tests/security/test_revocation.py`, `scripts/demo_p7.py` step 9

## Context
Authentication already re-checks the database on every request (ADR-020), so a revoked key stops working at once. What was missing is the *resource side*: what a revoked agent leaves behind, and what to do when the agent was not merely retired but poisoned.

## Decision
`POST /v1/principals/{id}/revoke {reason: retired | compromised, note?}` runs in **one tenant transaction** and writes a `principal.revoke` ledger command and an audit row.

| Effect | `retired` | `compromised` |
|---|---|---|
| principal `revoked` (who, when, why); all keys revoked | ✓ | ✓ |
| working memories expire at once | ✓ | ✓ |
| agent-private memories frozen (`frozen_at`: no write, supersede or publish → 409 `memory_frozen`); erasure is P8 | ✓ | ✓ |
| proposals under review | kept; the review item shows a `submitter_revoked` badge | **rejected**, their review items closed (`submitter_compromised`) |
| shared findings it published; memories and facts extracted from its submissions | stand | **`disputed`** + a `source_compromised` review item — unless an independent source also supports them |
| its pending model stages and their jobs | continue | `skipped_by_policy` (`submitter_compromised`), jobs cancelled; a job already claimed skips itself at its gate |

1. **Visible on the next request**: its key and any JWT minted before the revoke → 401; an SDK/MCP call → 401 / `unauthenticated`; `GET /v1/context/{id}` for its builds → 404 for everyone but an admin, who sees the package re-materialised and labelled `principal_revoked`; act-as on it → 403 `not_actable`.
2. **Independent corroboration keeps a fact alive**: a fact or memory with evidence from another submitter, a command or an external reference is not disputed because one of its sources was compromised. Evidence that is itself derived (a fact, an episode, a memory) is not independent.
3. **Quarantine, not deletion.** `source_compromised` items have two decisions: `restore` (back to `active`, as it was) and `confirm_removal` (`rejected`, kept as a record). Contexts show a disputed memory only as a **caution** ("its source was later found compromised; not verified"), and a disputed fact as "Disputed … Not settled."
4. **Idempotent and final.** A second revoke is a 200 with no new effects. Escalating `retired → compromised` applies the quarantine (a compromise discovered later is a real case); the reverse never un-quarantines. There is no un-revoke: ids and keys never come back, a replacement is a new registration.
5. **Scope.** Only agents and apps are revoked through the API; users and service principals are not managed here.

## Consequences
- The memory-poisoning response is reviewable and reversible, which makes an over-eager clean-up cheap to undo (one decision per item).
- A revoked agent's private data stays frozen and invisible until P8's erasure.
- Facts extracted from a compromised app's *commands* are not quarantined by this mechanism (only submissions); an app compromise should be followed by a review of its commands in the ledger.
