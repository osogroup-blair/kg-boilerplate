"""A fixed context build against a running API, captured as ids and a status only (no text). Taken on the source stack at backup time and again on the restored stack: the two
must match in status and items, which is how the rehearsal shows the restored knowledge answers the way the original did."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from tools.backup.config import REPO

PROBE_KEY = "demo/agent_planning"
PROBES = (
    {"task": "Help Sarah assess the implications of starting a business", "profile": "planning", "max_tokens": 600},
    {"task": "Help Sarah assess the implications of starting a business", "profile": "planning", "max_tokens": 250},
)


def credentials() -> dict[str, str]:
    path = REPO / ".local" / "dev-credentials.json"
    return json.loads(path.read_text()) if path.exists() else {}


def build(api_url: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 (a loopback URL chosen by the operator)
        api_url.rstrip("/") + "/v1/context/build",
        data=json.dumps(body).encode(),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return json.loads(resp.read())  # type: ignore[no-any-return]


def capture(api_url: str) -> dict[str, Any]:
    """Empty (`available: false`) when the dev credentials are not there: the rehearsal then reports the probe as skipped, not passed."""
    key = credentials().get(PROBE_KEY)
    if not key:
        return {"available": False, "builds": []}
    out = []
    for body in PROBES:
        try:
            for _ in range(
                3
            ):  # the first build after a (re)start can time out on a cold graph and answer `partial`: the probe measures the knowledge, not the warm-up
                r = build(api_url, key, body)
                if r["status"] != "partial":
                    break
        except Exception as exc:
            return {"available": False, "error": type(exc).__name__, "builds": []}
        items = sorted(f"{i['kind']}:{i.get('id')}" for k in ("facts", "memories", "instructions") for i in r.get(k, []))
        out.append(
            {"max_tokens": body["max_tokens"], "status": r["status"], "items": items, "tokens": (r.get("budgets") or {}).get("used_tokens")}
        )
    return {"available": True, "builds": out}


def save(setdir: Path, api_url: str) -> dict[str, Any]:
    probe = capture(api_url)
    (setdir / "probe.json").write_text(json.dumps(probe, indent=2, sort_keys=True))
    return probe
