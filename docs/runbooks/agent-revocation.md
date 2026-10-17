# Runbook: revoking an agent, including the compromised path

Revocation is **final**: ids and keys never come back; a replacement is a new registration. Choose the reason honestly, because it decides what happens to what the agent produced.

## Retired
The agent is being decommissioned or replaced. Console: Agents & Access → the principal → **Revoke…** → *Retired*, type the handle, confirm (or `kg principals revoke <handle> --reason retired`).
- Its keys stop working on the very next request (HTTP, SDK and MCP alike; a JWT minted earlier too).
- Working memories expire now; private memories are frozen (they can no longer be written or published; erasure is a later phase).
- Its open proposals stay in the Review Queue with a **submitter revoked** badge: decide them as usual.
- What it published stands.

## Compromised
Its key leaked, or its behaviour cannot be trusted (it may have been fed poisoned content or told to write it). Same dialog, *Compromised* (`--reason compromised`). In addition:
- Its **unreviewed proposals are rejected**.
- What it **published**, and the memories and facts **extracted from its submissions**, become **disputed**, each with a `source_compromised` review item. A fact or memory that an independent source also supports (another submitter's interaction, a command, an external reference) is **not** disputed.
- Its pending model processing is skipped (`submitter_compromised`).
- Other agents' contexts show the disputed items **only as a caution** ("its source was later found compromised; not verified"), never as knowledge.

### Working the queue
Review Queue → type **source compromised**. For each item read what it is (the memory or the fact, with its evidence), then decide:
- **Restore** — the source was not the problem after all (a published finding you verified yourself, a fact confirmed elsewhere). It returns to `active`.
- **Confirm removal** — it stays out. The record is kept as `rejected`, not deleted.
The effect is on the next context build. Nothing is deleted by revocation; erasure is a separate, later capability.

### After
- Rotate anything the agent could read that is secret outside the platform (the platform cannot know).
- Look at its **Activity** tab (last 30 days: interactions by status, proposals by status, published and verified findings, context builds, denials) and the audit log (`principal.revoked`, `key.revoked`, `grant.removed`).
- Facts that arrived through a compromised **app's commands** are not quarantined automatically: review `/v1/commands` for its actor.
- Register the replacement (see [agent-onboarding](agent-onboarding.md)); it starts with no inherited keys, grants or private memory.

## Escalating later
A principal retired last month and found compromised today: revoke it again as *compromised*. The quarantine applies; a later *retired* never un-quarantines anything.

## Verifying
`kg principals activity <handle>` · `GET /v1/context/{id}` for one of its builds as an admin (labelled `principal_revoked`; other agents get 404) · `make demo-p7` step 9 runs the whole path.
