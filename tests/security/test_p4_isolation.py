"""SEC-01/04/05 continued for P4: a route walker over every review / graph / evidence route, no content in telemetry, and RLS on every new table."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS
from kg_db.session import system_session, tenant_session
from kg_obs import metrics
from sqlalchemy import text

from tests.support.p4 import CYCLIST, NEWCO, ctx_of, decide, reviews, run_event, sarah_story, sql
from tests.support.tenants import clear_graph, make_tenant

pytestmark = [pytest.mark.integration, pytest.mark.security]
P4_PREFIXES = ("/v1/reviews", "/v1/graph")
P4_ENTITY_SUFFIXES = ("/graph", "/mentions", "/attribute-claims", "/summary")
NO_ID_ROUTES = {("GET", "/v1/reviews"), ("POST", "/v1/graph/search")}
ID_KIND = {"review_id": "review", "interaction_id": "interaction", "entity_id": "entity", "fact_id": "fact"}
WHO = {"decide_review": "reviewer", "read_review_evidence": "reviewer", "get_review": "reviewer"}


def p4_routes() -> list[tuple[str, str, Any]]:
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            new_entity = path.startswith("/v1/entities/{entity_id}/") and path.endswith(P4_ENTITY_SUFFIXES)
            new_fact = path == "/v1/relationships/{fact_id}/evidence"
            if path.startswith("/v1/graph/namespaces"):
                continue  # Flow F routes (P9): their isolation is walked in tests/integration/test_p9_reindex.py::test_a_namespace_can_never_name_another_workspaces_graph
            if path.startswith(P4_PREFIXES) or new_entity or new_fact:
                for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):  # type: ignore[attr-defined]
                    out.append((method, path, route))
    return out


def test_every_p4_route_is_classified():
    unclassified = [(m, p) for m, p, _ in p4_routes() if "{" not in p and (m, p) not in NO_ID_ROUTES]
    assert unclassified == []
    assert len(p4_routes()) >= 10


async def test_foreign_ids_are_indistinguishable_from_random_ids_on_every_p4_route(api, tenant, strict_mock):
    mine = tenant
    ids = await sarah_story(mine)
    detail = await run_event(api, mine, CYCLIST)
    (item,) = await reviews(mine, type="identity_match")
    foreign = {"review": item["id"], "interaction": detail["id"], "entity": ids["sarah"], "fact": ids["em"]}
    other = await make_tenant(api)
    try:
        probed = 0
        for method, path, route in p4_routes():
            params = [p.name for p in route.dependant.path_params]  # type: ignore[attr-defined]
            if not params:
                continue
            urls = []
            for ident in (foreign[ID_KIND[params[0]]], str(uuid.uuid4())):
                url = path.replace("{" + params[0] + "}", ident)
                for extra in params[1:]:
                    url = url.replace(
                        "{" + extra + "}", foreign[ID_KIND[extra]] if ident == foreign[ID_KIND[params[0]]] else str(uuid.uuid4())
                    )
                urls.append(url)
            body = {"decision": "different_entity", "rationale_code": "other"} if method == "POST" else None
            who = WHO.get(route.name, "admin")
            headers = {"if-match": 'W/"rev-1"'} if method == "POST" else None
            responses = [await other.req(method, u, who, json=body, headers=headers) for u in urls]
            f, r = responses
            assert f.status_code == r.status_code == 404, (method, path, f.status_code, f.text)
            strip = lambda x: {k: v for k, v in x.json()["error"].items() if k != "request_id"}  # noqa: E731
            assert strip(f) == strip(r), (method, path)
            assert all(v not in f.text for v in foreign.values())
            probed += 1
        assert probed >= 7, probed
        # the owner resolves the same ids, so the 404s above are about tenancy and not about a broken probe
        assert (await mine.get(f"/v1/reviews/{item['id']}", "reviewer")).status_code == 200
        assert (await mine.get(f"/v1/entities/{ids['sarah']}/graph", "reader")).status_code == 200
        assert (await mine.get(f"/v1/relationships/{ids['em']}/evidence", "reader")).status_code == 200
        # lists and searches show only the caller's workspace
        assert (await other.get("/v1/reviews", "admin")).json()["items"] == []
        assert (await other.req("POST", "/v1/graph/search", "admin", json={"query": "Sarah Johnson Acme"})).json()["facts"] == []
    finally:
        await clear_graph(api, other.group_id)


async def test_p4_routes_deny_by_default_without_credentials(api):
    for method, path, _ in p4_routes():
        url = path
        for name in ("review_id", "interaction_id", "entity_id", "fact_id"):
            url = url.replace("{" + name + "}", str(uuid.uuid4()))
        r = await api.client.request(method, url, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


async def test_extracted_names_and_fact_text_never_reach_logs_metrics_or_audit(api, tenant, strict_mock, capsys):
    """SEC-05: the text a conversation produced is data; it lives in the tables that hold knowledge and nowhere else."""
    from kg_obs.logging import configure_logging

    t = tenant
    configure_logging("INFO", service="test", json_output=True)
    try:
        ids = await sarah_story(t)
        await run_event(api, t, NEWCO, subjects=[ids["sarah"]])
        (item,) = await reviews(t, type="contradiction")
        r = await decide(t, item["id"], "keep_existing", rationale="verified_with_source", text_="SENTINEL-NOTE-9f2c Sarah left")
        assert r.status_code == 200
    finally:
        out = capsys.readouterr().out
        configure_logging("WARNING", service="test", json_output=True)
    sentinels = ["NewCo", "works at NewCo", "SENTINEL-NOTE-9f2c", "Sarah left"]
    for s in sentinels:
        assert s not in out, ("log", s)
    exposition = metrics.render().decode()
    for s in sentinels:
        assert s not in exposition, ("metrics", s)
    audit = " ".join(r.m for r in await sql(api, t, "SELECT metadata::text AS m FROM audit_log WHERE workspace_id = :ws"))
    for s in sentinels:
        assert s not in audit, ("audit", s)
    stage_results = " ".join(
        str(r.result)
        for r in await sql(
            api, t, "SELECT result::text AS result FROM interaction_stages WHERE workspace_id = :ws AND stage = 'graph_ingest'"
        )
    )
    assert "NewCo" not in stage_results  # the stage result holds ids and counts, never names or text
    jobs = " ".join(str(r.payload) for r in await sql(api, t, "SELECT payload::text AS payload FROM outbox_jobs WHERE workspace_id = :ws"))
    assert "NewCo" not in jobs
    calls = " ".join(str(tuple(r)) for r in await sql(api, t, "SELECT * FROM model_calls WHERE workspace_id = :ws"))
    assert "NewCo" not in calls  # the call log is metadata only
    # and the platform's own label sets stay closed: no metric label carries an identifier
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            assert not any(v == str(t.ws) or v in ids.values() for v in sample.labels.values()), sample


NEW_TABLES = ["fact_evidence", "entity_mentions", "identity_non_matches", "attribute_claims", "graph_episodes"]


async def test_rls_isolates_every_new_table_and_fails_closed_without_a_workspace(api, tenant, strict_mock):
    t = tenant
    ids = await sarah_story(t)
    await run_event(api, t, CYCLIST)
    other = await make_tenant(api)
    try:
        for table in [*NEW_TABLES, "review_items"]:
            mine = (await sql(api, t, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :ws"))[0].n  # noqa: S608
            async with tenant_session(api.engine, ctx_of(other)) as c:
                theirs = (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            async with system_session(api.engine) as c:  # no workspace set: nothing is visible
                nothing = (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert theirs == 0 and nothing == 0, table
            if table in ("fact_evidence", "entity_mentions", "review_items", "graph_episodes"):
                assert mine > 0, table
        # a row cannot be written for another workspace even by naming it
        async with tenant_session(api.engine, ctx_of(other)) as c:
            with pytest.raises(Exception, match="row-level security|violates"):
                await c.execute(
                    text("INSERT INTO identity_non_matches (entity_a, entity_b, workspace_id, reason_code) VALUES (:a, :b, :ws, 'x')"),
                    {
                        "a": min(uuid.UUID(ids["sarah"]), uuid.UUID(ids["acme"])),
                        "b": max(uuid.UUID(ids["sarah"]), uuid.UUID(ids["acme"])),
                        "ws": t.ws,
                    },
                )
    finally:
        await clear_graph(api, other.group_id)


async def test_a_review_item_cannot_reference_another_workspaces_rows(api, tenant, strict_mock):
    """The composite foreign keys: (workspace_id, entity_id) must exist in the SAME workspace."""
    a = tenant
    b = await make_tenant(api)
    try:
        ids_a = await sarah_story(a)
        ids_b = await sarah_story(b)
        async with tenant_session(api.engine, ctx_of(a)) as c:
            with pytest.raises(Exception, match="foreign key|violates"):
                await c.execute(
                    text(
                        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, subject_entity_id, candidate_entity_id) "
                        "VALUES (gen_random_uuid(), :ws, 'identity_match', 'x', 'x', :s, :c)"
                    ),
                    {"ws": a.ws, "s": uuid.UUID(ids_a["sarah"]), "c": uuid.UUID(ids_b["sarah"])},
                )
    finally:
        await clear_graph(api, b.group_id)


def test_the_sentinels_are_distinct_json_safe_strings():
    assert json.dumps("SENTINEL-NOTE-9f2c")
