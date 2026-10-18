"""Readable rendering of a context package. `out` is the `/v1/context/build` response body."""

from __future__ import annotations

from typing import Any


def show_context(out: dict[str, Any], *, title: str = "context") -> str:
    text = str(out.get("context_text", ""))
    used, budget = out.get("tokens_used"), out.get("max_tokens") or out.get("token_budget")
    head = f"== {title} " + (f"({used}/{budget} tokens) " if used is not None else "") + f"status={out.get('status', '?')} =="
    parts = [head, text.rstrip()]
    for key in ("unknowns", "conflicts"):
        items = out.get(key) or []
        if items:
            parts.append(f"-- {key}: " + "; ".join(str(i.get("text") or i.get("signal") or i.get("key") or i) for i in items))
    cites = out.get("citations") or []
    if cites:
        parts.append(f"-- {len(cites)} citation(s)")
    return "\n".join(parts)


def context_lines(out: dict[str, Any]) -> list[str]:
    return [ln for ln in str(out.get("context_text", "")).splitlines() if ln.startswith("- ")]
