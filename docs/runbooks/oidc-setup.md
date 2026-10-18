# Runbook: single sign-on for the console (OIDC)

Humans sign in through the organisation's identity provider; agents, apps and integrations keep API keys. The platform verifies RS256/ES256 tokens only (algorithms are pinned; `none` and HMAC are never accepted), maps IdP **groups to roles** per workspace, provisions the principal on first login and audits it.

## Configure
At the IdP create a **public client** (authorization code + PKCE, no client secret) with the redirect URI `https://<public host>/login/callback`, scopes `openid profile email groups`, and a `groups` claim in the ID token.
```
AUTH_OIDC_ENABLED=true
OIDC_ISSUER=https://idp.example.com/                # its discovery document and JWKS are derived from this
OIDC_AUDIENCE=<client id>                           # the `aud` the token must carry
OIDC_CLIENT_ID=<client id>
OIDC_REDIRECT_URIS=["https://kg.example.com/login/callback"]    # exact match, nothing else is accepted
# OIDC_GROUPS_CLAIM=groups  OIDC_WORKSPACE_CLAIM=kg_workspace  (optional; the workspace claim picks one workspace when groups map into several)
```
A confidential client works too (`OIDC_CLIENT_SECRET` via the `oidc_client_secret` Docker secret).

## Map groups to roles (per workspace)
Bootstrap with the break-glass key `kg workspaces create` printed (works while `ALLOW_BREAKGLASS_KEYS=true`), then:
```bash
curl -X POST https://<host>/v1/oidc-group-mappings -H "authorization: Bearer $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"group_name":"kg-admins","roles":["workspace_admin"]}'
```
(the console has no mapping editor yet; this endpoint, with `GET`/`DELETE` on the same path, is the interface). Roles: `workspace_admin, operator, reviewer, app_writer, agent, reader`. A group mapped into two workspaces is ambiguous by design; give each workspace its own group, or set the workspace claim.

## Verify and close the back door
1. Sign in through the console as a mapped user; `GET /v1/me` shows the roles. Remove the user from the group: the next token refresh (≤ 15 min) loses the roles.
2. Set `ALLOW_BREAKGLASS_KEYS=false` and redeploy. Human (user) API keys now answer 401; every use before that was audited (`breakglass_uses_total`). Keep one sealed break-glass key per workspace admin in the password manager, and turn the flag on only for an incident.

## Troubleshooting
`oidc_logins_total{outcome}` counts `ok`, `provisioned` (first login), `unmapped` (the user's groups map to no role in any workspace) and `rejected` (the token failed verification; the log line names the reason without the token: `unknown_kid` after a JWKS roll-over (the refetch is rate-limited, wait a minute), `algorithm_not_allowed`, `jwks_unavailable`, `discovery_issuer_mismatch`, an expired or wrong-audience token). A token from an issuer other than `OIDC_ISSUER` is never accepted. Clock skew above the 60 s leeway needs NTP on the hosts.
