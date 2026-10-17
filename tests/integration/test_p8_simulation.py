"""P8 WP8 (ONT-05, AT-17): simulating a draft ontology on a sample of real interactions, in a staging graph namespace that leaves nothing behind, and the version pinning that
publishing it then gives (new work pins v1.5.0, old provenance stays on the version it was made under, `--repin` moves one interaction)."""

from __future__ import annotations

import json
import uuid

import pytest
import yaml

from tests.support.p4 import add_mock_rules, ctx_of, ingest, run_event, sql, stage, strict_mock, wait_terminal  # noqa: F401
from tests.support.p8 import owner_exec, raw
from tests.support.tenants import AGENTS_YAML, GOVERNANCE_YAML, clear_graph, make_tenant
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration
QUIET_AGENTS = AGENTS_YAML.replace(
    "    extraction: enabled ", "    extraction: disabled", 1
)  # (no memory-extraction model call in these scenarios)
assert QUIET_AGENTS != AGENTS_YAML
TEXT = "Sarah Johnson: Michael reports to me, I manage Michael."


async def manages_rules(text: str) -> None:
    await add_mock_rules(
        [
            {
                "when": {"schema_name": "ExtractedEntities", "contains": [text]},
                "response": {
                    "extracted_entities": [
                        {"name": "Sarah Johnson", "entity_type_id": 2, "episode_indices": [0]},
                        {"name": "Michael", "entity_type_id": 2, "episode_indices": [0]},
                    ]
                },
            },
            {
                "when": {"schema_name": "ExtractedEdges", "contains": [text]},
                "response": {
                    "edges": [
                        {
                            "source_entity_name": "Sarah Johnson",
                            "target_entity_name": "Michael",
                            "relation_type": "MANAGES",
                            "fact": "Sarah Johnson manages Michael",
                            "valid_at": "2026-01-01T00:00:00Z",
                            "invalid_at": None,
                            "episode_indices": [0],
                        }
                    ]
                },
            },
            {
                "when": {"schema_name": "EdgeDuplicate", "contains": ["<new fact> Sarah Johnson manages Michael"]},
                "response": {"duplicate_facts": [], "contradicted_facts": []},
            },
            {
                "when": {"schema_name": "EdgeTimestamps", "contains": ["<fact> Sarah Johnson manages Michael"]},
                "response": {"valid_at": "2026-01-01T00:00:00Z", "invalid_at": None},
            },
            {"when": {"schema_name": "MANAGES", "contains": ["Sarah Johnson manages Michael"]}, "response": {}},
        ]
    )


async def draft_v15(t) -> dict:  # type: ignore[no-untyped-def]
    d = yaml.safe_load(GOVERNANCE_YAML)
    r = await t.req("POST", "/v1/ontologies/versions", "admin", json={"content": json.loads(json.dumps(d, default=str))})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def staging_nodes(api, t) -> int:  # type: ignore[no-untyped-def]
    driver = api.app.state.kg.graph._graphiti.driver
    records, _, _ = await driver.client.execute_query(
        "MATCH (n) WHERE n.group_id STARTS WITH $p RETURN count(n) AS n", parameters_={"p": f"{t.group_id}-sim-"}, database_="neo4j"
    )
    return int(records[0]["n"])


async def world(api):  # type: ignore[no-untyped-def]
    """A workspace on v1.4.0 with one processed interaction (the sample) and a v1.5.0 draft that adds MANAGES."""
    t = await make_tenant(api, ontology=QUIET_AGENTS)
    await manages_rules(TEXT)
    detail = await run_event(api, t, {"kind": "message", "content": TEXT})
    assert detail["processing_status"] == "indexed", [
        (x["stage"], x["status"], x.get("error_code"), x.get("result")) for x in detail["stages"]
    ]
    return t, detail["id"], await draft_v15(t)


async def counts(api, t) -> dict:  # type: ignore[no-untyped-def]
    r = await raw(
        api,
        t,
        "SELECT (SELECT count(*) FROM entities WHERE workspace_id = :ws) AS e, (SELECT count(*) FROM facts WHERE workspace_id = :ws) AS f, "
        "(SELECT count(*) FROM interactions WHERE workspace_id = :ws) AS i, (SELECT count(*) FROM graph_episodes WHERE workspace_id = :ws) AS g, "
        "(SELECT count(*) FROM entity_mentions WHERE workspace_id = :ws) AS m, (SELECT count(*) FROM fact_evidence WHERE workspace_id = :ws) AS ev",
    )
    return dict(r[0]._mapping)


async def test_a_preview_costs_nothing_and_a_confirmed_run_reports_and_leaves_nothing_behind(api, strict_mock):  # noqa: F811
    t, iid, draft = await world(api)
    try:
        before = await counts(api, t)
        calls_before = (await raw(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws"))[0].n
        # an agent cannot simulate; an admin previews
        assert (await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "agent", json={})).status_code == 403
        pre = await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": [iid]})
        assert pre.status_code in (200, 202), pre.text
        p = pre.json()
        assert p["status"] == "previewed" and p["runnable_count"] == 1 and p["cost_estimate"] is not None and p["estimate"]["calls"] > 0
        assert (await raw(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws"))[
            0
        ].n == calls_before  # a preview calls no model
        assert (await raw(api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'ontology_simulation'"))[
            0
        ].n == 0
        # the budget is enforced
        low = await t.req(
            "POST",
            f"/v1/ontologies/versions/{draft['id']}/simulate",
            "admin",
            json={"simulation_id": p["id"], "confirm": True, "budget_usd": 0.0000001},
        )
        assert low.status_code == 409 and low.json()["error"]["code"] == "simulation_refused", low.text
        async with running_worker(api, stages=("graph_projection", "ontology_simulation", "simulation_cleanup"), concurrency=2):
            go = await t.req(
                "POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"simulation_id": p["id"], "confirm": True}
            )
            assert go.status_code in (200, 202) and go.json()["status"] == "running", go.text
            assert go.json()["id"] == p["id"]
            again = await t.req(
                "POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"simulation_id": p["id"], "confirm": True}
            )
            assert again.status_code == 409  # started once

            async def done():  # type: ignore[no-untyped-def]
                got = (await t.get(f"/v1/ontologies/simulations/{p['id']}", "admin")).json()
                return got if got["status"] in ("completed", "failed") else None

            out = await wait_until(done, timeout=180, what="the simulation to finish")
        assert out["status"] == "completed", out
        rep = out["report"]
        assert rep["episodes"] == 1 and rep["facts_by_type"].get("MANAGES") == 1, rep
        assert rep["new_relationship_types"] == ["MANAGES"] and rep["diff"]["changes"] >= 1
        assert out["cost_actual"] is not None
        # nothing of it is left: no staging group, no row of the workspace's knowledge, the real group unchanged
        assert await staging_nodes(api, t) == 0
        assert await counts(api, t) == before
        row = (await raw(api, t, "SELECT cleaned_at, staging_group FROM ontology_simulations WHERE id = :i", i=uuid.UUID(p["id"])))[0]
        assert row.cleaned_at is not None and row.staging_group.startswith(f"ws-{t.ws}-sim-")
        audit = {a.action for a in await raw(api, t, "SELECT action FROM audit_log WHERE workspace_id = :ws")}
        assert {"ontology.simulated", "ontology.simulation_completed"} <= audit
    finally:
        await clear_graph(api, t.group_id)


async def test_only_a_draft_can_be_simulated_a_foreign_id_is_a_404_and_a_local_only_sample_is_never_sent(api, strict_mock):  # noqa: F811
    t, iid, draft = await world(api)
    other = await make_tenant(api, ontology=QUIET_AGENTS)
    try:
        pre = (await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": [iid]})).json()
        gone = await other.get(f"/v1/ontologies/simulations/{pre['id']}", "admin")
        missing = await other.get(f"/v1/ontologies/simulations/{uuid.uuid4()}", "admin")
        assert gone.status_code == missing.status_code == 404
        assert {k: v for k, v in gone.json()["error"].items() if k != "request_id"} == {
            k: v for k, v in missing.json()["error"].items() if k != "request_id"
        }
        # another workspace's interaction id is not a sample either
        assert (
            await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": [str(uuid.uuid4())]})
        ).status_code == 404
        # a published version is already in use: nothing to simulate
        active = (await t.get("/v1/ontologies/active")).json()["version"]["id"]
        assert (
            await t.req("POST", f"/v1/ontologies/versions/{active}/simulate", "admin", json={"interaction_ids": [iid]})
        ).status_code == 409
        # consent withheld never leaves: every sample skipped by policy means nothing could run
        secret = await ingest(t, {"kind": "message", "content": "Sarah Johnson: a private note"}, consent={"external_processing": "denied"})
        await raw(api, t, "SELECT 1")  # (keeps the connection pattern of the suite)
        await owner_exec("UPDATE interactions SET processing_status = 'indexed' WHERE id = :i", i=uuid.UUID(secret))
        only = await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": [secret]})
        assert only.status_code in (200, 202) and only.json()["runnable_count"] == 0 and only.json()["skipped"] == {"policy": 1}, only.text
        refused = await t.req(
            "POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"simulation_id": only.json()["id"], "confirm": True}
        )
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "simulation_refused"
    finally:
        await clear_graph(api, t.group_id)
        await clear_graph(api, other.group_id)


async def test_a_crashed_run_is_cleaned_by_the_delayed_job(api, strict_mock):  # noqa: F811
    t, iid, draft = await world(api)
    try:
        pre = (await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": [iid]})).json()
        go = await t.req(
            "POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"simulation_id": pre["id"], "confirm": True}
        )
        assert go.status_code in (200, 202)
        sim = uuid.UUID(pre["id"])
        # the run never happened (the worker died); something was left in the staging group meanwhile
        staging = api.app.state.kg.graph.staging(ctx_of(t), sim.hex[:12])
        await staging.run_cypher("CREATE (:Entity {uuid: $u, group_id: $group_id, name: 'leftover'})", u=str(uuid.uuid4()))
        assert await staging_nodes(api, t) == 1
        service = api.app.state.kg.simulation
        assert await service.cleanup(ctx_of(t), sim) is True
        assert await staging_nodes(api, t) == 0
        row = (await raw(api, t, "SELECT status, cleaned_at FROM ontology_simulations WHERE id = :i", i=sim))[0]
        assert row.status == "failed" and row.cleaned_at is not None
        assert await service.cleanup(ctx_of(t), sim) is False  # idempotent
    finally:
        await clear_graph(api, t.group_id)


async def test_at17_publishing_v15_pins_new_work_to_it_old_provenance_stays_and_repin_moves_one_interaction(api, strict_mock):  # noqa: F811
    t, old_iid, draft = await world(api)
    try:
        v14 = (await t.get("/v1/ontologies/active")).json()["version"]["id"]
        assert (
            await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/publish", "admin", json={"notes": "adds MANAGES"})
        ).status_code == 200
        assert (await t.get("/v1/ontologies/active")).json()["version"]["id"] == draft["id"]
        new_text = "Sarah Johnson: Michael works for me now, I manage Michael."
        await manages_rules(new_text)
        new = await run_event(api, t, {"kind": "message", "content": new_text})
        pins = {
            r.id: str(r.ontology_version_id)
            for r in await sql(api, t, "SELECT id::text AS id, ontology_version_id FROM interactions WHERE workspace_id = :ws")
        }
        assert pins[old_iid] == v14 and pins[new["id"]] == draft["id"]
        # the old interaction's derived records keep the version they were made under; the new ones carry v1.5.0
        old_cmds = await sql(
            api,
            t,
            "SELECT DISTINCT ontology_version_id::text AS v FROM commands WHERE workspace_id = :ws AND created_at < (SELECT recorded_at FROM interactions WHERE id = :i)",
            i=uuid.UUID(new["id"]),
        )
        assert {c.v for c in old_cmds} <= {v14}
        stages = {
            (r.iid, r.stage): str(r.ontology_version_id)
            for r in await sql(
                api, t, "SELECT interaction_id::text AS iid, stage, ontology_version_id FROM interaction_stages WHERE workspace_id = :ws"
            )
        }
        assert stages[(old_iid, "graph_ingest")] == v14 and stages[(new["id"], "graph_ingest")] == draft["id"]
        # reads mix both versions correctly
        assert (await t.get(f"/v1/interactions/{old_iid}", "admin")).status_code == 200
        assert (await t.get(f"/v1/interactions/{new['id']}", "admin")).status_code == 200
        # a replay WITHOUT repin keeps the pin; WITH repin it moves to the active version (audited)
        async with running_worker(api, concurrency=2):
            plain = await t.req(
                "POST", f"/v1/interactions/{old_iid}/replay", "operator", json={"stage": "graph_ingest", "reason_code": "operator_request"}
            )
            assert plain.status_code == 202, plain.text
            await wait_until(lambda: _settled(api, t, old_iid), timeout=120, what="the plain replay")
        assert (
            str(
                (
                    await sql(
                        api,
                        t,
                        "SELECT ontology_version_id FROM interaction_stages WHERE interaction_id = :i AND stage = 'graph_ingest'",
                        i=uuid.UUID(old_iid),
                    )
                )[0].ontology_version_id
            )
            == v14
        )
        async with running_worker(api, concurrency=2):
            moved = await t.req(
                "POST",
                f"/v1/interactions/{old_iid}/replay",
                "operator",
                json={"stage": "graph_ingest", "reason_code": "operator_request", "repin": True},
            )
            assert moved.status_code == 202, moved.text
            await wait_until(lambda: _settled(api, t, old_iid), timeout=120, what="the repinned replay")
        assert (
            str(
                (
                    await sql(
                        api,
                        t,
                        "SELECT ontology_version_id FROM interaction_stages WHERE interaction_id = :i AND stage = 'graph_ingest'",
                        i=uuid.UUID(old_iid),
                    )
                )[0].ontology_version_id
            )
            == draft["id"]
        )
        assert len(await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'interaction.repinned'")) == 1
        assert pins[new["id"]] == draft["id"] and stage(new, "graph_ingest")["status"] == "succeeded"
    finally:
        await clear_graph(api, t.group_id)


async def _settled(api, t, iid):  # type: ignore[no-untyped-def]
    rows = await sql(api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased')")
    d = (await t.get(f"/v1/interactions/{iid}", "admin")).json()
    return d if rows[0].n == 0 and d["processing_status"] in ("indexed", "partial") else None


_ = wait_terminal
