"""P8 WP5 (AT-16, SEC-05, MEM-07): subject and interaction deletion over the real stack. The freeze is checked BEFORE any job runs (the immediate read block), the purge by
what is left in every table afterwards, and the receipt, journal, tombstones and suppression by their own rows."""

from __future__ import annotations

import uuid

import pytest

from tests.support.p4 import sql
from tests.support.p5 import search
from tests.support.p8 import (
    SENTINEL,
    SENTINEL_EXT,
    deletion_world,
    raw,
    request_deletion,
    request_status,
    reviews_of,
    run_deletion,
)

pytestmark = pytest.mark.integration


async def test_the_freeze_hides_the_subject_everywhere_before_any_job_has_run(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    r = await request_deletion(t, "subject", w["zeb"], "app")  # the trusted-source app attests an end subject's request
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "frozen" and out["kind"] == "subject" and out["status_url"] == f"/v1/deletion-requests/{out['request_id']}"
    assert out["item_counts"]["postgres.delete.pending"] >= 4  # entity, fact, memory, interaction ... listed at once
    # no worker has run: Postgres still holds the rows, but every surface answers as if they were never there (SEC-05)
    for who in ("admin", "reviewer", "app"):
        assert (await t.get(f"/v1/entities/{w['zeb']}", who)).status_code == 404, who
        assert (await t.get(f"/v1/relationships/{w['knows']}", who)).status_code == 404, who
        assert (await t.get(f"/v1/interactions/{w['i_zeb']}", who)).status_code == 404, who
    for who in ("admin", "reviewer"):
        assert (await t.get(f"/v1/memories/{w['m_zeb']}", who)).status_code == 404 and (
            await t.get(f"/v1/memories/{w['m_from_zeb']}", who)
        ).status_code == 404
    sarah = (await t.get(f"/v1/entities/{w['sarah']}", "admin")).json()
    assert [x["rel_type"] for x in sarah["relationships"]] == ["WORKS_AT"]  # the KNOWS fact is gone from her page, history included
    hist = (await t.get(f"/v1/entities/{w['sarah']}", "admin", include="history")).json()["relationships"]
    assert [x["rel_type"] for x in hist] == ["WORKS_AT"]
    names = [e["name"] for e in (await t.get("/v1/entities", "admin", q="zebulon")).json()["items"]]
    assert names == []  # not in the directory either
    hits = [h["memory_id"] for h in (await search(t, "admin", "Zebulon green tea"))["hits"]]
    assert w["m_zeb"] not in hits and w["m_from_zeb"] not in hits
    assert w["m_keep"] in [
        h["memory_id"] for h in (await search(t, "admin", "Sarah rode far Saturday"))["hits"]
    ]  # what does not depend on him is untouched
    # the freeze is a status in Postgres, in one transaction
    rows = await raw(api, t, "SELECT status FROM entities WHERE id = :e", e=uuid.UUID(w["zeb"]))
    assert [r.status for r in rows] == ["deletion_pending"]
    # a re-creation attempt with the same strong identifier is refused from the moment of the request
    again = await t.entity(
        "Person", "Someone Else", external_ids=[{"system": "hr", "value": SENTINEL_EXT}], source_system="hr", source_ref="z"
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "subject_suppressed"
    # and so is an interaction that names him by that identifier
    ref = await t.req(
        "POST",
        "/v1/interactions",
        "app",
        json={
            "source_system": "chat",
            "source_ref": "again",
            "kind": "message",
            "content": "hi",
            "occurred_at": "2026-10-10T09:00:00Z",
            "subject_refs": [{"type": "Person", "system": "hr", "value": SENTINEL_EXT}],
        },
    )
    assert ref.status_code == 409 and ref.json()["error"]["code"] == "subject_suppressed"


async def test_who_may_ask_and_a_hidden_target_is_the_same_404(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    for who in ("agent", "reader", "operator"):
        assert (await request_deletion(t, "subject", w["zeb"], who)).status_code == 403, who
    assert (await request_deletion(t, "subject", w["zeb"], "reviewer")).status_code == 403  # a reviewer asks for MEMORIES only
    assert (await request_deletion(t, "memory", w["m_zeb"], "reviewer")).status_code == 202
    assert (await request_deletion(t, "subject", str(uuid.uuid4()), "admin")).status_code == 404
    # an app deletes only the interactions it submitted
    other = await t.req(
        "POST",
        "/v1/interactions",
        "admin",
        json={
            "source_system": "chat",
            "source_ref": "adm",
            "kind": "message",
            "content": "admin note",
            "occurred_at": "2026-10-10T09:00:00Z",
        },
    )
    assert (await request_deletion(t, "interaction", other.json()["event_id"], "app")).status_code == 404
    assert (await request_deletion(t, "interaction", w["i_sar"], "app")).status_code == 202
    # workspace and principal_data are admin-only; a workspace needs its slug typed
    assert (await request_deletion(t, "workspace", None, "app", confirm_slug=t.slug)).status_code == 403
    assert (await request_deletion(t, "workspace", None, "admin", confirm_slug="not-the-slug")).status_code == 403


async def test_the_request_is_idempotent(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    first = await request_deletion(t, "subject", w["zeb"], "admin", key="del-1")
    again = await request_deletion(t, "subject", w["zeb"], "admin", key="del-1")
    assert first.status_code == again.status_code == 202 and again.headers["idempotent-replay"] == "true"
    assert first.json()["request_id"] == again.json()["request_id"]
    other = await request_deletion(t, "interaction", w["i_sar"], "admin", key="del-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_mismatch"
    assert len(await sql(api, t, "SELECT 1 FROM deletion_requests WHERE workspace_id = :ws")) == 1


async def test_the_purge_removes_every_derivative_verifies_and_issues_a_receipt(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "subject", w["zeb"], "admin")).json()["request_id"]
    final = await run_deletion(api, t, rid)
    # the free-text memory that names him is an exception for a person; everything else is done
    assert final["status"] == "completed_with_exceptions", final
    (exc,) = await reviews_of(api, t, "deletion_exception")
    assert exc.reason_code == "shared_memory_mentions_subject" and exc.detail["memory_id"] == w["m_names"]
    ids = {k: uuid.UUID(v) for k, v in w.items()}
    gone = await raw(
        api,
        t,
        "SELECT (SELECT count(*) FROM entities WHERE id = :z) AS e, (SELECT count(*) FROM facts WHERE id = :k) AS f, (SELECT count(*) FROM interactions WHERE id = :i) AS i, "
        "(SELECT count(*) FROM curated_memories WHERE id = ANY(:m)) AS m, (SELECT count(*) FROM entity_external_ids WHERE value = :x) AS x, "
        "(SELECT count(*) FROM interaction_contents WHERE interaction_id = :i) AS c, (SELECT count(*) FROM memory_embeddings WHERE memory_id = ANY(:m)) AS v",
        z=ids["zeb"],
        k=ids["knows"],
        i=ids["i_zeb"],
        m=[ids["m_zeb"], ids["m_from_zeb"]],
        x=SENTINEL_EXT,
    )
    assert tuple(gone[0]) == (0, 0, 0, 0, 0, 0, 0)
    kept = await raw(
        api,
        t,
        "SELECT (SELECT count(*) FROM entities WHERE id = :s) AS s, (SELECT count(*) FROM facts WHERE id = :w) AS w, (SELECT count(*) FROM interactions WHERE id = :i) AS i, (SELECT count(*) FROM curated_memories WHERE id = :m) AS m",
        s=ids["sarah"],
        w=ids["works"],
        i=ids["i_sar"],
        m=ids["m_keep"],
    )
    assert tuple(kept[0]) == (1, 1, 1, 1)
    # the ledger keeps its rows, with the payload that carried his name redacted
    cmds = await raw(api, t, "SELECT payload FROM commands WHERE workspace_id = :ws AND command_type = 'entity.create'")
    assert not any(SENTINEL in str(c.payload) for c in cmds) and any(c.payload.get("redacted") for c in cmds)
    # tombstones and the suppression survive; the manifest is not removed while an exception is open (the verifier needs it for a re-run)
    tomb = await raw(api, t, "SELECT kind, id::text AS id FROM deletion_tombstones WHERE workspace_id = :ws")
    assert {(x.kind, x.id) for x in tomb} >= {
        ("entity", w["zeb"]),
        ("fact", w["knows"]),
        ("interaction", w["i_zeb"]),
        ("memory", w["m_zeb"]),
    }
    assert len(await raw(api, t, "SELECT 1 FROM deletion_suppressions WHERE workspace_id = :ws AND lifted_at IS NULL")) == 1
    receipt = final["receipt"]
    assert (
        receipt["status"] == "completed_with_exceptions"
        and receipt["stores"]["postgres"]["deleted"] > 5
        and receipt["backup_copies_expire_by"]
    )
    assert receipt["verification"]["id_residuals"] == 0
    # a person decides the exception: redact the text, and the original goes
    detail = await t.get(f"/v1/reviews/{exc.id}", "admin")
    assert detail.status_code == 200 and detail.json()["allowed_decisions"] == [
        "redact_text",
        "delete_item",
        "retain_with_reason",
        "retry_verification",
    ]
    body = detail.json()
    assert body["deletion_request_id"] == rid and body["detail"]["memory_id"] == w["m_names"] and SENTINEL not in str(body)
    from tests.support.p4 import decide

    r = await decide(t, str(exc.id), "redact_text", who="admin", text_="Thanks were exchanged.", rationale="privacy")
    assert r.status_code == 422  # the replacement text goes in edited_content
    r = await t.req(
        "POST",
        f"/v1/reviews/{exc.id}/decision",
        "admin",
        json={"decision": "redact_text", "rationale_code": "other", "edited_content": "Sarah thanked a colleague for the introduction."},
        headers={"if-match": detail.headers["etag"]},
    )
    assert r.status_code == 200, r.text
    final2 = await run_deletion(api, t, rid, until=("completed",))
    assert final2["status"] == "completed" and final2["receipt"]["verification"]["text_residuals"] == 0
    assert (
        len(await raw(api, t, "SELECT 1 FROM deletion_manifests WHERE request_id = :r", r=uuid.UUID(rid))) == 0
    )  # the manifest is gone at completion
    texts = [x.content for x in await raw(api, t, "SELECT content FROM curated_memories WHERE workspace_id = :ws")]
    assert not any(SENTINEL in c for c in texts) and "Sarah thanked a colleague for the introduction." in texts
    # the journal line (outside the database) names ids and HMACs, never a name
    line = [
        e
        for e in api.app.state.kg.deletion.journal.since(__import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").UTC))
        if e["request_id"] == rid
    ]
    assert line and w["zeb"] in line[0]["ids"]["entities"] and SENTINEL not in str(line[0]) and line[0]["suppression_hmacs"]
    assert api.app.state.kg.deletion.journal.verify() >= 1
    # re-creating him is still refused; an admin can lift the suppression (audited)
    assert (
        await t.entity("Person", "New Zeb", external_ids=[{"system": "hr", "value": SENTINEL_EXT}], source_system="hr", source_ref="n")
    ).status_code == 409
    prefix = (await t.get("/v1/suppressions", "admin")).json()[0]["value_hmac_prefix"]
    assert (await t.req("POST", f"/v1/suppressions/{prefix}/lift", "admin")).status_code == 200
    assert (
        await t.entity("Person", "Zeb Returns", external_ids=[{"system": "hr", "value": SENTINEL_EXT}], source_system="hr", source_ref="n2")
    ).status_code == 201
    assert len(await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'suppression.lifted'")) == 1


async def test_deleting_one_interaction_removes_its_memory_and_keeps_facts_with_other_evidence(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "interaction", w["i_sar"], "admin")).json()["request_id"]
    assert (await t.get(f"/v1/interactions/{w['i_sar']}", "admin")).status_code == 404
    assert (await t.get(f"/v1/memories/{w['m_keep']}", "admin")).status_code == 404  # rested only on it
    final = await run_deletion(api, t, rid)
    assert final["status"] == "completed", final
    assert (await raw(api, t, "SELECT count(*) AS n FROM curated_memories WHERE id = :m", m=uuid.UUID(w["m_keep"])))[0].n == 0
    assert (await raw(api, t, "SELECT count(*) AS n FROM interactions WHERE id = :i", i=uuid.UUID(w["i_sar"])))[0].n == 0
    # Sarah, her fact and the other interaction stay: they have their own evidence
    assert (await t.get(f"/v1/entities/{w['sarah']}", "admin")).status_code == 200
    assert (await t.get(f"/v1/relationships/{w['works']}", "admin")).status_code == 200
    assert (await t.get(f"/v1/interactions/{w['i_zeb']}", "admin")).status_code == 200


async def test_an_old_id_answers_like_a_missing_one_after_the_purge(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "interaction", w["i_sar"], "admin")).json()["request_id"]
    await run_deletion(api, t, rid)
    gone = await t.get(f"/v1/interactions/{w['i_sar']}", "admin")
    missing = await t.get(f"/v1/interactions/{uuid.uuid4()}", "admin")
    assert gone.status_code == missing.status_code == 404
    assert {k: v for k, v in gone.json()["error"].items() if k != "request_id"} == {
        k: v for k, v in missing.json()["error"].items() if k != "request_id"
    }  # byte-identical apart from the request id
    assert (await request_status(api, t, rid))["status"] == "completed"
