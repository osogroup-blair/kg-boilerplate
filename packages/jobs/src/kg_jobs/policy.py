"""Retry policy per stage and the category -> outcome mapping (phase-3 plan §5.5)."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from kg_jobs.errors import Category, JobError, Skip

Outcome = Literal["succeeded", "skipped", "retry", "dead", "budget_pause"]
MAX_RETRY_AFTER_SECONDS = 3600.0
MIN_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: float
    cap_seconds: float
    budget_pause_seconds: float = 900.0  # 15 minutes

    def delay(self, attempt: int, *, retry_after: float | None = None, rng: Callable[[], float] = random.random) -> float:
        """Seconds until the next attempt after `attempt` failed. A router Retry-After wins; otherwise full jitter in [0, min(cap, base * 2^(n-1))]."""
        if retry_after is not None:
            return float(min(max(retry_after, MIN_DELAY_SECONDS), MAX_RETRY_AFTER_SECONDS))
        ceiling = min(self.cap_seconds, self.base_seconds * (2 ** max(attempt - 1, 0)))
        return float(max(MIN_DELAY_SECONDS, rng() * ceiling))


POLICIES: dict[str, RetryPolicy] = {
    "normalize": RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=120.0),
    "graph_projection": RetryPolicy(max_attempts=8, base_seconds=2.0, cap_seconds=600.0),
    "graph_ingest": RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=300.0),
    # P5: the memory stage is a model call like graph_ingest (an unparseable answer is quarantined by the handler's own two-attempt rule); the embed job
    # retries a router outage for a good while, with a short cap so a recovered router is used soon; the expiry job only ever waits for a clock.
    "memory_propose": RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=300.0),
    "memory_embed": RetryPolicy(max_attempts=8, base_seconds=1.0, cap_seconds=30.0),
    "memory_expire": RetryPolicy(max_attempts=6, base_seconds=1.0, cap_seconds=30.0),
    # P8: consolidation is a model call that a person reviews anyway; a deletion step is idempotent and must get through, so it retries for a good while
    "memory_consolidate": RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=300.0),
    "ontology_simulation": RetryPolicy(max_attempts=3, base_seconds=5.0, cap_seconds=120.0),
    "simulation_cleanup": RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=120.0),
    "deletion_plan": RetryPolicy(max_attempts=8, base_seconds=2.0, cap_seconds=300.0),
    "deletion_purge": RetryPolicy(max_attempts=8, base_seconds=2.0, cap_seconds=300.0),
    "deletion_verify": RetryPolicy(max_attempts=8, base_seconds=2.0, cap_seconds=300.0),
}
DEFAULT_POLICY = RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=120.0)


def policy_for(stage: str) -> RetryPolicy:
    return POLICIES.get(stage, DEFAULT_POLICY)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    category: Category | None = None
    code: str | None = None
    delay_seconds: float = 0.0


SUCCEEDED = Decision("succeeded")


def decide(
    exc: BaseException | None,
    *,
    attempt: int,
    policy: RetryPolicy,
    rng: Callable[[], float] = random.random,
) -> Decision:
    """`attempt` is the 1-based number of the attempt that just ran. Anything that is not a typed JobError / Skip is retryable
    (`unexpected_error`) up to the attempt cap, and its message is never kept."""
    if exc is None:
        return SUCCEEDED
    if isinstance(exc, Skip):
        return Decision("skipped", Category.POLICY_TERMINAL, exc.code)
    if isinstance(exc, JobError):
        category, code, retry_after = exc.category, exc.code, exc.retry_after
    else:
        category, code, retry_after = Category.RETRYABLE, "unexpected_error", None
    if category is Category.POLICY_TERMINAL:
        return Decision("skipped", category, code)
    if category is Category.CONFIG_TERMINAL:
        return Decision("dead", category, code)
    if category is Category.BUDGET:
        return Decision("budget_pause", category, code, policy.budget_pause_seconds)
    if attempt >= policy.max_attempts:
        return Decision("dead", category, code)
    return Decision("retry", category, code, policy.delay(attempt, retry_after=retry_after, rng=rng))
