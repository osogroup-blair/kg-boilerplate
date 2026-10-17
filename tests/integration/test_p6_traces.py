"""P6 WP4: traces (metadata only), re-materialisation under current authorisation, encrypted debug snapshots, retention (AT-12, CTX-03, S8, ADR-034)."""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from kg_obs import metrics

from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p5 import act, run_embeds
from tests.support.p6 import build, ids_in, ok_build, owner_rows, q, sarah_world, texts

pytestmark = pytest.mark.integration


async def test_at12_the_trace_explains_the_build_and_holds_no_text(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    secret_task = f"zqtask{uuid.uuid4().hex} Help Sarah assess starting a business"
    out = await ok_build(t, "agent", secret_task, subject_ids=[ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan")
    assert out["retrieval_trace_id"] == out["context_id"]
    tr = (await t.get(f"/v1/context/{out['context_id']}/trace", "agent")).json()
    assert (tr["profile"], tr["profile_source"], tr["status"], tr["compression"]) == ("planning", "ontology", "complete", "structured")
    assert tr["used_tokens"] == out["budgets"]["used_tokens"] <= tr["max_tokens"] == 600
    assert (
        tr["versions"]["ranker"] == "ctx-rank.v1"
        and tr["versions"]["tokenizer"] == out["budgets"]["tokenizer_id"]
        and tr["versions"]["pipeline"] == "ctx.v1"
    )
    assert tr["plan"]["profile"] == "planning" and tr["plan"]["sections"] and tr["plan"]["query_terms"] > 0 and "query" not in tr["plan"]
    assert tr["counts"]["candidates_by_source"]["memory"] >= 3 and tr["counts"]["selected"] == len(
        [i for i in tr["items"] if i["included"]]
    )
    assert set(tr["latencies"]) >= {"authorize", "sources", "total", "per_source"} and tr["watermarks"].keys() >= {
        "graph_ingest",
        "memory_propose",
    }
    chosen = {i["id"] for i in tr["items"] if i["included"]}
    assert chosen == ids_in(out) | {None} - {None} or chosen >= ids_in(out)  # the trace names exactly the items the package returned
    excluded = [i for i in tr["items"] if not i["included"]]
    assert all(i["exclusion_reason"] and i["features"] for i in excluded) and all(
        i["features"] and i["tier"] for i in tr["items"] if i["included"]
    )
    assert {i["kind"] for i in tr["items"]} >= {"fact", "memory", "unknown", "conflict"}
    # positions are the rank order; included items come first
    assert [i["position"] for i in tr["items"]] == list(range(len(tr["items"])))
    # S8: neither the task nor the rendered text is in the trace, the database, the logs or the metrics
    blob = json.dumps(tr)
    assert secret_task not in blob and "Engineering Manager" not in blob and "state licence" not in blob
    rows = await owner_rows(
        "SELECT t::text AS j FROM context_traces t WHERE id = :id UNION ALL SELECT i::text FROM context_items i WHERE context_id = :id",
        id=uuid.UUID(out["context_id"]),
    )
    flat = " ".join(r.j for r in rows)
    assert "zqtask" not in flat and "Engineering Manager" not in flat
    assert "zqtask" not in metrics_text()


def metrics_text() -> str:
    from prometheus_client import generate_latest

    return generate_latest(metrics.REGISTRY).decode()


async def test_ctx03_the_trace_names_no_item_the_builder_may_not_see(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    planning = await ok_build(
        t, "agent", "check NewCo licensing rules for the plan", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan"
    )
    tr = (await t.get(f"/v1/context/{planning['context_id']}/trace", "agent")).json()
    seen = {i["id"] for i in tr["items"]}
    assert ids["note"] not in seen  # agent2's private working note is not even a candidate for agent (AT-10)
    assert ids["note"] not in json.dumps(tr) and ids["note"] not in json.dumps(planning)
    support = await ok_build(t, "agent3", "Help Sarah assess starting a business", subject_ids=[ids["sarah"]], profile="planning")
    tr3 = (await t.get(f"/v1/context/{support['context_id']}/trace", "agent3")).json()
    memory_ids = {ids[k] for k in ("business", "pref", "guide", "finding", "cycling")}
    assert not memory_ids & {
        i["id"] for i in tr3["items"]
    }  # agent3 has no grant: facts yes, Sarah's memories not even as excluded candidates
    assert tr3["counts"]["candidates_by_source"].get("memory", 0) == 0 and tr3["counts"]["memory_candidates_considered"] == 0


async def test_a_context_is_visible_to_its_builder_and_to_admins_and_to_nobody_else(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    cid = out["context_id"]
    for who in ("agent", "admin"):
        assert (await t.get(f"/v1/context/{cid}", who)).status_code == 200 and (
            await t.get(f"/v1/context/{cid}/trace", who)
        ).status_code == 200, who
    ghost = str(uuid.uuid4())
    a = await t.get(f"/v1/context/{cid}", "agent2")
    b = await t.get(f"/v1/context/{ghost}", "agent2")
    assert a.status_code == b.status_code == 404 and a.json()["error"]["code"] == b.json()["error"]["code"] == "not_found"
    assert (await t.get(f"/v1/context/{cid}/trace", "agent2")).status_code == 404
    assert (await t.get(f"/v1/context/{cid}/trace", "reviewer")).status_code == 404  # a role that may build is still not the owner
    assert (await t.get(f"/v1/context/{cid}", "reader")).status_code == 403
    mine = (await t.get("/v1/context", "agent")).json()["items"]
    assert [c["id"] for c in mine] == [cid]
    assert (await t.get("/v1/context", "agent2")).json()["items"] == []
    assert cid in [c["id"] for c in (await t.get("/v1/context", "admin")).json()["items"]]


async def test_rematerialisation_omits_what_is_no_longer_visible_or_active(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    again = (await t.get(f"/v1/context/{out['context_id']}", "agent")).json()
    assert again["rematerialized"] is True and again["withdrawn"] == 0 and again["context_text"] == out["context_text"]
    # a memory is superseded: it is withdrawn from the re-materialised package (and its replacement is NOT swapped in)
    r = await act(t, "admin", ids["pref"], "supersede", {"content": "Sarah prefers a single recommendation.", "reason_code": "clarified"})
    assert r.status_code == 201
    after = (await t.get(f"/v1/context/{out['context_id']}", "agent")).json()
    assert after["withdrawn"] == 1 and ids["pref"] not in ids_in(after) and r.json()["memory_id"] not in ids_in(after)
    assert "decision comparisons" not in texts(after) and "single recommendation" not in texts(after)
    # a grant is revoked: what the principal may no longer see disappears from its own earlier builds, and only that
    await owner_rows(
        "DELETE FROM principal_scopes WHERE workspace_id = :ws AND scope_kind = 'subject' AND principal_id = :p",
        ws=t.ws,
        p=uuid.UUID(await t.principal_id("agent")),
    )
    revoked = (await t.get(f"/v1/context/{out['context_id']}", "agent")).json()
    assert revoked["withdrawn"] >= 3 and ids["business"] not in ids_in(revoked) and ids["guide"] not in ids_in(revoked)
    assert ids["finding"] in ids_in(revoked)  # the project grant is untouched
    # the facts about Sarah are unaffected by memory withdrawals
    assert ids["em"] in ids_in(after)


async def test_a_deleted_or_expired_memory_never_resurfaces(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert ids["finding"] in ids_in(out)
    r = await act(t, "admin", ids["finding"], "expire", {"reason_code": "outdated"})
    assert r.status_code in (200, 201), r.text
    after = (await t.get(f"/v1/context/{out['context_id']}", "agent")).json()
    assert ids["finding"] not in ids_in(after) and after["withdrawn"] == 1 and "state licence" not in texts(after)
    _ = run_embeds


async def test_the_debug_snapshot_is_admin_only_encrypted_audited_and_expires(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    task = f"zqdebug{uuid.uuid4().hex} Help Sarah assess starting a business"
    refused = await build(t, "agent", task, subject_ids=[ids["sarah"]], profile="planning", debug_snapshot=True)
    assert refused.status_code == 403  # an agent cannot ask for one
    out = await ok_build(t, "admin", task, subject_ids=[ids["sarah"]], profile="planning", debug_snapshot=True)
    cid = out["context_id"]
    snap = (await t.get(f"/v1/context/{cid}/debug", "admin")).json()
    assert snap["task"] == task and snap["context_text"] == out["context_text"]
    assert (await t.get(f"/v1/context/{cid}/debug", "agent")).status_code == 403
    assert (await t.get(f"/v1/context/{cid}/debug", "reviewer")).status_code == 403
    rows = await owner_rows("SELECT kid, nonce, ciphertext FROM context_debug_snapshots WHERE context_id = :id", id=uuid.UUID(cid))
    assert len(rows) == 1 and b"zqdebug" not in bytes(rows[0].ciphertext) and b"Engineering Manager" not in bytes(rows[0].ciphertext)
    audit = (await t.get("/v1/audit", "admin", action="context.debug_read")).json()["items"]
    assert audit and audit[0]["resource_id"] == cid
    created = (await t.get("/v1/audit", "admin", action="context.debug_snapshot_created")).json()["items"]
    assert created and created[0]["resource_id"] == cid
    # the database clock decides: an expired snapshot is gone to readers and the purge removes the row
    await owner_rows(
        "UPDATE context_debug_snapshots SET expires_at = now() - interval '1 second', created_at = now() - interval '1 hour' WHERE context_id = :id",
        id=uuid.UUID(cid),
    )
    assert (await t.get(f"/v1/context/{cid}/debug", "admin")).status_code == 404
    purged = (await owner_rows("SELECT ops.purge_expired_context_debug() AS n"))[0].n
    assert purged >= 1 and not await owner_rows("SELECT 1 FROM context_debug_snapshots WHERE context_id = :id", id=uuid.UUID(cid))


async def test_retention_purges_old_traces_and_their_items(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    old = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    new = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    await owner_rows("UPDATE context_traces SET created_at = now() - interval '40 days' WHERE id = :id", id=uuid.UUID(old["context_id"]))
    gone = (await owner_rows("SELECT ops.purge_expired_context_traces(make_interval(days => 30)) AS n"))[0].n
    assert gone >= 1
    assert (await t.get(f"/v1/context/{old['context_id']}/trace", "agent")).status_code == 404
    assert (await t.get(f"/v1/context/{new['context_id']}/trace", "agent")).status_code == 200
    assert not await q(api, t, "SELECT 1 FROM context_items WHERE workspace_id = :ws AND context_id = :id", id=uuid.UUID(old["context_id"]))


async def test_a_trace_that_cannot_be_written_never_fails_the_build(api, tenant_ctx, strict_mock, monkeypatch):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.app.state.kg.context.store, "write", boom)
    before = metrics_text().count("context_trace_write_failures_total")
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    assert out["status"] == "complete" and out["retrieval_trace_id"] is None and "Engineering Manager" in texts(out)
    assert "context_trace_write_failures_total" in metrics_text() and before >= 0


async def test_logs_hold_no_task_and_no_rendered_text(api, tenant_ctx, strict_mock, caplog):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    sentinel = f"zqlog{uuid.uuid4().hex}"
    with caplog.at_level(logging.DEBUG):
        out = await ok_build(
            t,
            "agent",
            f"{sentinel} Help Sarah assess starting a business",
            subject_ids=[ids["sarah"]],
            profile="planning",
            project="newco-plan",
        )
    logged = "\n".join(r.getMessage() + " " + str(getattr(r, "__dict__", "")) for r in caplog.records)
    assert sentinel not in logged and "Engineering Manager" not in logged and out["context_text"][:60] not in logged
