# Runbook: payload encryption keys

Raw and normalised event content is stored AES-256-GCM encrypted. Each stored row records the key id (`kid`) it was encrypted under, so several keys can be live at once and a rotation never needs downtime.

## Configuration
| Setting | Meaning |
|---|---|
| `PAYLOAD_KEYS` | the keyring: `kid:base64(32 bytes),kid:base64(32 bytes)`. Every kid that still has rows must stay in it |
| `PAYLOAD_ACTIVE_KID` | the kid new writes use; it must be in `PAYLOAD_KEYS` or the API and worker refuse to start |
| `CONTENT_HMAC_SECRET` | HKDF master of the keyed content/request hash. **Do not rotate it casually:** a new value changes every request hash, so a replay of an old `Idempotency-Key` would answer `409 idempotency_mismatch` |

`ENV=production` refuses the development key (`dev1`), the development HMAC secret, a ring that lacks the active kid, and `KG_FAULTS`/`KG_FAULTS_DIR`.

Generate a key:
```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

## Back up the keys first
**A row whose key is lost is unreadable forever** (the ciphertext is bound to its row and to the key; there is no recovery path). Keep `PAYLOAD_KEYS` in your secret manager with versioned history, and test a restore before you rotate. A database backup without the keyring is useless for content.

## Rotate
1. Add the new key to `PAYLOAD_KEYS` **keeping the old one**, and deploy API and worker with both (new writes still use the old active kid). Check `GET /v1/operations/health` and the worker log.
2. Set `PAYLOAD_ACTIVE_KID` to the new kid and deploy again. New events are encrypted under it; old rows remain readable.
3. Re-encrypt the existing rows, one workspace at a time (resumable and idempotent; it only touches rows not yet under the target kid):
   ```bash
   uv run kg payloads reencrypt --workspace demo --to <new_kid> --actor admin@demo
   ```
   Each batch is a transaction with an audit entry (`payload.reencrypted`, the kid and the count; never content).
4. Verify nothing remains under the old kid (as the database owner):
   ```sql
   SELECT kid, count(*) FROM interaction_contents GROUP BY kid;
   ```
5. Only then remove the old key from `PAYLOAD_KEYS`, keeping it in the secret manager's history for as long as backups taken under it exist.

## What a failure looks like
- `payload_unreadable` (HTTP 500, `details.reason`): `unknown_key_id` means the kid is not in the ring; `integrity_check_failed` means the ciphertext does not belong to that row/key (a corrupted or spliced row). Neither contains content.
- The API or worker exits at start with "PAYLOAD_ACTIVE_KID ... is not in PAYLOAD_KEYS" or "PAYLOAD_KEYS ... base64" / "must be 32 bytes": fix the ring.
