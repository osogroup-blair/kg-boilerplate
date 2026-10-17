# ADR-036: Agent and app registration, the trusted-source flag and model disclosure

**Status:** Accepted  **Decision refs:** AGT-03, AGT-05, SEC-03, ING-06, phase-7 plan §5.1, decisions 3, 6, 9  **Evidence:** `tests/integration/test_p7_registration.py`, `tests/integration/test_db_p7.py`, `tests/integration/test_p7_publish.py` (`ing06`), `scripts/demo_p7.py` steps 2 and 8

## Context
Until P7 an agent existed only because `kg seed` or the CLI made it. The blueprint asks for admin-managed identities with owners, grants, quotas and a policy for what the platform may do with the content they submit. Two earlier deferrals land here: *who is a system of record* (P2 decision 2) and *whether an agent's content may go to external models* (ING-06).

## Decision
1. **Registration creates `agent` and `app` principals only** (`POST /v1/principals`, workspace admins, `principal.manage`). Users and service principals stay seed/CLI-managed until human identity moves to OIDC (P9). **The role is fixed by the kind** (`agent` → `agent`, `app` → `app_writer`); a request that names a role, a workspace or any unknown field is a 422, so a registered principal can never be a workspace admin or a reviewer.
2. **A registration holds**: handle (unique per workspace → 409 `handle_taken`), display name, description, owner (an active principal of this workspace), `trusted_source` (apps only; a CHECK enforces it), `model_disclosure`, `allowed_profiles`, three quotas (null = the platform default) and its grants. Everything is audited with ids, enums and counts; the handle and the revocation note never reach audit, logs or metrics.
3. **Keys** are minted with a name and an optional expiry; several may be active at once (rotation); revoking one is immediate. **The plaintext is in the response once** (`Cache-Control: no-store`); the ledger stores the answer without it, and an idempotent replay returns the principal and `key.plaintext = null`. A lost key is replaced, never recovered.
4. **`trusted_source`** decides command authority: an app holding `app_writer` gets `authoritative_system` only when `trusted_source` is true; otherwise its commands carry `user_asserted` and lose conflicts against authoritative facts (ADR-027). The seeded `conversation_app` is trusted (a migration backfill and the seed), so the P2–P6 demos keep their meaning. New apps default to untrusted.
5. **`model_disclosure`** is *submission consent*: whether content this principal submits may be processed by external models. `denied` forces `consent_external = false` at interaction acceptance (`min(request, policy)`), so the model stages are `skipped_by_policy` and the router sees nothing (a spy test proves it). It also keeps the principal's context tasks from leaving: no query embedding (graph unavailable, memory lexical-only: `unavailable: model_disclosure_denied`) and no `summarized` compression (structured, reason `model_disclosure_denied`).
6. **`allowed_profiles`** limits which context profiles the principal may build (empty: every profile). It is judged in `ContextService`, so it applies to normal builds and to the Context Lab (which judges the TARGET's registration).
7. **Update** is `PATCH` with `If-Match`; a revoked principal is final (409).
8. **The registration API can be switched off** (`PRINCIPAL_API_ENABLED=false` → 503 `feature_disabled`); `kg principals …` (an HTTP client of the same API) is the fallback surface.

## Consequences
- Privilege escalation through registration is closed by construction (fixed roles), by the endpoint's single action, and by negative tests for every non-admin role and for the other workspace.
- Disclosure is a property of the principal, enforced where content enters (acceptance) and where it would leave (embedding, summary), not a flag the caller can raise.
- Trust in an app is an explicit, audited, reversible admin decision rather than an accident of its role.
