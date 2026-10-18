# Runbook: rebuilding the graph for a changed ontology or after graph damage (Flow F)

Use when the ontology changed in a way that needs the graph rebuilt, when reconciliation shows drift that repair cannot fix, or after restoring Neo4j from an older dump. The rebuild happens in a **second namespace** while the live one keeps serving; cutover is one Neo4j transaction plus a Postgres pointer flip under the workspace's graph lock. The old namespace is kept for `REINDEX_RETENTION_DAYS` (14) for rollback.

```bash
kg reindex preview  --workspace acme                 # what would be written, what it would cost (nothing is written)
kg reindex start    --workspace acme --as admin@acme               # mode project: replay Postgres → graph, no model calls
kg reindex start    --workspace acme --as admin@acme --mode reextract   # also re-run extraction on episodes after cutover (spends model calls; the preview prints the estimate)
kg reindex show     --workspace acme                 # status, progress, the verification report
kg reindex activate --workspace acme --as admin@acme # only when status is ready and the report is clean
kg reindex rollback --workspace acme --as admin@acme # re-activate the retired namespace (inside the retention window)
kg reindex cancel   --workspace acme --as admin@acme # before activation: clears the new namespace
```
Statuses: `preview → building → catching_up → ready → activated` (or `failed`, `cancelled`, `rolled_back`). While building, writes to the workspace's graph continue and are synced into the new namespace (`catching_up`); `ready` means the verification (counts by label, a sample of edges and the context probe) matched.

**Do not** start a reindex during a deletion request for the same workspace (the deletion clears every namespace, including the inactive one; start it after). A reindex never touches Postgres facts or memories: they are the source.

If `activate` is refused, read `kg reindex show`: a verification mismatch means stop and escalate; `graph_lock_busy` means retry in a minute. `FI-21..23` in `tests/resilience` are the proofs that a crash at any point leaves exactly one live namespace.
