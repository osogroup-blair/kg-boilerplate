"""Tiered packing under a HARD token budget (phase-6 plan §5.9, CTX-01, CTX-05, ADR-033). Pure.

The ceiling is enforced on the exact bytes that will be returned: every candidate is admitted only if the WHOLE rendered package (headings, separators and all) still counts
at or under `max_tokens`, using the same `TokenCounter` that the trace records. There is no estimate to drift from the output, and the final text is the last text that passed.

    1. the REQUIRED tier (unknowns, conflicts, the subjects' critical current facts, required guidance) must fit as a whole, or the answer is `insufficient_budget`
       with the number of tokens it would need: it is never trimmed (CTX-05)
    2. then HIGH, then OPTIONAL, best score per token first, within soft per-group shares; whatever the shares left over flows to the next pass
    3. `max_items` caps what is added beyond the required tier

An item that does not fit is skipped and the next, smaller one is tried. Nothing is truncated mid-sentence: an item is in whole or not at all."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from kg_common.tokens import TokenCounter

from kg_context.models import Candidate
from kg_context.render import INSUFFICIENT_BUDGET_NOTICE, line_of, render, render_items

GROUP_OF = {
    "current_facts": "current_facts",
    "unverified_facts": "current_facts",
    "procedural": "instructions",
    "uncertainty": "conflicts_unknowns",
}
TIER_ORDER = ("required", "high", "optional")


@dataclass
class PackResult:
    ok: bool
    selected: list[Candidate]
    text: str  # what is returned as context_text (empty in items mode)
    used_tokens: int
    reserved_tokens: int
    required_tokens: int
    truncated: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)


def pack(
    ranked: list[Candidate],
    *,
    counter: TokenCounter,
    max_tokens: int,
    max_items: int,
    mode: str,
    response_mode: str,
    item_chars: int,
    section_budget: dict[str, float],
    as_of: dt.datetime | None = None,
) -> PackResult:
    index = {c.key: i for i, c in enumerate(ranked)}
    live = [c for c in ranked if c.exclusion is None]

    def body(selected: list[Candidate]) -> str:
        ordered = sorted(selected, key=lambda c: index[c.key])
        return (
            render_items(ordered, mode=mode, item_chars=item_chars)
            if response_mode == "items"
            else render(ordered, mode=mode, item_chars=item_chars, as_of=as_of)
        )

    def measure(selected: list[Candidate]) -> tuple[str, int]:
        text = body(selected)
        return text, counter.count(text)

    reserved = measure([])[1]
    for c in live:
        c.tokens = counter.count(line_of(c, mode, item_chars))
    required = [c for c in live if c.tier == "required"]
    text, used = measure(required)
    required_tokens = used
    if used > max_tokens:
        notice = INSUFFICIENT_BUDGET_NOTICE if response_mode != "items" else ""
        notice_tokens = counter.count(notice) if notice else 0
        if notice_tokens > max_tokens:
            notice, notice_tokens = "", 0
        refused = {"budget": 0}
        for c in live:
            c.exclusion = "budget"
            refused["budget"] += 1
        return PackResult(
            False,
            [],
            notice,
            notice_tokens,
            reserved,
            required_tokens,
            {t: sum(1 for c in live if c.tier == t) for t in TIER_ORDER},
            refused,
        )

    selected = list(required)
    group_tokens: dict[str, int] = {}
    for c in selected:
        group_tokens[GROUP_OF.get(c.section, "relevant")] = group_tokens.get(GROUP_OF.get(c.section, "relevant"), 0) + c.tokens
    truncated = {"high": 0, "optional": 0}
    excluded: dict[str, int] = {}

    def density(c: Candidate) -> tuple[float, int, str]:
        return (-(c.score / max(1, c.tokens)), index[c.key], c.key)

    for tier in ("high", "optional"):
        pool = sorted((c for c in live if c.tier == tier), key=density)
        deferred: list[Candidate] = []
        for c in pool:  # pass 1: within the soft group shares
            group = GROUP_OF.get(c.section, "relevant")
            share = section_budget.get(group)
            if share is not None and group_tokens.get(group, 0) + c.tokens > share * max_tokens:
                deferred.append(c)
                continue
            if len(selected) - len(required) >= max_items:
                c.exclusion = "max_items"
                continue
            _, trial_used = measure([*selected, c])
            if trial_used <= max_tokens:
                selected.append(c)
                group_tokens[group] = group_tokens.get(group, 0) + c.tokens
            else:
                deferred.append(c)
        for c in deferred:  # pass 2: whatever the shares left over
            if c.exclusion is not None:
                continue
            if len(selected) - len(required) >= max_items:
                c.exclusion = "max_items"
                continue
            _, trial_used = measure([*selected, c])
            if trial_used <= max_tokens:
                selected.append(c)
            else:
                c.exclusion = "budget"
    for c in live:
        if c.exclusion in ("budget", "max_items"):
            excluded[c.exclusion] = excluded.get(c.exclusion, 0) + 1
            if c.tier in truncated:
                truncated[c.tier] += 1
    final = sorted(selected, key=lambda c: index[c.key])
    final_text, final_used = measure(final)  # the text that is returned is measured once more, exactly as returned
    while (
        final_used > max_tokens
    ):  # a safety net: admission was exact, so this never runs; if it ever did, the cheapest-to-lose optional item goes, never a required one
        droppable = [c for c in final if c.tier != "required"]
        if not droppable:  # pragma: no cover
            raise AssertionError("context budget violated by the required tier")
        victim = min(droppable, key=lambda c: (TIER_ORDER.index(c.tier) * -1, c.score, c.key))
        victim.exclusion = "budget"
        truncated[victim.tier] = truncated.get(victim.tier, 0) + 1
        excluded["budget"] = excluded.get("budget", 0) + 1
        final.remove(victim)
        final_text, final_used = measure(final)
    return PackResult(
        True, final, "" if response_mode == "items" else final_text, final_used, reserved, required_tokens, truncated, excluded
    )
