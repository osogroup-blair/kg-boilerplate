"""kg_jobs: the generic transactional outbox (ADR-025). No domain knowledge: stages, subjects and payloads are opaque IDs.

enqueue()      in the CALLER's transaction (the outbox pattern)
OpsClient      the ops.* SECURITY DEFINER wrappers (claim / heartbeat / complete / release / backlog_stats)
Dispatcher     the worker-side loop: leased, fenced claims, per-stage slots, heartbeats, retries by category, graceful shutdown
"""

__version__ = "0.1.0"
