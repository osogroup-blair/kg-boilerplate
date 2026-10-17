"""The release gate's own rules: strict metrics are exact, proposed ones have tolerances, latency has slack, a missing metric is a failure, and a changed dataset fails."""

from __future__ import annotations

import copy

import pytest

from eval import gate

pytestmark = pytest.mark.unit

BASE = {
    "strict": {"context.leakage": 0, "context.budget_adherence": 1.0},
    "proposed": {
        "context.evidence_recall_cases_1_8": 0.95,
        "context.irrelevant_inclusion": 0.05,
        "extraction.tokens_per_episode": 4000.0,
        "context.latency_p95": 0.07,
    },
    "tolerances": gate.TOLERANCE,
    "datasets": {},
}
OK = {
    "metrics": {
        "context.leakage": 0.0,
        "context.budget_adherence": 1.0,
        "context.evidence_recall_cases_1_8": 0.95,
        "context.irrelevant_inclusion": 0.05,
        "extraction.tokens_per_episode": 4000.0,
        "context.latency_p95": 0.07,
    },
    "datasets": {"problems": []},
    "suites": {"context": {"exit": 0}},
}


def run(**changes):
    cur = copy.deepcopy(OK)
    cur["metrics"].update(changes)
    return gate.compare(cur, BASE)


def test_an_identical_run_passes():
    assert run() == []


def test_strict_metrics_have_no_tolerance_at_all():
    assert any("context.leakage" in p for p in run(**{"context.leakage": 1.0}))
    assert any("context.budget_adherence" in p for p in run(**{"context.budget_adherence": 0.999}))


def test_relevance_cost_and_latency_have_their_tolerances():
    assert run(**{"context.evidence_recall_cases_1_8": 0.94}) == []  # inside -0.02
    assert any("evidence_recall" in p for p in run(**{"context.evidence_recall_cases_1_8": 0.92}))
    assert run(**{"context.irrelevant_inclusion": 0.07}) == []  # inside +0.02
    assert any("irrelevant" in p for p in run(**{"context.irrelevant_inclusion": 0.08}))
    assert run(**{"extraction.tokens_per_episode": 4399.0}) == []  # inside +10%
    assert any("tokens_per_episode" in p for p in run(**{"extraction.tokens_per_episode": 4500.0}))
    assert run(**{"context.latency_p95": 0.30}) == []  # a few hundred ms of noise on a 70 ms mock is not a regression (slack)
    assert any("latency_p95" in p for p in run(**{"context.latency_p95": 0.5}))


def test_a_metric_that_was_not_measured_fails_instead_of_passing_silently():
    cur = copy.deepcopy(OK)
    del cur["metrics"]["context.leakage"]
    assert any("not measured" in p for p in gate.compare(cur, BASE))


def test_a_failed_suite_and_a_changed_dataset_fail():
    cur = copy.deepcopy(OK)
    cur["suites"]["context"]["exit"] = 1
    cur["datasets"]["problems"] = ["context: files changed since the freeze"]
    problems = gate.compare(cur, BASE)
    assert any("SUITE context" in p for p in problems) and any("DATASET" in p for p in problems)
