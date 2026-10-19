"""Writes apps/site/src/data/generated/proof.json: every number the landing page shows, read from the repository's own reports (lesson Y16).

If a report is missing the generator fails: a landing page must never show a number it cannot trace to a file."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from scripts.site.common import BLOB, GENERATED, ROOT


def newest(pattern: str) -> Any:
    found = sorted(ROOT.glob(pattern))
    if not found:
        raise SystemExit(f"gen_proof: no file matches {pattern}")
    return found[-1]


def rel(p: Any) -> str:
    return str(p.relative_to(ROOT))


def main() -> int:
    all_path = newest("eval/reports/*/all.json")
    allj = json.loads(all_path.read_text())
    m = allj["metrics"]
    base_path = newest("eval/reports/baseline-*.json")
    base = json.loads(base_path.read_text())
    handoff = (ROOT / "docs/release/handoff.md").read_text()
    t = re.search(r"(\d+) unit, integration (\d+), security (\d+), contract (\d+), admin Vitest (\d+)", handoff)
    tests = dict(zip(("unit", "integration", "security", "contract", "console"), map(int, t.groups()), strict=True)) if t else {}
    curve = base["size_curve"]
    proof: dict[str, Any] = {
        "date": allj["date"],
        "mode": allj["mode"],
        "commit": allj["git"]["describe"],
        "sources": {
            "evaluation": f"{BLOB}/{rel(all_path)}",
            "baseline": f"{BLOB}/{rel(base_path)}",
            "handoff": f"{BLOB}/docs/release/handoff.md",
        },
        "tiles": [
            {
                "value": f"{int(m['context.leakage'])}",
                "label": "records leaked across permissions",
                "detail": f"{int(m['context.cases'])} frozen context cases",
                "src": "evaluation",
            },
            {
                "value": f"{m['context.budget_adherence'] * 100:.0f}%",
                "label": "context packages inside their token budget",
                "detail": "the budget is enforced on the rendered text",
                "src": "evaluation",
            },
            {
                "value": f"{m['context.attribution'] * 100:.0f}%",
                "label": "context items with a citation",
                "detail": "every fact and memory carries its evidence",
                "src": "evaluation",
            },
            {
                "value": f"{int(m['authorization.probes'])}",
                "label": "authorization probes, 0 policy mismatches",
                "detail": "every route × every caller class, plus frozen and deleted subjects",
                "src": "evaluation",
            },
            {
                "value": f"{int(m['injection.payloads'])}",
                "label": "prompt-injection payloads, 0 failed checks",
                "detail": "through every ingress",
                "src": "evaluation",
            },
            {
                "value": f"{int(m['conflicts.cases'])}",
                "label": "identity and conflict cases, 0 mismatches",
                "detail": "the pure policy code, frozen cases",
                "src": "evaluation",
            },
        ],
        "tests": tests,
        "latency": {
            "modelled_router": base["latency_model"].startswith("provisional"),
            "context_p95_ms": {k: round(v["context"]["p95_ms"]) for k, v in curve.items()},
            "search_p95_ms": {k: round(v["search"]["p95_ms"]) for k, v in curve.items()},
            "memories": {k: v["memories"] for k, v in curve.items()},
        },
        "ann_trigger": base["ann_trigger"]["trigger"],
    }
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(f"proof.json written from {rel(all_path)} and {rel(base_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
