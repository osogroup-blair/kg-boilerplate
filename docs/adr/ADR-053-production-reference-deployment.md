# ADR-053: Production reference deployment is one VM with Docker Compose

**Status:** Accepted  **Decision refs:** phase-9 plan WP10, F-04  **Evidence:** `deploy/compose/docker-compose.prod.yml`, `scripts/prod_smoke.py` (`make prod-smoke`), `docs/runbooks/deploy-production.md`

## Decision
Caddy terminates TLS and serves the console and proxies the API (two replicas, found by DNS, least-connections, health-checked) and the MCP server; Postgres and Neo4j are on an `internal: true` network; only the API and worker can also reach the `egress` network (for the model router); every container is read-only (the exception is Neo4j: its entrypoint writes a generated config, so it runs as its own non-root user with a writable root), `no-new-privileges`, `cap_drop: ALL`, non-root; every credential is a Docker secret read through `NAME_FILE`; the deletion journal and the backups live outside the database. Backups run from the host (`systemd` timers) because the Neo4j Community dump is offline.

## Consequences
One VM is a single point of failure for availability; recoverability comes from encrypted off-host backups and a measured RTO/RPO, not from redundancy. Kubernetes is not provided; the compose file documents every dependency a manifest would need.
