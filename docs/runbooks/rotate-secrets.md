# Runbook: rotating secrets (JWT keys, the key pepper, the content HMAC)

Every secret the platform signs or hashes with is a **ring of versions**, so a rotation never needs downtime and a leaked value stops mattering once its version is retired. `kg secrets status` shows each ring (version labels and fingerprints, never a secret), how many live API keys sit on each pepper, and the key versions the deletion journal names.

| Secret | Setting(s) | Rotation shape |
|---|---|---|
| JWT signing key | `AUTH_JWT_KEYS="kid:secret,kid:secret"`, `AUTH_JWT_ACTIVE_KID` | add → activate → wait one token lifetime → remove |
| API-key pepper | `AUTH_KEY_PEPPER`+`AUTH_KEY_PEPPER_ID`, `AUTH_KEY_PEPPER_PREVIOUS`+`AUTH_KEY_PEPPER_PREVIOUS_ID` | dual-verify window → keys re-hash on next use → force-expire the stragglers |
| Content HMAC | `CONTENT_HMAC_SECRETS="v1:secret,v2:secret"`, `CONTENT_HMAC_ACTIVE` | add → activate → keep old versions while anything stored names them |
| Payload encryption keys | `PAYLOAD_KEYS`, `PAYLOAD_ACTIVE_KID` | see [payload-key-rotation.md](payload-key-rotation.md) |
| Router keys, database passwords | Docker secrets (`deploy/compose/secrets/*`) | replace the file, `docker compose up -d` the services that read it |

Production refuses a secret under 32 bytes or equal to a development default at boot. Generate: `uv run kg secrets generate` (printed once; the platform never stores it). Put it in the secret manager first.

## 1. JWT signing key
1. `kg secrets generate` → new secret. Set `AUTH_JWT_KEYS="old:<old>,new:<new>"` (keep `AUTH_JWT_ACTIVE_KID=old`). Deploy API replicas (both keys verify, old still signs).
2. Set `AUTH_JWT_ACTIVE_KID=new`. Deploy. New tokens carry the new `kid`; tokens signed with `old` still verify.
3. Wait `AUTH_JWT_TTL_SECONDS` (15 min by default) plus the leeway.
4. Remove `old` from `AUTH_JWT_KEYS`. Deploy. Tokens still carrying `old` now fail with 401 and the console/SDK re-exchange their key.
5. `kg secrets record new --kind jwt --status active` and `kg secrets record old --kind jwt --status retired`.

If `old` leaked: skip the wait. Do steps 1, 2 and 4 immediately; everyone re-authenticates (revocation of principals is separate: see [agent-revocation.md](agent-revocation.md)).

## 2. API-key pepper (a window, not a flip)
API keys are stored as keyed hashes. A new pepper cannot re-hash a key the platform does not hold, so keys migrate **when they are next used**.
1. Set `AUTH_KEY_PEPPER_PREVIOUS=<old>`, `AUTH_KEY_PEPPER_PREVIOUS_ID=<old label, e.g. p1>`, `AUTH_KEY_PEPPER=<new>`, `AUTH_KEY_PEPPER_ID=<new label, e.g. p2>`. Deploy. A key hashed under the old pepper is accepted once and re-hashed under the new one (`key_rehashes_total` counts them; each is audited).
2. `kg secrets status` shows the live keys per pepper. Let the count under the old fingerprint fall for the window the owner chose (a week is typical). Keys that are never used stay on the old pepper.
3. Count what would stop: `kg secrets expire-keys --pepper <old fingerprint>`; then `--confirm`. Those keys stop working now (audited per workspace); their owners mint new ones (`kg keys create`).
4. Remove `AUTH_KEY_PEPPER_PREVIOUS*`. Deploy. `kg secrets record p2 --kind auth_pepper --status active`.

If the pepper leaked, the hashes are only as safe as the keys' entropy (48 random bytes): still do step 1 and run step 3 immediately.

## 3. Content HMAC
The content HMAC keys idempotency checks, suppression tombstones and the deletion journal's chain. Stored values name the version that produced them (journal lines carry `kid`).
1. `CONTENT_HMAC_SECRETS="v1:<old>,v2:<new>"`, `CONTENT_HMAC_ACTIVE=v1`; deploy. 2. `CONTENT_HMAC_ACTIVE=v2`; deploy. New hashes use `v2`; lookups of anything stored try every version, so an old `Idempotency-Key` replay and an old tombstone still match.
3. **Never remove a version while anything names it**: `kg secrets status` lists the journal's key versions; `kg deletions journal` fails loudly if the ring lacks a version the journal uses. Keep old versions in the secret manager for as long as backups and the journal exist.

## After any rotation
`kg secrets status` once per ring; `kg secrets record` per step (an audit of what was done, never the secret); the `secret_ring_versions` gauge returns to 1 per ring when nothing is mid-rotation.
