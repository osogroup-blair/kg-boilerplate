"""P5 WP6: `kg memories …` and `kg scopes …`, run as real subprocesses against kg_test. A CLI call is authorised exactly like an API call: the named actor's roles and grants decide."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.integration.test_p3_cli import kg
from tests.support.p4 import sarah_story
from tests.support.p5 import body, get, run_embeds, save

pytestmark = pytest.mark.integration


async def test_scopes_grant_list_revoke_are_audited_and_admin_only(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    ws = t.slug
    base = ["--workspace", ws]
    granted = await kg(
        "scopes", "grant", *base, "--principal", f"agent3@{ws}", "--kind", "subject", "--ref", ids["sarah"], "--actor", f"admin@{ws}"
    )
    assert granted.returncode == 0 and "granted" in granted.stdout, granted.stderr
    again = await kg(
        "scopes", "grant", *base, "--principal", f"agent3@{ws}", "--kind", "subject", "--ref", ids["sarah"], "--actor", f"admin@{ws}"
    )
    assert "already granted" in again.stdout
    listed = await kg("scopes", "list", *base)
    assert f"agent3@{ws}\tsubject\t{ids['sarah']}" in listed.stdout
    assert f"agent@{ws}\tproject\tnewco-plan" in listed.stdout
    denied = await kg(
        "scopes", "grant", *base, "--principal", f"agent3@{ws}", "--kind", "subject", "--ref", "*", "--actor", f"reviewer@{ws}"
    )
    assert denied.returncode != 0 and "may not manage scope grants" in denied.stderr
    bad = await kg("scopes", "grant", *base, "--principal", f"agent3@{ws}", "--kind", "galaxy", "--ref", "x", "--actor", f"admin@{ws}")
    assert bad.returncode != 0
    # the grant is effective at once (it is read per request) and revocable
    mid = (await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"]
    assert (await get(t, "agent3", mid)).status_code == 200
    revoked = await kg(
        "scopes", "revoke", *base, "--principal", f"agent3@{ws}", "--kind", "subject", "--ref", ids["sarah"], "--actor", f"admin@{ws}"
    )
    assert revoked.returncode == 0 and "revoked" in revoked.stdout
    assert (await get(t, "agent3", mid)).status_code == 404
    audit = {a["action"] for a in (await t.get("/v1/audit", "admin", limit=100)).json()["items"]}
    assert {"scope.granted", "scope.revoked"} <= audit


async def test_memories_list_show_search_supersede_expire_follow_the_actors_grants(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    ws = t.slug
    mid = (
        await save(
            t,
            "admin",
            body(content="Sarah plans the offsite agenda in the autumn.", subject_ids=[ids["sarah"]], source_ref="x", purpose="p"),
        )
    ).json()["memory_id"]
    await run_embeds(api, t)
    base = ["--workspace", ws]
    listed = await kg("memories", "list", *base, "--actor", f"reviewer@{ws}")
    assert listed.returncode == 0, listed.stderr
    assert [json.loads(line)["id"] for line in listed.stdout.splitlines()] == [mid]
    nothing = await kg("memories", "list", *base, "--actor", f"agent3@{ws}")  # no grant, no memory, from the CLI too
    assert nothing.returncode == 0 and nothing.stdout.strip() == ""
    shown = await kg("memories", "show", mid, *base, "--actor", f"reviewer@{ws}")
    assert json.loads(shown.stdout)["content"].startswith("Sarah plans the offsite")
    hidden = await kg("memories", "show", mid, *base, "--actor", f"agent3@{ws}")
    assert hidden.returncode != 0 and "not_found" in hidden.stderr
    found = await kg("memories", "search", "offsite agenda", *base, "--actor", f"reviewer@{ws}")
    out = json.loads(found.stdout)
    assert [h["memory_id"] for h in out["hits"]] == [mid] and out["degraded"] == []
    none = await kg("memories", "search", "offsite agenda", *base, "--actor", f"agent3@{ws}")
    assert json.loads(none.stdout)["candidates_considered"] == 0
    refused = await kg("memories", "supersede", mid, *base, "--actor", f"app@{ws}", "--content", "x y z")
    assert refused.returncode != 0 and "forbidden" in refused.stderr  # an app saves; it does not curate
    sup = await kg(
        "memories",
        "supersede",
        mid,
        *base,
        "--actor",
        f"reviewer@{ws}",
        "--content",
        "Sarah plans the offsite agenda in the spring.",
        "--reason",
        "corrected",
    )
    assert sup.returncode == 0, sup.stderr
    new_id = json.loads(sup.stdout)["memory_id"]
    assert (await get(t, "admin", mid)).json()["status"] == "superseded"
    exp = await kg("memories", "expire", new_id, *base, "--actor", f"reviewer@{ws}", "--reason", "stale")
    assert exp.returncode == 0 and json.loads(exp.stdout)["status"] == "expired"


async def test_reembed_writes_a_new_version_while_reads_stay_pinned_then_the_switch_and_the_drop(api, tenant_mem):
    from kg_db.session import tenant_session
    from kg_domain.enums import PrincipalKind, Role
    from kg_domain.principal import Principal
    from kg_memory.models import SearchIn
    from kg_memory.reads import Viewer
    from kg_memory.reembed import reembed_workspace
    from kg_memory.scopes import ScopeGrants
    from kg_memory.search import MemorySearch

    from tests.support.p4 import ctx_of, sql

    t = tenant_mem
    ids = await sarah_story(t)
    mid = uuid.UUID(
        (
            await save(
                t,
                "admin",
                body(content="Sarah plans the offsite agenda in the autumn.", subject_ids=[ids["sarah"]], source_ref="x", purpose="p"),
            )
        ).json()["memory_id"]
    )
    await run_embeds(api, t)
    st = api.app.state.kg
    report = await reembed_workspace(api.engine, st.router.gateway, st.router.config, st.settings, t.ws, to_version=2)
    assert (report.embedded, report.remaining, report.coverage) == (1, 0, 1.0)
    again = await reembed_workspace(api.engine, st.router.gateway, st.router.config, st.settings, t.ws, to_version=2)
    assert again.embedded == 0 and again.coverage == 1.0  # resumable: nothing left to do
    assert sorted(
        r.embedding_version for r in await sql(api, t, "SELECT embedding_version FROM memory_embeddings WHERE memory_id = :m", m=mid)
    ) == [1, 2]
    p = Principal(
        principal_id=uuid.UUID(await t.principal_id("admin")),
        workspace_id=t.ws,
        kind=PrincipalKind.USER,
        handle="a",
        roles=frozenset({Role.WORKSPACE_ADMIN}),
    )
    async with tenant_session(api.engine, ctx_of(t)) as c:
        v = Viewer(p, await ScopeGrants.for_principal(c, p), curator=True)
        pinned_v1 = await st.memory.search.run(c, v, SearchIn(query="offsite agenda"))
        v2_settings = st.settings.model_copy(update={"memory_embedding_version": 2})
        pinned_v2 = await MemorySearch(st.router.gateway, st.router.config, v2_settings, service="kg-api").run(
            c, v, SearchIn(query="offsite agenda")
        )
    assert (pinned_v1.embedding_version, pinned_v2.embedding_version) == (1, 2)
    assert [h.memory_id for h in pinned_v1.hits] == [h.memory_id for h in pinned_v2.hits] == [mid] and pinned_v2.embedding_coverage == 1.0
    dropped = await reembed_workspace(api.engine, st.router.gateway, st.router.config, st.settings, t.ws, to_version=2, drop_version=1)
    assert dropped.dropped == 1
    assert [
        r.embedding_version for r in await sql(api, t, "SELECT embedding_version FROM memory_embeddings WHERE memory_id = :m", m=mid)
    ] == [2]
