"""The authorization evaluation (SEC-01/02, AT-08/09/10): the frozen probe set against a live in-process API.

    make eval-authorization

STRICT, all 0: `policy_mismatches` (a route that should refuse did not, or the reverse for unauthenticated callers), `leakage` (a foreign administrator, a frozen subject or a deleted
subject showing anything of this workspace), `server_errors` (any 5xx), `foreign_mutations` (rows changed by another workspace's administrator).

Mutating probes use random ids for the callers who ARE allowed (so nothing real is touched) and the real ids for callers who must be refused and for the foreign administrator
(refusal and isolation are exactly what is being checked). Output: eval/reports/authorization-mock-<date>.json."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.harness import stack  # noqa: E402

CASES = ROOT / "eval" / "datasets" / "authorization" / "v1" / "cases.json"
REPORTS = ROOT / "eval" / "reports"
PARAM = re.compile(r"\{([^}]+)\}")
COUNT_TABLES = (
    "entities",
    "facts",
    "curated_memories",
    "interactions",
    "commands",
    "principals",
    "role_assignments",
    "api_keys",
    "deletion_requests",
    "workspace_settings",
    "ontology_versions",
    "review_items",
)


def fill(path: str, world: dict[str, str], *, real: bool) -> str:
    def value(m: re.Match[str]) -> str:
        name = m.group(1)
        if real and name in world:
            return world[name]
        if name == "prefix":
            return "kgk_nothing"
        if name == "namespace":
            return "ws-" + str(uuid.uuid4()) + "-r1"
        return str(uuid.uuid4())

    return PARAM.sub(value, path)


async def counts(api: Any, t: Any) -> dict[str, int]:
    from tests.support.p8 import raw

    out = {}
    for table in COUNT_TABLES:
        out[table] = (await raw(api, t, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :ws"))[0].n  # noqa: S608 (constants)
    return out


async def build_world(api: Any, t: Any) -> tuple[dict[str, str], dict[str, str]]:
    from tests.support.p8 import deletion_world, raw

    w = await deletion_world(api, t)
    built = await t.req("POST", "/v1/context/build", "admin", json={"task": "What do we know about Sarah?"}, key="")
    versions = await raw(api, t, "SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws")
    jobs = await raw(api, t, "SELECT id FROM outbox_jobs WHERE workspace_id = :ws LIMIT 1")
    cmds = await raw(api, t, "SELECT id FROM commands WHERE workspace_id = :ws LIMIT 1")
    world = {
        "entity_id": w["sarah"],
        "fact_id": w["works"],
        "memory_id": w["m_keep"],
        "interaction_id": w["i_sar"],
        "event_id": w["i_sar"],
        "principal_id": await t.principal_id("agent"),
        "version_id": str(versions[0].active_version_id),
        "context_id": str(built.json().get("context_id") or uuid.uuid4()),
        "job_id": str(jobs[0].id) if jobs else str(uuid.uuid4()),
        "command_id": str(cmds[0].id) if cmds else str(uuid.uuid4()),
    }
    frozen = {"entity_id": w["zeb"], "fact_id": w["knows"], "memory_id": w["m_zeb"], "interaction_id": w["i_zeb"], "event_id": w["i_zeb"]}
    return world, frozen | {"_zeb": w["zeb"]}


async def run() -> dict[str, Any]:
    from tests.support.p8 import request_deletion, run_deletion
    from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

    cases = json.loads(CASES.read_text())["cases"]
    problems: list[str] = []
    stats = {
        "cases": len(cases),
        "probes": 0,
        "policy_mismatches": 0,
        "leakage": 0,
        "server_errors": 0,
        "foreign_mutations": 0,
        "frozen_leaks": 0,
        "deleted_leaks": 0,
    }
    failures: list[dict[str, Any]] = []
    async with stack() as api:
        t = await make_tenant(api, ontology=GOVERNANCE_YAML)
        other = await make_tenant(api, ontology=GOVERNANCE_YAML)
        try:
            world, frozen = await build_world(api, t)
            known = set(world.values()) | {v for k, v in frozen.items() if not k.startswith("_")} | {t.slug, str(t.ws)}
            for case in cases:
                method, path, caller = case["method"], case["path"], case["caller"]
                mutating = method in ("POST", "PUT", "PATCH", "DELETE")
                real = not mutating or case["expect"] in ("403", "no_leak", "401")
                url = fill(path, world, real=real)
                headers: dict[str, str] = {}
                if caller == "foreign":
                    headers = other.h("admin")
                elif caller != "unauth":
                    headers = t.h(caller)
                if mutating:
                    headers["idempotency-key"] = str(uuid.uuid4())
                before = await counts(api, t) if caller == "foreign" and mutating else None
                resp = await api.client.request(method, url, headers=headers, json={} if mutating else None)
                stats["probes"] += 1
                bad = ""
                if resp.status_code >= 500:
                    stats["server_errors"] += 1
                    bad = f"server error {resp.status_code}"
                elif case["expect"] == "401" and resp.status_code != 401:
                    stats["policy_mismatches"] += 1
                    bad = f"expected 401, got {resp.status_code}"
                elif case["expect"] == "403" and resp.status_code != 403:
                    stats["policy_mismatches"] += 1
                    bad = f"expected 403, got {resp.status_code}"
                elif case["expect"] == "allowed" and resp.status_code == 401:
                    stats["policy_mismatches"] += 1
                    bad = "an authenticated, permitted caller was answered 401"
                elif case["expect"] == "no_leak":
                    if any(i in resp.text for i in known):
                        stats["leakage"] += 1
                        bad = "a foreign administrator's response carries this workspace's ids"
                    if before is not None and (after := await counts(api, t)) != before:
                        stats["foreign_mutations"] += 1
                        bad = f"a foreign mutation changed rows: {[k for k in before if before[k] != after[k]]}"
                if bad:
                    failures.append({"case": case["id"], "problem": bad, "status": resp.status_code})
            # a subject FROZEN by a deletion request disappears from every read at once; a DELETED one stays gone
            reads = [
                (c["path"], c)
                for c in cases
                if c["method"] == "GET"
                and c["caller"] == "admin"
                and PARAM.search(c["path"])
                and set(PARAM.findall(c["path"])) <= set(frozen)
            ]
            request = await request_deletion(t, "subject", frozen["_zeb"], "app")
            assert request.status_code == 202, request.text
            for stage_name, key in (("frozen", "frozen_leaks"), ("deleted", "deleted_leaks")):
                if stage_name == "deleted":
                    done = await run_deletion(api, t, request.json()["request_id"])
                    assert done["status"] in ("completed", "completed_with_exceptions"), done
                for path, case in reads:
                    resp = await api.client.get(fill(path, frozen, real=True), headers=t.h("admin"))
                    stats["probes"] += 1
                    if resp.status_code not in (
                        404,
                        422,
                    ):  # 422: the request is refused for its own shape before any object is looked up, so it reveals nothing
                        stats[key] += 1
                        stats["leakage"] += 1
                        failures.append(
                            {
                                "case": case["id"],
                                "problem": f"a {stage_name} subject answered {resp.status_code} instead of 404/422",
                                "status": resp.status_code,
                            }
                        )
        finally:
            for tenant in (t, other):
                await clear_graph(api, tenant.group_id)
    for k in ("policy_mismatches", "leakage", "server_errors", "foreign_mutations"):
        if stats[k]:
            problems.append(f"{stats[k]} {k.replace('_', ' ')}")
    return {"mode": "mock", "date": dt.date.today().isoformat(), "summary": stats, "failures": failures[:50], "problems": problems}


def main() -> int:
    report = asyncio.run(run())
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"authorization-mock-{report['date']}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report["summary"]))
    for f in report["failures"][:15]:
        print("FAIL", f, file=sys.stderr)
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
