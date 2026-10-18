# ADR-049: OIDC for people, API keys for agents and apps, break-glass by flag

**Status:** Accepted  **Decision refs:** DEC-03, SEC-03, phase-9 plan WP7  **Evidence:** `tests/integration/test_p9_oidc.py`, `packages/governance/tests/test_oidc.py`, `apps/admin/src/pages/p9.test.tsx`

## Decision
1. People sign in with the organisation's IdP: authorization code + PKCE; the API does the code exchange; the console holds an ID token in memory/sessionStorage and a rotating refresh token in an httpOnly, SameSite=Strict cookie scoped to `/v1/auth/oidc`, renewed with a `X-Requested-With: kg-console` header.
2. Tokens are verified with pinned algorithms (RS256/ES256 only), the issuer's JWKS (rolled over by `kid`, refetch rate-limited), issuer, audience and time. Groups map to roles per workspace (`oidc_group_mappings`); the principal is provisioned on first login by a SECURITY DEFINER function that also audits it.
3. Agents, apps and integrations keep API keys (scoped, revocable, grant-based).
4. In production a **human** principal's API key works only while `ALLOW_BREAKGLASS_KEYS=true`, and every use is audited.

## Consequences
Offboarding is a group change at the IdP (effective within one token lifetime, 15 min). The platform stores no passwords. A group mapped into two workspaces is ambiguous and is refused unless a workspace claim disambiguates.
