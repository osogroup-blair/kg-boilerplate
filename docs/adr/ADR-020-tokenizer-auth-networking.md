# ADR-020: Tokenizer and budget accounting; dev auth; dev networking

**Status:** Accepted for dev auth and networking; Provisional for the tokenizer margin (pending TK-02)  **Decision refs:** CTX-01, DEC-03  **Evidence:** TK-01, AU-01, NW-01

## Tokenizer and budget (CTX-01)
- The router picks the model, so the platform counts **its own output** (the context package), not router calls.
- **Count = ceil(max(o200k_base, cl100k_base) × 1.10)** using tiktoken. The two agree within 1% on English/JSON/UUID text but cl100k_base counts ~47% more on multilingual text, so the larger is used; a `chars/4` heuristic under-counts structured text (JSON, UUIDs, dates) by up to 60% and is forbidden for hard budgets.
- The tokenizer id string (`tiktoken:max(o200k_base,cl100k_base)+margin0.1`) is recorded in every context trace.
- The 10% margin is **provisional**: TK-02 measures router `prompt_tokens` for the same samples on each alias and raises the margin until the platform count ≥ observed for 100% of samples.

## Dev auth (DEC-03)
- Platform-issued API keys `kgk_<8-char prefix>_<secret>`: stored as prefix + SHA-256(pepper‖key), constant-time compare, optional expiry, immediate revocation (no cache layer), prefix displayed in the admin UI, plaintext shown once. Principals are workspace-bound; the workspace comes from the key record or token claim, **never** from a header or body.
- Optional 15-minute JWT (HS256, local signer) after key exchange. Rejected in tests: tampered claims, `alg=none`, wrong key, wrong audience, missing `workspace_id`, expired, algorithm confusion.
- **Library: PyJWT** (small, algorithms must be pinned, required-claim enforcement). authlib is an OAuth framework we do not need.
- **OIDC path (documented, not built):** verify RS256/ES256 against the IdP's JWKS (`PyJWKClient`), pin algorithms, require `iss/aud/exp`, map a configured claim (or group→workspace table) to `workspace_id`. The `Principal` shape is unchanged, so no other code moves.

## Dev networking (NW-01)
- The router binds `127.0.0.1` only. Verified on macOS Docker Desktop: a container reaches a host-loopback-only service through `http://host.docker.internal:<port>`, and a container's own `127.0.0.1` does not.
- Defaults: `OSO_ROUTER_BASE_URL=http://127.0.0.1:8780/v1` for host-run processes, `http://host.docker.internal:8780/v1` for containerised api/worker, with `extra_hosts: ["host.docker.internal:host-gateway"]` in compose for Linux CI. Do **not** rebind the router to `0.0.0.0`. Fallback if Docker Desktop changes behaviour: run api/worker on the host in dev.
- Not yet verified against the real router process (it was not running during the offline run); LV-01 and `make verify-router` from a container cover it.
