# ADR-048: Row-level security stays at the workspace; per-principal RLS and a content-reader role are not adopted

**Status:** Accepted  **Decision refs:** SEC-01, SEC-02, phase-9 plan WP6, DEC-06  **Evidence:** `tests/security/test_isolation.py`, `tests/security/test_p5_isolation.py`..`test_p7_isolation.py`, `tests/security/test_memory_leaks.py`, `tests/security/test_fact_acl_leaks.py`

## Context
The phase-9 review asked whether the database should enforce *principal* visibility too (session variable for the principal, policies on memories and facts), and whether content should be readable only through a separate database role.

## Decision
Keep **forced RLS on `workspace_id`** for every tenant table (the runtime role cannot bypass it) and enforce scope, subject and project visibility **in the query** (the authorisation is part of the ranking query, ADR-031) and in the deterministic ACL layer (ADR-037). Do not add per-principal policies or a content-reader role.

## Why
- Principal visibility is relational (grants, subject ACLs, project scopes, revocation state); a policy would restate it in SQL and run on every row of every query, on pooled connections where a stale session variable is the failure mode.
- The leak suites, route walkers and the authorization eval (911 cases, 941 probes, all strict counters 0) test the property directly; the workspace RLS is the backstop that contains any application bug to one tenant.
- A content-reader role would not stop an application bug that already holds the reader connection.

## Consequences
A defect in the ACL layer could expose another principal's content *within one workspace* (never across workspaces). Mitigation is the test surface above, the double check of graph results against Postgres ACLs, and metadata-only telemetry. Revisit together with DEC-06 if a tenant requires database-enforced separation of duties.
