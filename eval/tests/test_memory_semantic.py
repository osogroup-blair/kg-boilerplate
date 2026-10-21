"""The live memory evaluation's semantic fallback: exact substrings stay authoritative, the judge is consulted only when they fail, nothing else is relaxed."""

from __future__ import annotations

import json

import httpx

from eval.memory.judge import Judge
from eval.memory.run_eval import aggregate
from eval.memory.score import matches

WANT = {
    "type": "episodic",
    "status": "active",
    "evidence_status": "unverified",
    "subjects": ["Sarah Johnson"],
    "scope": "subject",
    "must_contain": ["software business", "Michael"],
    "must_not_contain": ["resigned", "quit"],
    "linked_facts": ["CONSIDERING"],
}


def got(content: str, **over: object) -> dict[str, object]:
    base = {
        "content": content,
        "type": "episodic",
        "status": "active",
        "evidence_status": "unverified",
        "subjects": ["Sarah Johnson"],
        "scopes": [("subject", "x")],
        "linked_facts": ["CONSIDERING"],
    }
    return {**base, **over}


def test_an_exact_substring_match_never_asks_the_judge():
    asked: list[str] = []
    assert matches(WANT, got("Sarah discussed a possible software business with Michael."), lambda r, t: asked.append(t) or False)
    assert asked == []


def test_a_paraphrase_is_accepted_only_if_the_judge_says_it_conveys_everything():
    paraphrase = got("Sarah is thinking about founding a company, encouraged by Michael.")
    assert not matches(WANT, paraphrase)  # offline: exact substrings only
    assert not matches(WANT, paraphrase, lambda r, t: False)
    assert matches(WANT, paraphrase, lambda r, t: r == ["software business", "Michael"])


def test_the_judge_never_overrides_the_structural_conditions_or_a_forbidden_claim():
    always = lambda r, t: True  # noqa: E731
    assert not matches(WANT, got("Sarah quit and will start a software business with Michael."), always)  # must_not_contain is exact
    assert not matches(WANT, got("Sarah is considering a company.", type="semantic"), always)
    assert not matches(WANT, got("Sarah is considering a company.", subjects=["Michael"]), always)
    assert not matches(WANT, got("Sarah is considering a company.", scopes=[("workspace", "w")]), always)
    assert not matches(WANT, got("Sarah is considering a company.", linked_facts=[]), always)


def judge_with(handler) -> Judge:  # type: ignore[no-untyped-def]
    return Judge("https://router.example/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_judge_asks_at_temperature_zero_with_a_strict_schema_and_logs_the_verdict():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"conveys_all": False, "missing": ["Michael"]})}}]})

    j = judge_with(handler)
    assert j.conveys(["software business", "Michael"], "Sarah wants a company.") is False
    assert seen["temperature"] == 0 and seen["model"] == "oso/code-high" and seen["response_format"]["json_schema"]["strict"] is True  # type: ignore[index]
    assert seen["auth"] == "Bearer k"
    assert j.log == [
        {"required": ["software business", "Michael"], "memory": "Sarah wants a company.", "conveys_all": False, "missing": ["Michael"]}
    ]


def test_the_judge_retries_a_busy_router_and_then_gives_up():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"code": "routing_failed"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"conveys_all": True, "missing": []})}}]})

    import eval.memory.judge as module

    module.time.sleep = lambda s: None  # type: ignore[assignment]
    assert judge_with(flaky).conveys(["x"], "x") is True and calls["n"] == 3
    import pytest

    with pytest.raises(httpx.HTTPError):
        judge_with(lambda r: httpx.Response(503, json={})).conveys(["x"], "x")


def test_aggregate_reports_the_mean_and_range_per_run_and_per_scenario():
    def r(run: int, sid: str, tp: int, fp: int, fn: int, sec: float) -> dict[str, object]:
        return {
            "run": run,
            "id": sid,
            "memories": {"tp": tp, "fp": fp, "fn": fn},
            "seconds": sec,
            "escalation_violations": 0,
            "unmapped_subject_leaks": 0,
            "procedural_auto_activations": 0,
            "scope_violations": 0,
        }

    results = [r(1, "a", 1, 0, 0, 10.0), r(1, "b", 0, 1, 1, 20.0), r(2, "a", 1, 0, 0, 14.0), r(2, "b", 1, 0, 0, 22.0)]
    agg = aggregate(results, 2)
    assert [x["precision"] for x in agg["per_run"]] == [0.5, 1.0] and [x["recall"] for x in agg["per_run"]] == [0.5, 1.0]
    assert agg["overall"]["precision"] == {"mean": 0.75, "min": 0.5, "max": 1.0}
    assert agg["per_scenario"]["b"]["tp"] == [0, 1] and agg["per_scenario"]["b"]["seconds"] == {"mean": 21.0, "min": 20.0, "max": 22.0}
    assert agg["overall"]["strict_counters_all_zero"] is True
