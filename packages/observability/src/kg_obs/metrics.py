"""Prometheus metrics. NO label may carry a workspace, principal or resource identifier (a test audits the registry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

http_requests_total = Counter("http_requests_total", "HTTP requests", ["route", "method", "status"], registry=REGISTRY)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP latency", ["route", "method"], buckets=_BUCKETS, registry=REGISTRY
)
auth_failures_total = Counter("auth_failures_total", "Authentication failures by reason", ["reason"], registry=REGISTRY)
authz_denials_total = Counter("authz_denials_total", "Authorization denials by action", ["action"], registry=REGISTRY)
router_calls_total = Counter("router_calls_total", "Model router calls", ["purpose", "alias", "operation", "outcome"], registry=REGISTRY)
router_tokens_total = Counter("router_tokens_total", "Model router tokens", ["purpose", "direction"], registry=REGISTRY)
worker_heartbeat_unixtime = Gauge("worker_heartbeat_unixtime", "Last worker heartbeat (unix seconds)", registry=REGISTRY)
ontology_publish_total = Counter("ontology_publish_total", "Ontology publish attempts", ["outcome"], registry=REGISTRY)
commands_total = Counter("commands_total", "Deterministic commands", ["command_type", "outcome"], registry=REGISTRY)
rule_violations_total = Counter("rule_violations_total", "Rule violations by kind and action", ["rule_kind", "action"], registry=REGISTRY)
projection_total = Counter("projection_total", "Graph projection attempts", ["kind", "outcome"], registry=REGISTRY)
projection_lag_seconds = Histogram(
    "projection_lag_seconds",
    "Seconds from a row being recorded to its projection",
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 3600),
    registry=REGISTRY,
)
# ---- outbox and interaction ledger (P3). No label carries an identifier; stage / status / outcome / kind are closed enums. ----------------------
interactions_accepted_total = Counter("interactions_accepted_total", "Interactions accepted into the ledger", ["kind"], registry=REGISTRY)
interactions_status_total = Counter("interactions_status_total", "Interactions reaching a processing status", ["status"], registry=REGISTRY)
outbox_jobs_total = Counter("outbox_jobs_total", "Outbox job completions by outcome", ["stage", "outcome"], registry=REGISTRY)
outbox_job_duration_seconds = Histogram(
    "outbox_job_duration_seconds",
    "Handler wall time per attempt",
    ["stage"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
    registry=REGISTRY,
)
outbox_backlog = Gauge("outbox_backlog", "Outbox jobs by stage and status (all workspaces)", ["stage", "status"], registry=REGISTRY)
outbox_oldest_pending_age_seconds = Gauge(
    "outbox_oldest_pending_age_seconds", "Age of the oldest claimable pending job", ["stage"], registry=REGISTRY
)
outbox_lease_reclaims_total = Counter(
    "outbox_lease_reclaims_total", "Jobs claimed after their previous lease expired", ["stage"], registry=REGISTRY
)
outbox_stale_completions_total = Counter(
    "outbox_stale_completions_total", "Completions rejected because the lease was lost (fencing)", ["stage"], registry=REGISTRY
)
outbox_dead_letters = Gauge("outbox_dead_letters", "Dead-lettered jobs by stage (all workspaces)", ["stage"], registry=REGISTRY)
# ---- extraction, identity and review (P4). Closed label values only (an outcome, a status, a kind, a type): never an id, a name or a fact. ----------
graph_ingest_total = Counter("graph_ingest_total", "graph_ingest stage attempts by outcome", ["outcome"], registry=REGISTRY)
graph_ingest_duration_seconds = Histogram(
    "graph_ingest_duration_seconds",
    "Wall time of one graph_ingest attempt (model calls included)",
    buckets=(0.05, 0.25, 1, 2.5, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
extraction_entities_total = Counter(
    "extraction_entities_total", "Entities Graphiti returned, by how the platform resolved them", ["resolution"], registry=REGISTRY
)
extraction_facts_total = Counter(
    "extraction_facts_total", "Extracted facts by the status the platform decided", ["status"], registry=REGISTRY
)
extraction_reverts_total = Counter(
    "extraction_reverts_total", "Graphiti changes to authoritative graph state that the platform undid", ["kind"], registry=REGISTRY
)
reviews_created_total = Counter("reviews_created_total", "Review items opened", ["item_type"], registry=REGISTRY)
review_decisions_total = Counter("review_decisions_total", "Review decisions", ["item_type", "decision"], registry=REGISTRY)
review_items_open = Gauge("review_items_open", "Open review items by type (all workspaces)", ["type"], registry=REGISTRY)
review_age_seconds = Gauge("review_age_seconds", "Age of the oldest open review item by type", ["type"], registry=REGISTRY)
graph_lock_wait_seconds = Histogram(
    "graph_lock_wait_seconds",
    "Time spent waiting for a workspace's graph writer lock",
    buckets=(0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 15, 60),
    registry=REGISTRY,
)
graph_search_total = Counter("graph_search_total", "Diagnostic graph searches by outcome", ["outcome"], registry=REGISTRY)
graph_search_unreconciled_hits_total = Counter(
    "graph_search_unreconciled_hits_total", "Graph hits dropped because Postgres has no matching fact yet", registry=REGISTRY
)
# ---- curated memory (P5). Closed label values only (a type, a status, an origin, an outcome): never an id, a scope, a query or any content. ----------
memories_total = Gauge("memories_total", "Curated memories by type and status (all workspaces)", ["type", "status"], registry=REGISTRY)
memory_writes_total = Counter(
    "memory_writes_total", "Memory write attempts by origin and outcome", ["origin", "outcome"], registry=REGISTRY
)
memory_extraction_candidates_total = Counter(
    "memory_extraction_candidates_total",
    "Candidates the model proposed, by what the platform did with them (kept, or the drop reason)",
    ["outcome"],
    registry=REGISTRY,
)
memory_search_total = Counter(
    "memory_search_total", "Memory searches by whether the vector half was available", ["degraded"], registry=REGISTRY
)
memory_search_duration_seconds = Histogram(
    "memory_search_duration_seconds",
    "Wall time of one memory search (the query embedding included)",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)
memory_search_candidates = Histogram(
    "memory_search_candidates",
    "Authorised candidates considered per search (a count of what the caller may see, never of what it may not)",
    buckets=(0, 1, 5, 10, 50, 100, 500, 1000, 5000, 20000),
    registry=REGISTRY,
)
memory_embed_total = Counter("memory_embed_total", "Memory embedding attempts by outcome", ["outcome"], registry=REGISTRY)
memory_embedding_coverage = Gauge(
    "memory_embedding_coverage", "Share of active memories that have a vector (all workspaces)", registry=REGISTRY
)
memory_expired_total = Counter("memory_expired_total", "Memories expired by the delayed expiry job", registry=REGISTRY)
dependency_up = Gauge("dependency_up", "1 if the dependency answered its last check", ["dependency"], registry=REGISTRY)


def render() -> bytes:
    return generate_latest(REGISTRY)


# ---- context engine (P6). Closed label values only (a profile name, a status, a source, a reason): never an id, a task or any content. ----------------
context_builds_total = Counter(
    "context_builds_total", "Context builds by profile, status and compression", ["profile", "status", "compression"], registry=REGISTRY
)
context_build_duration_seconds = Histogram(
    "context_build_duration_seconds",
    "Wall time of one context build, every source included",
    ["profile"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
    registry=REGISTRY,
)
context_source_duration_seconds = Histogram(
    "context_source_duration_seconds",
    "Wall time of one context source",
    ["source"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 3),
    registry=REGISTRY,
)
context_source_failures_total = Counter(
    "context_source_failures_total", "Context sources that failed or timed out", ["source", "reason"], registry=REGISTRY
)
context_tokens_used = Histogram(
    "context_tokens_used",
    "Tokens in a built package (the platform's own conservative count)",
    ["profile"],
    buckets=(64, 128, 256, 512, 768, 1024, 1536, 2048, 4096),
    registry=REGISTRY,
)
context_budget_violations_total = Counter(
    "context_budget_violations_total", "Packages whose re-counted text exceeded the budget (must stay 0; alert)", registry=REGISTRY
)
context_items_selected = Counter("context_items_selected", "Items packed into contexts by kind", ["kind"], registry=REGISTRY)
context_compression_fallbacks_total = Counter(
    "context_compression_fallbacks_total", "Summarised builds that fell back to structured, by reason", ["reason"], registry=REGISTRY
)
context_rate_limited_total = Counter(
    "context_rate_limited_total", "Context builds refused by the per-principal rate limit", registry=REGISTRY
)
context_trace_write_failures_total = Counter(
    "context_trace_write_failures_total", "Context traces that could not be written (the build still answered)", registry=REGISTRY
)

# ---- agents, grants, ACLs (P7). Closed label values only (a kind, a status, a reason, a surface, a quota). `mcp_tool_calls_total{tool,outcome}` lives in the MCP server's own registry (/metrics on :8010): never a principal, a handle or an id. ------
principals_total = Gauge("principals_total", "Registered principals by kind and status", ["kind", "status"], registry=REGISTRY)
grants_total = Gauge("grants_total", "Grants held, by scope kind (aggregate across workspaces)", ["scope_kind"], registry=REGISTRY)
revocations_total = Counter("revocations_total", "Principals revoked, by reason", ["reason"], registry=REGISTRY)
quota_exceeded_total = Counter("quota_exceeded_total", "Requests refused by a per-principal quota", ["quota"], registry=REGISTRY)
acl_hidden_total = Counter(
    "acl_hidden_total",
    "How often the subject filter hid an entity, a fact or a hit from a principal (a count; never the ids)",
    ["surface"],
    registry=REGISTRY,
)

# ---- governance (P8). Closed label values only (a kind, a store, an action, a mode, an outcome): never an id, a name or any residual text. ----------------------
corrections_total = Counter("corrections_total", "Corrections applied, by kind", ["kind"], registry=REGISTRY)
consolidation_runs_total = Counter(
    "consolidation_runs_total", "Consolidation runs by mode and outcome", ["mode", "outcome"], registry=REGISTRY
)
consolidation_drops_total = Counter(
    "consolidation_drops_total", "Synthesised statements or groups dropped, by reason", ["reason"], registry=REGISTRY
)
deletion_requests_total = Counter(
    "deletion_requests_total", "Deletion requests by kind and outcome", ["kind", "outcome"], registry=REGISTRY
)
deletion_items_total = Counter(
    "deletion_items_total", "Deletion plan items acted on, by store and action", ["store", "action"], registry=REGISTRY
)
deletion_freeze_seconds = Histogram(
    "deletion_freeze_seconds",
    "Wall time of the synchronous freeze of a deletion request",
    buckets=(0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 15),
    registry=REGISTRY,
)
deletion_completion_seconds = Histogram(
    "deletion_completion_seconds",
    "Seconds from a deletion request to its receipt",
    buckets=(1, 5, 15, 60, 300, 900, 3600, 21600),
    registry=REGISTRY,
)
deletion_residuals_total = Counter(
    "deletion_residuals_total", "Residuals the verifier found after a purge (alert when above 0), by store", ["store"], registry=REGISTRY
)
deletion_requests_open = Gauge(
    "deletion_requests_open", "Deletion requests by kind and status (all workspaces)", ["kind", "status"], registry=REGISTRY
)
retention_deleted_total = Counter(
    "retention_deleted_total", "Rows or contents removed by a retention job, by class", ["retention_class"], registry=REGISTRY
)
simulations_total = Counter("simulations_total", "Ontology simulations by outcome", ["outcome"], registry=REGISTRY)

# ---- release hardening (P9). Closed label values only (a backend, an outcome, a kind, a class, a store): never an id. ----------------------------------------------------
limiter_decisions_total = Counter(
    "limiter_decisions_total", "Rate-limit decisions by limiter, backend and outcome", ["limiter", "backend", "outcome"], registry=REGISTRY
)
limiter_errors_total = Counter(
    "limiter_errors_total",
    "Shared limiter failures by what the platform did (failed_open / failed_closed)",
    ["limiter", "mode"],
    registry=REGISTRY,
)
cache_invalidations_total = Counter(
    "cache_invalidations_total", "Cache invalidation notifications received, by kind", ["kind"], registry=REGISTRY
)
invalidation_listener_up = Gauge(
    "invalidation_listener_up", "1 while this process holds its LISTEN connection for cache invalidation", registry=REGISTRY
)
oidc_logins_total = Counter("oidc_logins_total", "OIDC token verifications by outcome", ["outcome"], registry=REGISTRY)
breakglass_uses_total = Counter(
    "breakglass_uses_total", "API-key authentications in production while break-glass keys are allowed", registry=REGISTRY
)
reconcile_drift = Gauge(
    "reconcile_drift", "Drift found by the latest completed reconcile scan, by class (all workspaces)", ["drift_class"], registry=REGISTRY
)
reconcile_repairs_total = Counter("reconcile_repairs_total", "Safe reconcile repairs applied, by class", ["drift_class"], registry=REGISTRY)
reindex_runs = Gauge("reindex_runs", "Reindex runs by status (all workspaces)", ["status"], registry=REGISTRY)
backup_age_seconds = Gauge("backup_age_seconds", "Age of the newest completed backup per store (-1: never)", ["store"], registry=REGISTRY)
canary_builds_total = Counter("canary_builds_total", "Synthetic canary context builds by outcome", ["outcome"], registry=REGISTRY)
canary_last_success_unixtime = Gauge("canary_last_success_unixtime", "Unix time of the last successful canary build", registry=REGISTRY)
maintenance_refusals_total = Counter("maintenance_refusals_total", "Writes refused while MAINTENANCE_MODE=read_only", registry=REGISTRY)
key_rehashes_total = Counter(
    "key_rehashes_total",
    "API keys re-hashed under the current pepper after authenticating under an older one (the pepper rotation drain)",
    registry=REGISTRY,
)
secret_ring_versions = Gauge("secret_ring_versions", "Versions held in a secret ring (1 = nothing rotating)", ["ring"], registry=REGISTRY)
