"""Pytest plugin: turns test outcomes into spikes/results/<id>.json ({id, tag, status, evidence, notes, versions}).

Check IDs come from test names: test_g03_x -> G-03, test_gr06_x -> GR-06, test_pv02_x -> PV-02 ...
Tests in tools/mock_router/tests aggregate into MR-01.
Tests attach findings with `note(id, text)` / `evidence(id, key=value)` and may override the status with `set_status(id, 'fail')`
for the case "the check discovered a limitation; a fallback was chosen" (pytest still passes: the finding is the result).
"""
from __future__ import annotations

import importlib.metadata as md
import os
import json
import platform
import re
from collections import defaultdict
from pathlib import Path

import pytest

RESULTS = Path(__file__).parent / "results"
_NAME = re.compile(r"test_(g|gr|pv|tk|au|nw|lv)(\d+)_", re.I)
_state: dict[str, dict] = defaultdict(lambda: {"outcomes": [], "evidence": {}, "notes": [], "status_override": None, "tests": []})
LIVE_IDS = {"G-05", "GR-11", "GR-12", "GR-13", "TK-02"}


def note(check_id: str, text: str) -> None:
    _state[check_id]["notes"].append(text)


def evidence(check_id: str, **items) -> None:
    _state[check_id]["evidence"].update(items)


def set_status(check_id: str, status: str) -> None:
    assert status in ("pass", "fail", "skipped", "deferred")
    _state[check_id]["status_override"] = status


def _check_id(nodeid: str) -> str | None:
    if nodeid.startswith("test_contract.py"):
        return "MR-01"
    m = _NAME.search(nodeid.split("::")[-1])
    return f"{m.group(1).upper()}-{int(m.group(2)):02d}" if m else None


def _versions() -> dict:
    out = {"python": platform.python_version()}
    for pkg in ("graphiti-core", "neo4j", "pydantic", "openai", "tiktoken", "psycopg", "pgvector", "fastapi", "httpx", "pyjwt"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            pass
    return out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    cid = _check_id(item.nodeid)
    if cid is None:
        return
    if rep.when == "call" or (rep.when == "setup" and rep.outcome in ("skipped", "failed")):
        st = _state[cid]
        st["outcomes"].append("pass" if rep.passed else "skipped" if rep.skipped else "fail")
        st["tests"].append(item.nodeid.split("/")[-1])
        if rep.failed:
            st["evidence"].setdefault("failures", []).append(str(rep.longrepr)[-600:])


def pytest_sessionfinish(session, exitstatus):
    if not _state:
        return
    RESULTS.mkdir(exist_ok=True)
    versions = _versions()
    for cid, st in _state.items():
        outcomes = st["outcomes"]
        if st["status_override"]:
            status = st["status_override"]
        elif "fail" in outcomes:
            status = "fail"
        elif outcomes and all(o == "skipped" for o in outcomes):
            status = "deferred" if cid in LIVE_IDS else "skipped"
        else:
            status = "pass"
        live_run = os.environ.get("OSO_ROUTER_MODE") == "live"
        tag = ("LIVE" if live_run and status != "deferred" else "LIVE-DEFERRED") if cid in LIVE_IDS else "OFFLINE"
        fname = f"live-{cid}.json" if live_run and cid in LIVE_IDS and status != "deferred" else f"{cid}.json"   # live evidence never overwritten by offline runs
        (RESULTS / fname).write_text(json.dumps({
            "id": cid, "tag": tag, "status": status, "evidence": {"tests": sorted(set(st["tests"])), **st["evidence"]},
            "notes": " ".join(st["notes"]), "versions": versions}, indent=2, default=str) + "\n")
