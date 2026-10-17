# ADR-035: The Context Lab acts as an agent through the real endpoint

**Status:** Accepted  **Decision refs:** ADM-04, ADM-05, AT-12  **Evidence:** `tests/integration/test_p6_api.py` (`adm04`, `adm05`), Playwright `e2e/context.spec.ts`

## Context
To tune a profile or debug an agent's answer an operator must see **what that agent gets**. A console that builds with the operator's own wider rights shows the wrong thing, and a console with a privileged build path is a way around authorisation (ADM-05).

## Decision
1. **No second path.** The Lab calls `POST /v1/context/build` with `X-Act-As-Principal: <principal id>`. The service rebuilds the target's `Principal` (kind, roles) from Postgres and its scope grants and classification ceiling from `principal_scopes`, then runs `ContextService.build` unchanged. The target's key is never needed or used.
2. **Who may.** The caller must hold `context.lab` (workspace admins). The target must be in the caller's workspace (another workspace's principal is invisible under RLS, so the answer is the same 404 as an id that never existed), of kind `agent` or `app`, **active**, and hold no `workspace_admin` role; a user, a service, an admin or a revoked principal is a 403. Naming yourself is an ordinary build. `debug_snapshot` is judged against the caller, not the target.
3. **Where the header counts.** Only on `POST /v1/context/build`. On any other route it is ignored and `act_as_header_ignored` is logged (no value).
4. **Audit and trace.** Every Lab build writes `context.lab_build` (actor = the admin, `target_principal_id`, `context_id`, `profile`; ids only) in the admin's own transaction before the response leaves; the trace is stored under the target with `act_as_by`. The rate limit applies to the admin (the caller), not the target.
5. **The console** lists only active agents and apps, offers only the chosen profile's compression modes and a budget slider capped at its ceiling, and shows the package, the budget bar, the unavailable sources, the candidates by source, selected items with their score breakdown, excluded items with their reason, unknowns and conflicts, watermarks and per-stage latency, and a compare mode (two budgets side by side). Retrieved text is rendered as text nodes (React escapes it); only the handles the engine wrote are linked.

## Consequences
- What the Lab shows is by construction what the agent receives (a test builds both and compares the bytes).
- Act-as is a privilege-escalation surface by nature; the controls are narrow (admin → same-workspace agent/app, active, audited) and each has a negative test.
- P7 adds agent registration with allowed profiles; act-as then also honours them.
