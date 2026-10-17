"""P6 test rig: Sarah's world through the REAL pipeline (commands, extraction, memory, embeddings), and context helpers over the real API.

`sarah_world` is the state the demo builds, in the order the blueprint tells it:
    commands     Sarah (Engineering Manager at Acme since 2024-06-01, Software Engineer before), Michael, Entrepreneurship
    evt_003      extraction: CONSIDERING (unverified) + an episodic memory (active, unverified) linked to the fact
    NewCo        extraction: an unverified WORKS_AT NewCo claim (a competing proposal and a contradiction review; Acme stays current)
    memories     an approved preference (semantic), procedural guidance, a project finding, the cycling distractor, and a private working note
Agents: "agent" is the PLANNING agent (Sarah + the project), "agent2" the RESEARCH agent (Sarah + the project), "agent3" has no grants at all."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from kg_db.session import tenant_session
from sqlalchemy import text

from tests.support.p4 import EVT003, NEWCO, add_mock_rules, ctx_of, run_event, sarah_story
from tests.support.p5 import (
    BUSINESS,
    CYCLIST_TEXT,
    GUIDELINE,
    PREFERENCE,
    accept_event,
    run_embeds,
    save,
)

FINDING = "NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026)."
NOTE = "check NewCo licensing rules for the plan, private to the research agent"
TASK = "Help Sarah assess the implications of starting a business"
EMPTY_MEMORY_RULE = {
    "when": {"alias": "oso/general", "schema_name": "MemoryCandidates", "contains": ["work at NewCo now"]},
    "response": {"candidates": []},
}


async def sarah_world(
    api: Any, t: Any, *, newco: bool = True, cycling: bool = True, finding: bool = True, mock_rules: bool = True
) -> dict[str, str]:
    """Builds the world in a fresh `tenant_ctx` tenant. Returns the ids the tests need. Requires the strict mock fixture (the memory stage answers from scenarios)."""
    ids = await sarah_story(t, projected=False)
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", ids["sarah"])
    if mock_rules:
        await add_mock_rules([EMPTY_MEMORY_RULE])
    subjects = [ids["sarah"], ids["michael"], ids["topic"]]
    await run_event(api, t, EVT003, subjects=subjects)
    rows = await q(
        api, t, "SELECT id::text AS id FROM curated_memories WHERE workspace_id = :ws AND origin = 'extracted' AND type = 'episodic'"
    )
    if rows:
        ids["business"] = rows[0].id
    else:  # a live model may extract nothing from evt_003; the benchmark then still has the memory it asks about, saved explicitly
        assert not mock_rules, "evt_003 should have produced one episodic memory"
        evt = await accept_event(t, {"kind": "message", "content": EVT003["content"]}, subjects=[ids["sarah"]])
        saved = await save(
            t,
            "admin",
            {
                "type": "episodic",
                "content": BUSINESS,
                "subject_ids": [ids["sarah"]],
                "source_interaction_id": evt,
                "purpose": "business aspiration",
            },
        )
        ids["business"] = saved.json()["memory_id"]
    for key, rel in (("considering", "CONSIDERING"), ("knows", "KNOWS")):
        found = await q(
            api, t, "SELECT id::text AS id FROM facts WHERE workspace_id = :ws AND rel_type = :rel ORDER BY recorded_at LIMIT 1", rel=rel
        )
        if found:
            ids[key] = found[0].id
    if newco:
        await run_event(api, t, NEWCO, subjects=[ids["sarah"]])
        claim = await q(
            api,
            t,
            "SELECT f.id::text AS id, f.status FROM facts f JOIN entities e ON e.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.rel_type = 'WORKS_AT' AND e.display_name = 'NewCo'",
        )
        if claim:
            assert claim[0].status in ("proposed", "disputed"), claim
            ids["newco_fact"] = claim[0].id
        else:
            assert not mock_rules, "the NewCo event should have produced a competing claim"
    evt004 = await accept_event(
        t, {"kind": "message", "content": "Sarah Johnson: please line the options up with risks and trade-offs."}, subjects=[ids["sarah"]]
    )
    pref = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": PREFERENCE,
            "subject_ids": [ids["sarah"]],
            "source_interaction_id": evt004,
            "purpose": "advice style",
        },
    )
    assert pref.status_code == 201, pref.text
    ids["pref"] = pref.json()["memory_id"]
    guide = await save(
        t,
        "admin",
        {
            "type": "procedural",
            "content": GUIDELINE,
            "subject_ids": [ids["sarah"]],
            "source_interaction_id": evt004,
            "purpose": "advice style",
        },
    )
    assert guide.status_code == 201, guide.text
    ids["guide"] = guide.json()["memory_id"]
    if finding:
        evt = await accept_event(t, {"kind": "message", "content": "Research Agent: the NewCo plan needs a state licence."}, who="agent2")
        f = await save(
            t,
            "admin",
            {
                "type": "semantic",
                "content": FINDING,
                "scopes": [{"kind": "project", "ref": "newco-plan"}],
                "source_interaction_id": evt,
                "purpose": "research finding",
            },
        )
        assert f.status_code == 201, f.text
        ids["finding"] = f.json()["memory_id"]
    if cycling:
        evt = await accept_event(t, {"kind": "message", "content": CYCLIST_TEXT}, subjects=[ids["sarah"]])
        c = await save(
            t,
            "admin",
            {"type": "episodic", "content": CYCLIST_TEXT, "subject_ids": [ids["sarah"]], "source_interaction_id": evt, "purpose": "hobby"},
        )
        assert c.status_code == 201, c.text
        ids["cycling"] = c.json()["memory_id"]
    note = await save(t, "agent2", {"type": "working", "content": NOTE, "run_id": "r1"})
    assert note.status_code == 201, note.text
    ids["note"] = note.json()["memory_id"]
    await run_embeds(api, t)
    return ids


async def q(api: Any, t: Any, query: str, **params: Any) -> list[Any]:
    async with tenant_session(api.engine, ctx_of(t)) as c:
        return list((await c.execute(text(query), {"ws": t.ws, **params})).all())


async def build(t: Any, who: str, task: str = TASK, **body: Any) -> httpx.Response:
    return await t.req("POST", "/v1/context/build", who, json={"task": task, **body}, key="")


async def ok_build(t: Any, who: str, task: str = TASK, **body: Any) -> dict[str, Any]:
    r = await build(t, who, task, **body)
    assert r.status_code == 200, r.text
    return dict(r.json())


def texts(out: dict[str, Any]) -> str:
    return str(out["context_text"])


def ids_in(out: dict[str, Any]) -> set[str]:
    return {i["id"] for k in ("facts", "memories", "instructions", "conflicts") for i in out[k] if i["id"]}


def lines(out: dict[str, Any]) -> list[str]:
    return [ln for ln in texts(out).splitlines() if ln.startswith("- ")]


def new_run() -> str:
    return f"r{uuid.uuid4().hex[:8]}"


async def owner_rows(query: str, **params: Any) -> list[Any]:
    """The owner role (bypasses RLS): for probes that must see rows, or change a clock, no tenant session can."""
    from tests.support.p5 import owner_sql

    return await owner_sql(query, **params)
