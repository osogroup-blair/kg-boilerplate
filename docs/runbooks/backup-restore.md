# Runbook: backup, restore and key recovery (AT-18)

## What is backed up, and how
`make backup` (production: `kg-backup.timer`, nightly 02:30, see `deploy/systemd/`) writes one **encrypted set** to `BACKUP_DIR`:
| Store | How | Note |
|---|---|---|
| Postgres | `pg_dump -Fc` streamed through `age` | online |
| Neo4j | offline `neo4j-admin dump` (the container is stopped for ~20 s) | Community Edition has no online backup; reads degrade, writes queue |
| Configuration | non-secret settings and the compat locks | |
| Keys | the payload keyring and the HMAC ring, `age`-encrypted to the **escrow** recipients | a separate location (`BACKUP_ESCROW_DIR`), different recipients than the data |

Everything is `age`-encrypted to **public** recipients (`BACKUP_RECIPIENTS`); the machine that makes backups cannot read them. The manifest carries per-file SHA-256, row counts, the migration head, the structure names (triggers, indexes, policies) and a context-probe so a restore can be checked. `kg-backup.service` verifies the set and prunes by `BACKUP_RETENTION_DAYS` (always keeping the newest three).

**The deletion journal is deliberately NOT in the backup** (`DELETION_JOURNAL_DIR`, a separate append-only volume): it is what makes a restore forget again.

## Keys: who holds what
- *Recovery identity* (`data.age`): decrypts the backups. Held offline by at least two people; **never** on the backup host.
- *Escrow identity* (`escrow.age`): decrypts the key escrow only. Held by a different person.
- Without both the data identity AND the payload keyring (from escrow), a backup cannot be read: payloads are AES-GCM under keys that exist only in the keyring.
- Test annually: a person other than the operator decrypts the newest escrow with the escrow identity and checks the payload kids against `SELECT kid, count(*) FROM interaction_contents GROUP BY kid`.

## Weekly: the restore rehearsal
`make restore-rehearsal` (production: `kg-rehearsal.timer` on a recovery host) restores the newest set into the separate `kg-restore` compose project, brings the API/worker up with `MAINTENANCE_MODE=read_only` (every write answers 503), then:
1. `kg deletions reapply --since <backup time>` (verifies the journal chain, removes what was deleted after the backup);
2. `kg reconcile --scope all --repair` for the safe classes; newer interactions are listed for re-ingest with a cost preview and **not** executed;
3. the checks: row counts vs the manifest, residual scan for every journal entry (**0 residuals**), the context probe matches its pre-backup answer, pooled-reuse/RLS, every trigger, ADR-required index and Graphiti constraint present;
4. writes `eval/reports/restore-<date>.md` and a `backup_runs` row with **RTO** (wall clock) and **RPO** (age of the backup). The console's Operations page shows both.

A release needs a passing rehearsal from the release candidate's own backups.

## A real restore
1. Stop writers: `MAINTENANCE_MODE=read_only` on API replicas, scale the worker to 0.
2. Restore Postgres (`pg_restore` into an empty `kg` database, as the superuser) and Neo4j (`neo4j-admin database load` with the container stopped), from one set (the manifest ties them together). `python -m tools.backup rehearse --keep` performs exactly this in the `kg-restore` project and is the dress rehearsal.
3. Provide the keyring (escrow) and the **same** HMAC ring versions the journal names.
4. `kg db upgrade` if the backup is older than the code.
5. `kg deletions reapply --since <backup time> --dry-run`, then without `--dry-run`. Exit 0 = applied; 1 = a line needs a person (read its output); **2 = the journal does not verify: stop.**
6. `kg reconcile --workspace <each> --scope all --repair`; review what is left (events newer than the backup are listed for re-ingest: `--allow-reingest` prints the cost first).
7. Re-enable writes (`MAINTENANCE_MODE=off`), start the worker. Watch `/v1/operations/alerts`.

**Known limit (accepted, F-08):** a text-residual *exception* a person recorded after the backup (a legal-hold decision) is not re-raised by reapply because the journal carries ids and HMACs, never names. Re-enter such exceptions from the review queue.
