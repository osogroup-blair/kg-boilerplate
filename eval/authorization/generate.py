"""Generates eval/datasets/authorization/v1/cases.json: one case per (route, caller class). The route list and each route's policy come from the code that serves them
(`ALL_ROUTERS`, `policy_of`); the EXPECTATION per caller class comes from the role/action matrix (`ROLE_ACTIONS`):

    unauth         a route that is not public answers 401
    <role>         a role without the route's action answers 403; a role with it answers anything but 401 and 5xx (a handler may still say 403/404/409/422)
    foreign        an administrator of ANOTHER workspace never gets this workspace's data: the response holds none of its ids, and a mutation changes none of its rows

The file is hashed into the dataset manifest, so a new route, a changed policy or a changed matrix shows up as a changed dataset and must be re-frozen on purpose."""

from __future__ import annotations

import json
import re
from pathlib import Path

from kg_api.routers import ALL_ROUTERS
from kg_domain.enums import Role
from kg_governance.authz.policy import ROLE_ACTIONS, policy_of

OUT = Path(__file__).resolve().parents[1] / "datasets" / "authorization" / "v1" / "cases.json"
CALLERS = {
    "admin": Role.WORKSPACE_ADMIN,
    "operator": Role.OPERATOR,
    "reviewer": Role.REVIEWER,
    "app": Role.APP_WRITER,
    "agent": Role.AGENT,
    "reader": Role.READER,
}
SKIP = re.compile(r"^/(docs|redoc|openapi\.json)")


def build() -> dict[str, object]:
    routes = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            endpoint = getattr(route, "endpoint", None)
            if not path or endpoint is None or SKIP.match(path):
                continue
            policy = policy_of(endpoint)
            assert policy is not None, path
            for method in sorted(m for m in getattr(route, "methods", set()) if m not in ("HEAD", "OPTIONS")):
                routes.append((method, path, policy))
    cases: list[dict[str, object]] = []
    for method, path, policy in sorted(routes, key=lambda r: (r[1], r[0])):
        slug = f"{method} {path}"
        if policy.kind == "public":
            cases.append({"id": f"{slug} :: unauth", "method": method, "path": path, "caller": "unauth", "expect": "public"})
            continue
        cases.append({"id": f"{slug} :: unauth", "method": method, "path": path, "caller": "unauth", "expect": "401"})
        for name, role in CALLERS.items():
            if policy.kind == "authenticated" or (policy.action is not None and policy.action in ROLE_ACTIONS[role]):
                expect = "allowed"
            else:
                expect = "403"
            cases.append(
                {
                    "id": f"{slug} :: {name}",
                    "method": method,
                    "path": path,
                    "caller": name,
                    "expect": expect,
                    "action": policy.action.value if policy.action else None,
                }
            )
        cases.append({"id": f"{slug} :: foreign", "method": method, "path": path, "caller": "foreign", "expect": "no_leak"})
    return {"suite": "authorization", "version": "v1", "seed": 7, "provenance": "synthetic only", "cases": cases}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    OUT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(data['cases'])} cases)")  # type: ignore[arg-type]
