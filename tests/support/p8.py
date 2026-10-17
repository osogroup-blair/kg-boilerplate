"""P8 test rig: corrections, consolidation, deletion and simulation helpers over the real API, plus the SQL probes the suites share."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from kg_db.session import tenant_session
from sqlalchemy import text

from tests.support.p4 import HR, ctx_of, sql


def rev_header(revision: int) -> dict[str, str]:
    return {"if-match": f'W/"rev-{revision}"'}


async def fact_rev(t: Any, fact_id: str, who: str = "admin") -> int:
    r = await t.get(f"/v1/relationships/{fact_id}", who)
    assert r.status_code == 200, r.text
    return int(r.json()["revision"])


async def entity_rev(t: Any, entity_id: str, who: str = "admin") -> int:
    r = await t.get(f"/v1/entities/{entity_id}", who)
    assert r.status_code == 200, r.text
    return int(r.json()["revision"])


async def correct_fact(
    t: Any,
    fact_id: str,
    kind: str,
    who: str = "reviewer",
    *,
    reason: str = "source_error",
    key: str | None = None,
    revision: int | None = None,
    **extra: Any,
) -> httpx.Response:
    rev = revision if revision is not None else await fact_rev(t, fact_id, "admin")
    return await t.req(
        "POST",
        f"/v1/relationships/{fact_id}/corrections",
        who,
        json={"kind": kind, "reason_code": reason, **extra},
        key=key,
        headers=rev_header(rev),
    )


async def correct_entity(
    t: Any,
    entity_id: str,
    kind: str,
    who: str = "admin",
    *,
    reason: str = "source_error",
    key: str | None = None,
    revision: int | None = None,
    **extra: Any,
) -> httpx.Response:
    rev = revision if revision is not None else await entity_rev(t, entity_id, "admin")
    return await t.req(
        "POST",
        f"/v1/entities/{entity_id}/corrections",
        who,
        json={"kind": kind, "reason_code": reason, **extra},
        key=key,
        headers=rev_header(rev),
    )


async def correct_memory(
    t: Any, memory_id: str, kind: str, who: str = "reviewer", *, reason: str = "other", key: str | None = None, **extra: Any
) -> httpx.Response:
    got = await t.req("GET", f"/v1/memories/{memory_id}", who)
    assert got.status_code == 200, got.text
    return await t.req(
        "POST",
        f"/v1/memories/{memory_id}/corrections",
        who,
        json={"kind": kind, "reason_code": reason, **extra},
        key=key,
        headers={"if-match": got.headers["etag"]},
    )


async def audit_rows(api: Any, t: Any, action: str, **where: Any) -> list[Any]:
    clause = "".join(f" AND {k} = :{k}" for k in where)
    return await sql(
        api, t, f"SELECT * FROM audit_log WHERE workspace_id = :ws AND action = :a{clause} ORDER BY created_at, id", a=action, **where
    )  # noqa: S608


async def reviews_of(api: Any, t: Any, item_type: str, status: str = "open") -> list[Any]:
    return await sql(
        api,
        t,
        "SELECT * FROM review_items WHERE workspace_id = :ws AND item_type = :ityp AND status = :st ORDER BY created_at, id",
        ityp=item_type,
        st=status,
    )


async def edge_of(api: Any, t: Any, fact_id: str) -> Any:
    return await api.app.state.kg.graph.scoped(ctx_of(t)).read_edge(fact_id)


async def node_of(api: Any, t: Any, entity_id: str) -> Any:
    return await api.app.state.kg.graph.scoped(ctx_of(t)).read_node(entity_id)


async def reconcile_clean(api: Any, t: Any) -> None:
    report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
    assert report.drift == [], report.drift


async def prime_org(t: Any, name: str, ref: str) -> str:
    r = await t.entity("Organization", name, external_ids=HR(ref), source_system="hr", source_ref=ref)
    assert r.status_code in (200, 201), r.text
    return str(r.json()["entity_id"])


async def raw(api: Any, t: Any, query: str, **params: Any) -> list[Any]:
    """A tenant-scoped probe that sees everything of the tenant, frozen rows included (the API hides them)."""
    async with tenant_session(api.engine, ctx_of(t)) as c:
        result = await c.execute(text(query), {"ws": t.ws, **params})
        return list(result.all()) if result.returns_rows else []


def new_id() -> str:
    return str(uuid.uuid4())


async def owner_exec(query: str, **params: Any) -> list[Any]:
    from tests.support.p5 import owner_sql

    return await owner_sql(query, **params)


async def run_consolidation(api: Any, t: Any, *, timeout: float = 90.0) -> None:  # noqa: ASYNC109
    """The consolidation jobs are DELAYED (the debounce window); the suite makes them due, then runs the real worker until the outbox is quiet."""
    from tests.support.worker import running_worker, wait_until

    await owner_exec(
        "UPDATE outbox_jobs SET available_at = now() WHERE workspace_id = :ws AND stage = 'memory_consolidate' AND status = 'pending'",
        ws=t.ws,
    )
    async with running_worker(api, stages=("memory_consolidate", "memory_embed"), concurrency=2):

        async def quiet() -> bool:
            rows = await sql(
                api,
                t,
                "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND stage IN ('memory_consolidate','memory_embed') AND status IN ('pending','leased') AND available_at <= now()",
            )
            return rows[0].n == 0

        await wait_until(quiet, timeout=timeout, what="the consolidation jobs to finish")


# ---- deletion ----------------------------------------------------------------------------------------------------------------------------------

SENTINEL = "Zebulon Quillfeather"
SENTINEL_EXT = "E-ZQ-4417"


async def request_deletion(
    t: Any, kind: str, target: str | None, who: str = "admin", *, reason: str = "data_subject_request", key: str | None = None, **extra: Any
) -> httpx.Response:
    body: dict[str, Any] = {"kind": kind, "reason_code": reason, **extra}
    if target is not None:
        body["target_id"] = target
    return await t.req("POST", "/v1/deletion-requests", who, json=body, key=key)


async def request_status(api: Any, t: Any, request_id: str) -> dict[str, Any]:
    rows = await sql(
        api, t, "SELECT status, receipt, error_code FROM deletion_requests WHERE workspace_id = :ws AND id = :id", id=uuid.UUID(request_id)
    )
    return {"status": rows[0].status, "receipt": rows[0].receipt, "error_code": rows[0].error_code}


async def run_deletion(
    api: Any,
    t: Any,
    request_id: str,
    *,
    timeout: float = 120.0,  # noqa: ASYNC109
    until: tuple[str, ...] = ("completed", "completed_with_exceptions", "failed", "cancelled"),
) -> dict[str, Any]:
    """Runs the real worker until the request reaches one of the states in `until` (default: any terminal state)."""
    from tests.support.worker import running_worker, wait_until

    async with running_worker(api, stages=("graph_projection", "deletion_plan", "deletion_purge", "deletion_verify"), concurrency=2):

        async def done() -> dict[str, Any] | None:
            s = await request_status(api, t, request_id)
            return s if s["status"] in until else None

        return await wait_until(done, timeout=timeout, what=f"deletion request {request_id} to reach {until}")  # type: ignore[no-any-return]


async def deletion_world(api: Any, t: Any) -> dict[str, str]:
    """A small world to delete from, WITHOUT a worker (so every record is exactly where this builder put it):

    Sarah (kept) and Zebulon Quillfeather (the subject; strong id hr:E-ZQ-4417), KNOWS(Sarah -> Zebulon), two interactions (about Zebulon / about Sarah), and memories:
      m_zeb       about Zebulon only                            -> deleted with him
      m_from_zeb  about Sarah, resting ONLY on the Zebulon interaction -> deleted (privacy over recall)
      m_keep      about Sarah, resting on the other interaction     -> kept
      m_names     about Sarah, a command-asserted fact citing nothing of his, whose TEXT names Zebulon -> a deletion exception
    """
    from tests.support.p5 import accept_event, save

    sarah = (await t.entity("Person", "Sarah Johnson", external_ids=HR("E-1001"), source_system="hr", source_ref="E-1001")).json()[
        "entity_id"
    ]
    zeb = (
        await t.entity("Person", SENTINEL, aliases=["Zeb Q."], external_ids=HR(SENTINEL_EXT), source_system="hr", source_ref=SENTINEL_EXT)
    ).json()["entity_id"]
    acme = await prime_org(t, "Acme", "ORG-1")
    knows = (await t.relationship("KNOWS", sarah, zeb, valid_from="2023-01-01", source_system="hr", source_ref="k1")).json()
    works = (
        await t.relationship(
            "WORKS_AT", sarah, acme, valid_from="2022-01-10", attributes={"role": "Engineer"}, source_system="hr", source_ref="w1"
        )
    ).json()
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", sarah)
    i_zeb = await accept_event(t, {"kind": "message", "content": f"{SENTINEL} said hello to Sarah on Monday."}, subjects=[zeb, sarah])
    i_sar = await accept_event(t, {"kind": "message", "content": "Sarah rode 40 km on Saturday."}, subjects=[sarah])

    async def mem(content: str, ev_interaction: str | None, subj: list[str], **extra: Any) -> str:
        body = {"type": "episodic", "content": content, "subject_ids": subj, "purpose": "history", **extra}
        if ev_interaction:
            body["source_interaction_id"] = ev_interaction
        else:
            body["source_ref"] = f"x-{uuid.uuid4().hex[:6]}"
        r = await save(t, "admin", body)
        assert r.status_code == 201, r.text
        return str(r.json()["memory_id"])

    return {
        "sarah": sarah,
        "zeb": zeb,
        "acme": acme,
        "knows": knows["fact_id"],
        "works": works["fact_id"],
        "i_zeb": i_zeb,
        "i_sar": i_sar,
        "m_zeb": await mem("Zebulon likes green tea in the afternoon.", None, [zeb]),
        "m_from_zeb": await mem("Sarah greeted a colleague on Monday morning.", i_zeb, [sarah]),
        "m_keep": await mem("Sarah rode far on Saturday.", i_sar, [sarah]),
        "m_names": await mem(f"Sarah thanked {SENTINEL} for the introduction.", None, [sarah]),
    }
