"""Encrypted backups and the restore rehearsal (phase-9 plan WP5, AT-18, V6, V7).

A driver that runs on the Docker host (or a CI runner): it calls `docker` / `docker compose` and the tool image (`pg_dump`, `neo4j-admin`, `age`); it imports no platform package,
so it works when the platform itself does not. A backup SET is a directory of age-encrypted artifacts plus a plain `manifest.json` (store, size, sha256, row counts: no content,
no secrets). Keys are exported separately to the escrow location and never into the set."""
