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
projection_pending = Gauge("projection_pending", "Rows waiting for graph projection (set by the sweeper)", registry=REGISTRY)
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
dependency_up = Gauge("dependency_up", "1 if the dependency answered its last check", ["dependency"], registry=REGISTRY)


def render() -> bytes:
    return generate_latest(REGISTRY)
