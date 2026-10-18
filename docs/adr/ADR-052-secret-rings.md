# ADR-052: Every signing or hashing secret is a ring of versions

**Status:** Accepted  **Decision refs:** F-01, phase-9 plan WP6  **Evidence:** `packages/common/tests/test_secrets.py`, `packages/governance/tests/test_rotation.py`, `tests/integration/test_p9_rotation.py`, `docs/runbooks/rotate-secrets.md`

## Decision
`SecretRing` (`version:secret,…` + an active version) for the JWT signing key (`kid`), the content HMAC and, with a dual-verify window, the API-key pepper (stored as `api_keys.pepper_id`, a fingerprint). New values use the active version; verification and lookups try every version; stored values name theirs (journal lines carry `kid`). The pepper window ends with `kg secrets expire-keys`. `secret_versions` records labels, status and dates, never a secret.

## Consequences
Rotation needs no downtime and no re-hash of data the platform cannot re-hash. A version still named by stored values (the journal, tombstones) must stay in the ring; removing it breaks verification loudly rather than silently.
