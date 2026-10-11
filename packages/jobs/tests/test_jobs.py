"""Unit tests for kg_jobs: retry policy, category mapping, payload validation, fault injection, and the dispatcher against an in-memory backend."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from kg_jobs import faults
from kg_jobs.client import BacklogRow, ClaimedJob
from kg_jobs.dispatcher import Dispatcher
from kg_jobs.errors import Category, JobError, Skip
from kg_jobs.outbox import validate_payload
from kg_jobs.policy import MAX_RETRY_AFTER_SECONDS, RetryPolicy, decide, policy_for

pytestmark = pytest.mark.unit
POLICY = RetryPolicy(max_attempts=5, base_seconds=2.0, cap_seconds=120.0)


# ---- policy -----------------------------------------------------------------------------------------------------------------------------------


def test_backoff_is_full_jitter_within_exponential_bounds_and_capped():
    for attempt, ceiling in [(1, 2.0), (2, 4.0), (3, 8.0), (6, 64.0), (7, 120.0), (20, 120.0)]:
        assert POLICY.delay(attempt, rng=lambda: 1.0) == pytest.approx(ceiling)
        assert POLICY.delay(attempt, rng=lambda: 0.0) == pytest.approx(0.1)  # never a hot loop
        assert 0.1 <= POLICY.delay(attempt) <= ceiling


def test_retry_after_takes_precedence_and_is_bounded():
    assert POLICY.delay(1, retry_after=45.0, rng=lambda: 0.0) == 45.0
    assert POLICY.delay(1, retry_after=10**9) == MAX_RETRY_AFTER_SECONDS
    assert POLICY.delay(1, retry_after=-5) == 0.1


@pytest.mark.parametrize(
    ("category", "outcome"),
    [
        (Category.RETRYABLE, "retry"),
        (Category.POLICY_TERMINAL, "skipped"),
        (Category.CONFIG_TERMINAL, "dead"),
        (Category.BUDGET, "budget_pause"),
    ],
)
def test_category_to_outcome_mapping(category: Category, outcome: str):
    d = decide(JobError(category, "some_code"), attempt=1, policy=POLICY)
    assert d.outcome == outcome and d.code == "some_code" and d.category is category


def test_retryable_becomes_dead_when_attempts_are_exhausted_keeping_the_code():
    d = decide(JobError(Category.RETRYABLE, "graph_unavailable"), attempt=5, policy=POLICY)
    assert (d.outcome, d.code) == ("dead", "graph_unavailable")


def test_unexpected_exception_is_retryable_and_its_message_is_dropped():
    d = decide(RuntimeError("SENTINEL-CONTENT in message"), attempt=1, policy=POLICY)
    assert d.outcome == "retry" and d.code == "unexpected_error" and "SENTINEL" not in repr(d)
    assert decide(RuntimeError("x"), attempt=5, policy=POLICY).outcome == "dead"


def test_skip_is_policy_terminal_and_budget_pause_has_the_long_backoff():
    assert decide(Skip("no_consent"), attempt=1, policy=POLICY).outcome == "skipped"
    d = decide(JobError(Category.BUDGET, "budget_exceeded"), attempt=99, policy=POLICY)  # never dead: waiting for budget is not a failure
    assert d.outcome == "budget_pause" and d.delay_seconds == 900.0


def test_stage_policies_match_the_plan():
    assert (policy_for("normalize").max_attempts, policy_for("normalize").cap_seconds) == (5, 120.0)
    assert (policy_for("graph_projection").max_attempts, policy_for("graph_projection").cap_seconds) == (8, 600.0)


def test_error_codes_are_tokens_not_sentences():
    with pytest.raises(ValueError):
        JobError(Category.RETRYABLE, "Some free text with spaces")


def test_payloads_carry_ids_not_content():
    ok = validate_payload({"interaction_id": uuid.uuid4(), "generation": 0, "flag": True})
    assert isinstance(ok["interaction_id"], str)
    for bad in ({"text": "hello world this is content"}, {"nested": {"a": 1}}, {"Bad-Key": 1}, {"k": "x" * 65}):
        with pytest.raises(ValueError):
            validate_payload(bad)


# ---- fault injection ---------------------------------------------------------------------------------------------------------------------------


def test_fault_spec_parsing_rejects_unknown_points():
    assert [(r.stage, r.point, r.count) for r in faults.parse_spec("normalize:after_claim:3, graph_projection:raise_config:1")] == [
        ("normalize", "after_claim", 3),
        ("graph_projection", "raise_config", 1),
    ]
    with pytest.raises(ValueError):
        faults.parse_spec("normalize:explode:1")


def test_stall_rules_carry_a_duration_and_block_the_process(tmp_path, monkeypatch):
    (rule,) = faults.parse_spec("normalize:stall:2:7.5")
    assert (rule.point, rule.count, rule.seconds) == ("stall", 2, 7.5)
    assert faults.parse_spec("normalize:stall:1")[0].seconds == faults.DEFAULT_STALL_SECONDS
    with pytest.raises(ValueError):
        faults.parse_spec("normalize:after_claim:1:5")  # only stall takes a duration
    slept: list[float] = []
    monkeypatch.setattr(faults.time, "sleep", slept.append)
    f = faults.Faults("normalize:stall:1:7.5", str(tmp_path))
    f.hit("normalize", "stall")
    f.hit("normalize", "stall")
    assert slept == [7.5]


def test_raise_faults_fire_count_times_then_stop(tmp_path):
    f = faults.Faults("normalize:raise_config:2", str(tmp_path))
    for _ in range(2):
        with pytest.raises(JobError) as exc:
            f.hit("normalize", "raise_config")
        assert exc.value.category is Category.CONFIG_TERMINAL
    f.hit("normalize", "raise_config")  # exhausted
    f.hit("other_stage", "raise_config")  # a different stage is untouched
    # the counter survives a "restart": a new instance over the same directory sees it
    faults.Faults("normalize:raise_config:2", str(tmp_path)).hit("normalize", "raise_config")


def test_spec_file_arms_a_worker_without_a_restart(tmp_path):
    f = faults.Faults("", str(tmp_path))
    f.hit("normalize", "raise_retryable")  # nothing armed
    (tmp_path / "spec").write_text("normalize:raise_retryable:1\n")
    with pytest.raises(JobError):
        f.hit("normalize", "raise_retryable")
    f.hit("normalize", "raise_retryable")


def test_crash_points_exit_hard(tmp_path, monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(faults.os, "_exit", lambda code: exits.append(code))
    faults.Faults("normalize:after_claim:1", str(tmp_path)).hit("normalize", "after_claim")
    assert exits == [faults.EXIT_CODE]


# ---- dispatcher against an in-memory backend ---------------------------------------------------------------------------------------------------


def mk(stage: str = "s", *, attempts: int = 1, max_attempts: int = 5, generation: int = 0, **payload: Any) -> ClaimedJob:
    return ClaimedJob(
        job_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        stage=stage,
        subject_type="interaction",
        subject_id=uuid.uuid4(),
        payload={"generation": generation, **payload},
        attempts=attempts,
        max_attempts=max_attempts,
        lease_token=uuid.uuid4(),
        pipeline_version="p3.v1",
        ontology_version_id=None,
        correlation_id="corr-1",
    )


class FakeBackend:
    def __init__(self, jobs: list[ClaimedJob]):
        self.queue = list(jobs)
        self.completed: list[tuple[uuid.UUID, str, dict[str, Any]]] = []
        self.lost: set[uuid.UUID] = set()
        self.released = 0
        self.max_claim = 0
        self.stale = False

    async def claim(self, worker_id: str, stages: list[str], limit: int, lease_seconds: int) -> list[ClaimedJob]:
        self.max_claim = max(self.max_claim, limit)
        out = [j for j in self.queue if j.stage in stages][:limit]
        for j in out:
            self.queue.remove(j)
        return out

    async def heartbeat(self, worker_id: str, tokens: list[uuid.UUID], lease_seconds: int) -> list[uuid.UUID]:
        return [t for t in tokens if t in self.lost]

    async def complete(self, job_id: uuid.UUID, token: uuid.UUID, outcome: str, **kw: Any) -> str:
        self.completed.append((job_id, outcome, kw))
        return "stale" if self.stale else "ok"

    async def release(self, worker_id: str) -> int:
        self.released += 1
        return 0

    async def backlog_stats(self) -> list[BacklogRow]:
        return []


class Handler:
    def __init__(self, stage: str, fn=None):  # type: ignore[no-untyped-def]
        self.stage, self.fn, self.seen, self.failures = stage, fn, [], []

    async def handle(self, job: ClaimedJob) -> None:
        self.seen.append(job)
        if self.fn:
            await self.fn(job)

    async def on_failure(self, job: ClaimedJob, decision) -> None:  # type: ignore[no-untyped-def]
        self.failures.append((job.job_id, decision.outcome, decision.code))


async def run_until(backend: FakeBackend, handlers: dict[str, Handler], done, *, timeout: float = 5.0, **kw: Any) -> Dispatcher:  # type: ignore[no-untyped-def]  # noqa: ASYNC109
    d = Dispatcher(
        backend,
        handlers,
        worker_id="w1",
        lease_seconds=kw.pop("lease_seconds", 3),
        poll_busy=0.01,
        poll_idle=0.02,
        shutdown_grace=2.0,
        **kw,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        async with asyncio.timeout(timeout):
            while not done():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task
    return d


async def test_success_completes_the_job():
    job = mk("s")
    backend, h = FakeBackend([job]), Handler("s")
    await run_until(backend, {"s": h}, lambda: backend.completed)
    assert backend.completed[0][:2] == (job.job_id, "succeeded") and h.failures == []
    assert backend.released == 1  # graceful shutdown always hands leases back


async def test_typed_errors_map_to_outcomes_and_call_the_failure_hook():
    async def boom(job: ClaimedJob) -> None:
        raise JobError(Category.RETRYABLE, "graph_unavailable")

    job = mk("s", attempts=1)
    backend, h = FakeBackend([job]), Handler("s", boom)
    await run_until(backend, {"s": h}, lambda: backend.completed)
    _, outcome, kw = backend.completed[0]
    assert outcome == "retry" and kw["error_code"] == "graph_unavailable" and 0.1 <= kw["delay_seconds"] <= 2.0
    assert h.failures == [(job.job_id, "retry", "graph_unavailable")]


async def test_an_exhausted_job_is_dead_lettered_without_running_the_handler_again():
    job = mk("s", attempts=6, max_attempts=5)  # claimed again after crashing its way through every attempt
    backend, h = FakeBackend([job]), Handler("s")
    await run_until(backend, {"s": h}, lambda: backend.completed)
    assert h.seen == [] and backend.completed[0][1] == "dead" and backend.completed[0][2]["error_code"] == "attempts_exhausted"
    assert h.failures[0][1:] == ("dead", "attempts_exhausted")


async def test_a_failing_dead_hook_leaves_the_lease_to_expire_instead_of_acking():
    class Hook(Handler):
        async def on_failure(self, job, decision):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

    async def poison(job: ClaimedJob) -> None:
        raise JobError(Category.CONFIG_TERMINAL, "bad_config")

    h = Hook("s", poison)
    backend = FakeBackend([mk("s")])
    await run_until(backend, {"s": h}, lambda: h.seen)
    await asyncio.sleep(0.2)
    assert backend.completed == []  # no ack: the job is claimed again after the lease expires and the hook gets another chance


async def test_stale_completions_are_counted_not_raised():
    backend = FakeBackend([mk("s")])
    backend.stale = True
    await run_until(backend, {"s": Handler("s")}, lambda: backend.completed)
    assert backend.completed[0][1] == "succeeded"  # the dispatcher asked; the database said stale; nothing else happens


async def test_per_stage_limits_and_total_concurrency_are_respected():
    running = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}

    def make(stage: str):  # type: ignore[no-untyped-def]
        async def work(job: ClaimedJob) -> None:
            running[stage] += 1
            peak[stage] = max(peak[stage], running[stage])
            await asyncio.sleep(0.05)
            running[stage] -= 1

        return Handler(stage, work)

    jobs = [mk("a") for _ in range(8)] + [mk("b") for _ in range(8)]
    backend = FakeBackend(jobs)
    await run_until(backend, {"a": make("a"), "b": make("b")}, lambda: len(backend.completed) == 16, concurrency=4, stage_limits={"a": 1})
    assert peak["a"] == 1 and peak["b"] <= 4 and peak["a"] + peak["b"] <= 5
    assert backend.max_claim <= 4


async def test_a_lost_lease_cancels_the_handler_and_never_completes():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(job: ClaimedJob) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    job = mk("s")
    backend, h = FakeBackend([job]), Handler("s", slow)
    d = Dispatcher(backend, {"s": h}, worker_id="w1", lease_seconds=1, poll_busy=0.01, poll_idle=0.02, shutdown_grace=2.0)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await asyncio.wait_for(started.wait(), 3)
    backend.lost.add(job.lease_token)  # another worker reclaimed it
    await asyncio.wait_for(cancelled.wait(), 3)
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert backend.completed == []  # the fenced ack is skipped entirely


async def test_graceful_shutdown_waits_for_inflight_jobs_then_releases():
    finished = asyncio.Event()

    async def work(job: ClaimedJob) -> None:
        await asyncio.sleep(0.3)
        finished.set()

    backend = FakeBackend([mk("s")])
    h = Handler("s", work)
    d = Dispatcher(backend, {"s": h}, worker_id="w1", lease_seconds=3, poll_busy=0.01, poll_idle=0.02, shutdown_grace=2.0)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    while not h.seen:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    stop.set()  # SIGTERM while a job is running
    await task
    assert finished.is_set() and backend.completed[0][1] == "succeeded" and backend.released == 1


async def test_shutdown_after_the_grace_period_cancels_and_releases_without_completing():
    async def forever(job: ClaimedJob) -> None:
        await asyncio.sleep(60)

    backend = FakeBackend([mk("s")])
    h = Handler("s", forever)
    d = Dispatcher(backend, {"s": h}, worker_id="w1", lease_seconds=3, poll_busy=0.01, poll_idle=0.02, shutdown_grace=0.2)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    while not h.seen:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert (
        backend.completed == [] and backend.released == 1
    )  # a shutdown is not a failure: the lease goes back, the attempt is not consumed


async def test_every_job_logs_its_pinned_versions_and_never_content(capsys):
    """ONT-03: the worker's log line per job carries the pipeline and ontology version it ran under, plus ids, counts and outcome: nothing content-like."""
    from kg_obs.logging import configure_logging

    configure_logging("INFO", service="kg-worker-test")
    ontology = uuid.uuid4()
    job = ClaimedJob(
        job_id=uuid.uuid4(), workspace_id=uuid.uuid4(), stage="s", subject_type="interaction", subject_id=uuid.uuid4(),
        payload={"generation": 0}, attempts=1, max_attempts=5, lease_token=uuid.uuid4(), pipeline_version="p3.v1", ontology_version_id=ontology, correlation_id="corr-9",
    )  # fmt: skip
    backend, handler = FakeBackend([job]), Handler("s")
    await run_until(backend, {"s": handler}, lambda: backend.completed)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    (done,) = [line for line in lines if line.get("event") == "job_done"]
    assert done["job_id"] == str(job.job_id) and done["stage"] == "s" and done["attempt"] == 1 and done["outcome"] == "succeeded"
    assert done["pipeline_version"] == "p3.v1" and done["ontology_version_id"] == str(ontology) and done["correlation_id"] == "corr-9"
    assert isinstance(done["duration_ms"], float) and not {"payload", "content", "text", "ciphertext"} & set(done)
