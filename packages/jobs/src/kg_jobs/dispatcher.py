"""The worker-side outbox loop (ADR-025).

    claim (leased, fenced) -> run the handler in a slot -> map the result with the retry policy -> complete (fenced)

Delivery is AT LEAST ONCE: a crash anywhere between claim and complete lets the lease expire and the job is claimed again. Handlers are idempotent
(deterministic keys or state-based writes), so the observable result is EFFECTIVELY ONCE. Time is judged by the database: the dispatcher passes
durations (lease length, backoff), never timestamps.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kg_obs import context, metrics
from kg_obs.logging import get_logger

from kg_jobs import faults as fault_hooks
from kg_jobs.client import ClaimedJob, JobsBackend
from kg_jobs.errors import Category
from kg_jobs.policy import SUCCEEDED, Decision, RetryPolicy, decide, policy_for

log = get_logger("kg.jobs.dispatcher")


class JobHandler(Protocol):
    stage: str

    async def handle(self, job: ClaimedJob) -> None:
        """Do the work idempotently, write the result in ONE tenant transaction, then return. Raise JobError / Skip to fail the attempt."""
        ...


class FailureHooks(Protocol):
    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        """Optional. Called before the job is completed as retry / dead / skipped, so the domain record (a stage row) reflects the outcome."""
        ...


@dataclass
class _Running:
    job: ClaimedJob
    task: asyncio.Task[None] | None = None
    lost: bool = False
    started: float = field(default_factory=time.perf_counter)


class Dispatcher:
    def __init__(
        self,
        backend: JobsBackend,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str,
        concurrency: int = 4,
        stage_limits: Mapping[str, int] | None = None,
        lease_seconds: int = 30,
        poll_busy: float = 0.2,
        poll_idle: float = 2.0,
        shutdown_grace: float = 20.0,
        policies: Mapping[str, RetryPolicy] | None = None,
        stats_interval: float = 10.0,
    ):
        self._backend, self._handlers = backend, dict(handlers)
        self._worker_id, self._concurrency = worker_id, concurrency
        self._stage_limits = dict(stage_limits or {})
        self._lease, self._poll_busy, self._poll_idle = lease_seconds, poll_busy, poll_idle
        self._grace, self._stats_interval = shutdown_grace, stats_interval
        self._policies = dict(policies or {})
        self._running: dict[uuid.UUID, _Running] = {}
        self._wake = asyncio.Event()
        self._rotate = 0

    # ---- introspection (tests, worker status) -----------------------------------------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        return len(self._running)

    def _policy(self, stage: str) -> RetryPolicy:
        return self._policies.get(stage) or policy_for(stage)

    # ---- main loop --------------------------------------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(stop), name="outbox-heartbeat")
        stats = asyncio.create_task(self._stats_loop(stop), name="outbox-stats")
        delay = self._poll_busy
        try:
            while not stop.is_set():
                try:
                    claimed = await self._claim_round()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # database unavailable (a restart, a failover): keep looping, the pool reconnects
                    log.warning("outbox_claim_failed", error_type=type(exc).__name__)
                    claimed, delay = 0, max(delay, 1.0)
                delay = self._poll_busy if claimed else min(max(delay, self._poll_busy) * 1.5, self._poll_idle)
                await self._wait(stop, delay)
        finally:
            await self._drain()
            for t in (heartbeat, stats):
                t.cancel()
            await asyncio.gather(heartbeat, stats, return_exceptions=True)

    async def _wait(self, stop: asyncio.Event, delay: float) -> None:
        """Sleeps up to `delay`, waking early when a slot frees up or the worker is stopping."""
        waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            self._wake.clear()

    async def _claim_round(self) -> int:
        free = self._concurrency - len(self._running)
        if free <= 0 or not self._handlers:
            return 0
        stages = list(self._handlers)
        self._rotate = (self._rotate + 1) % len(stages)
        stages = stages[self._rotate :] + stages[: self._rotate]  # rotate so one stage cannot always take the first slots
        per_stage = Counter(r.job.stage for r in self._running.values())
        claimed = 0
        for stage in stages:
            room = min(free, self._stage_limits.get(stage, self._concurrency) - per_stage[stage])
            if room <= 0:
                continue
            for job in await self._backend.claim(self._worker_id, [stage], room, self._lease):
                entry = _Running(job)
                entry.task = asyncio.create_task(self._run_job(entry), name=f"job-{job.stage}-{job.job_id}")
                self._running[job.job_id] = entry
                free -= 1
                claimed += 1
                if job.reclaimed:
                    metrics.outbox_lease_reclaims_total.labels(stage=job.stage).inc()
            if free <= 0:
                break
        return claimed

    # ---- one job ----------------------------------------------------------------------------------------------------------------------------

    async def _run_job(self, entry: _Running) -> None:
        job = entry.job
        context.correlation_id_var.set(job.correlation_id)
        policy = self._policy(job.stage)
        faults = fault_hooks.current()
        handler = self._handlers[job.stage]
        decision: Decision
        started = time.perf_counter()
        try:
            try:
                faults.hit(job.stage, "after_claim")
                faults.hit(job.stage, "stall")
                if job.attempts > job.max_attempts:  # claimed again after crashing max_attempts times: do not run it a further time
                    decision = Decision("dead", Category.RETRYABLE, "attempts_exhausted")
                else:
                    faults.hit(job.stage, "raise_retryable")
                    faults.hit(job.stage, "raise_config")
                    await handler.handle(job)
                    decision = SUCCEEDED
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decision = decide(exc, attempt=job.attempts, policy=policy)
                if decision.code == "unexpected_error":
                    log.warning("job_unexpected_error", job_id=str(job.job_id), stage=job.stage, error_type=type(exc).__name__)
            metrics.outbox_job_duration_seconds.labels(stage=job.stage).observe(time.perf_counter() - started)
            if decision.outcome != "succeeded" and not await self._record_failure(handler, job, decision):
                return  # leave the lease to expire: the job runs again and the hook is retried
            faults.hit(job.stage, "before_complete")
            result = await self._backend.complete(
                job.job_id,
                job.lease_token,
                decision.outcome,
                error_category=decision.category.value if decision.category else None,
                error_code=decision.code,
                delay_seconds=decision.delay_seconds,
            )
            outcome = "stale" if result == "stale" else decision.outcome
            if result == "stale":
                metrics.outbox_stale_completions_total.labels(stage=job.stage).inc()
            metrics.outbox_jobs_total.labels(stage=job.stage, outcome=outcome).inc()
            log.info(
                "job_done",
                job_id=str(job.job_id),
                stage=job.stage,
                attempt=job.attempts,
                outcome=outcome,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                pipeline_version=job.pipeline_version,
                ontology_version_id=str(job.ontology_version_id) if job.ontology_version_id else None,
                correlation_id=job.correlation_id,
                error_code=decision.code,
            )
        except asyncio.CancelledError:
            if entry.lost:
                metrics.outbox_jobs_total.labels(stage=job.stage, outcome="lease_lost").inc()
                log.warning("job_lease_lost", job_id=str(job.job_id), stage=job.stage, attempt=job.attempts)
                return  # someone else owns it now: no completion, no error
            raise  # shutdown after the grace period: release_jobs hands the lease back
        except Exception as exc:  # e.g. the database went away between the handler and the ack: the lease expires and the job is re-run
            log.warning("job_completion_failed", job_id=str(job.job_id), stage=job.stage, error_type=type(exc).__name__)
        finally:
            self._running.pop(job.job_id, None)
            self._wake.set()

    async def _record_failure(self, handler: JobHandler, job: ClaimedJob, decision: Decision) -> bool:
        hook: Any = getattr(handler, "on_failure", None)
        if hook is None:
            return True
        try:
            await hook(job, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("job_failure_hook_failed", job_id=str(job.job_id), stage=job.stage, error_type=type(exc).__name__)
            return decision.outcome == "retry"  # the retry record is informational; a dead/skipped record must be written before the ack
        return True

    # ---- leases -----------------------------------------------------------------------------------------------------------------------------

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        interval = max(self._lease / 3, 0.5)
        while not stop.is_set() or self._running:
            await asyncio.sleep(interval)
            live = {r.job.lease_token: r for r in self._running.values() if not r.lost}
            if not live:
                continue
            try:
                lost = await self._backend.heartbeat(self._worker_id, list(live), self._lease)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the lease simply runs down if the database is away; the fenced ack protects the result
                log.warning("outbox_heartbeat_failed", error_type=type(exc).__name__)
                continue
            for token in lost:
                entry = live.get(token)
                if entry is not None and entry.task is not None:
                    entry.lost = True
                    entry.task.cancel()  # cooperative: the handler is cancelled at its next await

    async def _stats_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                rows = await self._backend.backlog_stats()
                seen: set[tuple[str, str]] = set()
                for r in rows:
                    metrics.outbox_backlog.labels(stage=r.stage, status=r.status).set(r.jobs)
                    seen.add((r.stage, r.status))
                    if r.status == "pending" and r.oldest_age_seconds is not None:
                        metrics.outbox_oldest_pending_age_seconds.labels(stage=r.stage).set(r.oldest_age_seconds)
                    if r.status == "dead":
                        metrics.outbox_dead_letters.labels(stage=r.stage).set(r.jobs)
                for stage in {s for s, _ in seen}:  # a stage that drained to nothing must read 0, not its last value
                    for status in ("pending", "leased", "dead"):
                        if (stage, status) not in seen:
                            metrics.outbox_backlog.labels(stage=stage, status=status).set(0)
                    if (stage, "dead") not in seen:
                        metrics.outbox_dead_letters.labels(stage=stage).set(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("outbox_stats_failed", error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._stats_interval)
            except TimeoutError:
                pass

    # ---- shutdown ---------------------------------------------------------------------------------------------------------------------------

    async def _drain(self) -> None:
        """Graceful stop: no new claims, wait up to the grace period for running jobs, cancel the rest and hand their leases back (a shutdown is not a failure)."""
        tasks = [r.task for r in self._running.values() if r.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._grace)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        try:
            released = await self._backend.release(self._worker_id)
            if released:
                log.info("outbox_leases_released", count=released)
        except Exception as exc:
            log.warning("outbox_release_failed", error_type=type(exc).__name__)
