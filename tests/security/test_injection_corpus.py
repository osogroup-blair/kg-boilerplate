"""SEC-04 / AT-15: a corpus of hostile text (tests/fixtures/injection/corpus.yaml, 45 payloads in 8 categories) pushed through every ingress. Everything is data: stored and shown
verbatim (or refused with a 422), quoted to a model only inside the user turn, delimited in a context, and never able to change a role, a grant, an authority class, a review
decision, a policy, a deletion plan or the purpose of a model call."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import yaml

from tests.support.p4 import Spied, add_mock_rules, ctx_of, sarah_story, sql, strict_mock, wait_terminal  # noqa: F401
from tests.support.p5 import save
from tests.support.p6 import ok_build
from tests.support.p8 import raw, rev_header
from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant
from tests.support.worker import running_worker, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.security]
CORPUS = yaml.safe_load((Path(__file__).resolve().parents[1] / "fixtures" / "injection" / "corpus.yaml").read_text())["payloads"]
QUIET = GOVERNANCE_YAML.replace(
    "    extraction: enabled", "    extraction: disabled", 1
)  # no memory-extraction call: only graph extraction sees interactions
assert QUIET != GOVERNANCE_YAML


def test_the_corpus_is_what_the_suite_says_it_is():
    assert len(CORPUS) >= 40 and len({p["id"] for p in CORPUS}) == len(CORPUS)
    assert {p["category"] for p in CORPUS} >= {"override", "spoof", "tool", "delimiter", "markup", "query", "exfil", "unicode", "social"}


async def authority_snapshot(api, t):  # type: ignore[no-untyped-def]
    r = await raw(
        api,
        t,
        "SELECT (SELECT count(*) FROM principals WHERE workspace_id = :ws) AS principals, (SELECT count(*) FROM role_assignments WHERE workspace_id = :ws) AS roles, "
        "(SELECT count(*) FROM principal_scopes WHERE workspace_id = :ws) AS grants, (SELECT count(*) FROM api_keys WHERE workspace_id = :ws) AS keys, "
        "(SELECT count(*) FROM deletion_requests WHERE workspace_id = :ws) AS deletions, (SELECT count(*) FROM legal_holds WHERE workspace_id = :ws AND released_at IS NULL) AS holds, "
        "(SELECT count(*) FROM workspace_settings WHERE workspace_id = :ws) AS settings, (SELECT count(*) FROM review_items WHERE workspace_id = :ws) AS reviews, "
        "(SELECT count(*) FROM facts WHERE workspace_id = :ws) AS facts, (SELECT count(*) FROM entities WHERE workspace_id = :ws) AS entities, "
        "(SELECT count(*) FROM ontology_versions WHERE workspace_id = :ws AND state = 'published') AS published, "
        "(SELECT count(*) FROM curated_memories WHERE workspace_id = :ws) AS memories",
    )
    return dict(r[0]._mapping)


def no_change(before: dict, after: dict, allowed: tuple[str, ...] = ()) -> None:  # type: ignore[type-arg]
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k] and k not in allowed}
    assert changed == {}, changed


async def audit_text(api, t) -> str:  # type: ignore[no-untyped-def]
    rows = await raw(api, t, "SELECT action, metadata::text AS m, resource_type FROM audit_log WHERE workspace_id = :ws")
    return "\n".join(f"{r.action} {r.m}" for r in rows)


def leak_probes() -> list[str]:
    # fragments distinctive enough to show up in an audit row if the text had been copied there
    return ["evil.example", "DROP TABLE", "ADMIN_OF", "delete_workspace", "maintenance mode", "api keys"]


async def test_interactions_with_hostile_content_are_extracted_as_data_and_change_nothing(api, strict_mock):  # noqa: F811
    t = await make_tenant(api, ontology=QUIET)
    try:
        ids = await sarah_story(t)
        before = await authority_snapshot(api, t)
        await add_mock_rules(
            [
                {"when": {"schema_name": "ExtractedEntities", "contains": ["INJ-"]}, "response": {"extracted_entities": []}},
                {"when": {"schema_name": "ExtractedEdges", "contains": ["INJ-"]}, "response": {"edges": []}},
            ]
        )
        accepted, refused = [], []
        with Spied(api) as spy:
            async with running_worker(api, concurrency=2):
                for p in CORPUS:
                    r = await t.req(
                        "POST",
                        "/v1/interactions",
                        "app",
                        json={
                            "source_system": "chat",
                            "source_ref": f"inj-{p['id']}",
                            "kind": "message",
                            "content": f"INJ-{p['id']}: {p['text']}",
                            "occurred_at": "2026-10-10T09:00:00Z",
                            "subject_refs": [{"entity_id": ids["sarah"]}],
                        },
                    )
                    assert r.status_code in (202, 422), (p["id"], r.status_code, r.text[:200])  # never a 5xx
                    (accepted if r.status_code == 202 else refused).append((p["id"], r))
                assert len(accepted) >= 40, [p for p, _ in refused]

                async def settled():  # type: ignore[no-untyped-def]
                    rows = await sql(
                        api,
                        t,
                        "SELECT count(*) FILTER (WHERE processing_status IN ('indexed','partial','failed')) AS done, count(*) AS n FROM interactions WHERE workspace_id = :ws AND source_ref LIKE 'inj-%'",
                    )
                    quiet = await sql(
                        api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased')"
                    )
                    return rows[0].done == rows[0].n and quiet[0].n == 0

                await wait_until(settled, timeout=600, what="the hostile interactions to be processed")
        after = await authority_snapshot(api, t)
        no_change(before, after)  # nothing the model could have been talked into: no entity, fact, grant, review, deletion, hold or setting
        statuses = {
            r.processing_status for r in await sql(api, t, "SELECT processing_status FROM interactions WHERE source_ref LIKE 'inj-%'")
        }
        assert statuses <= {"indexed", "partial"}, statuses
        # the router saw the hostile text only as a USER turn: no system / developer message ever carries it, and every call is for the one purpose that processes content
        model_requests = [r for r in spy.requests if r["path"].endswith("/chat/completions")]
        assert model_requests, "the extraction never reached the router"
        for r in model_requests:
            for m in r["body"]["messages"]:
                if m["role"] != "user":
                    assert "INJ-" not in str(m["content"]), (r["path"], m["role"])
        purposes = {x.purpose for x in await sql(api, t, "SELECT DISTINCT purpose FROM model_calls WHERE workspace_id = :ws")}
        assert not purposes & {"memory_consolidation", "ontology_simulation", "memory_extraction"}, (
            purposes
        )  # hostile content never reached a purpose that could act on it
        assert purposes, "no model call was recorded"
        for probe in leak_probes():
            assert probe not in await audit_text(api, t), probe
    finally:
        await clear_graph(api, t.group_id)


async def test_hostile_names_memories_notes_and_reasons_are_stored_as_data_and_the_context_keeps_its_delimiters(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    await t.grant("agent", "subject", ids["sarah"])
    before = await authority_snapshot(api, t)
    stored_memories: dict[str, str] = {}
    for p in CORPUS:
        text_ = p["text"].replace("\u0000", "")  # (a NUL is refused as a 422 elsewhere; the other control codes stay)
        # an entity whose name and alias are the payload (the app is a trusted source; names are data)
        e = await t.entity("Person", text_[:200], aliases=[text_[:100]], source_system="inj", source_ref=f"e-{p['id']}")
        assert e.status_code in (201, 422), (p["id"], e.status_code, e.text[:200])
        if e.status_code == 201:
            got = (await t.get(f"/v1/entities/{e.json()['entity_id']}", "admin")).json()
            assert got["name"] == text_[:200]  # verbatim, never interpreted
        # a memory whose content is the payload
        m = await save(
            t,
            "admin",
            {
                "type": "semantic",
                "content": text_,
                "subject_ids": [ids["sarah"]],
                "purpose": "injection-test",
                "source_ref": f"m-{p['id']}",
            },
        )
        assert m.status_code in (201, 422), (p["id"], m.status_code, m.text[:200])
        if m.status_code == 201:
            stored_memories[p["id"]] = m.json()["memory_id"]
            back = (await t.get(f"/v1/memories/{m.json()['memory_id']}", "admin")).json()
            assert back["content"] == text_
        # a legal hold note and a correction reason are free text too
        h = await t.req(
            "POST",
            "/v1/legal-holds",
            "admin",
            json={"scope_kind": "subject", "target_id": ids["michael"], "reason_code": "test", "note": text_[:400]},
        )
        assert h.status_code in (201, 422), (p["id"], h.status_code)
        if h.status_code == 201:
            assert (await t.req("DELETE", f"/v1/legal-holds/{h.json()['id']}", "admin")).status_code == 200
    assert len(stored_memories) >= 40
    # a memory is quoted in a context only inside the data block: the block's own markers cannot be closed or faked by what it quotes
    for who in ("admin", "agent"):
        out = await ok_build(t, who, "What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="planning")
        ctx = out["context_text"]
        assert ctx.count("[END CONTEXT DATA]") == 1 and ctx.count("[CONTEXT DATA") == 1, (who, ctx.count("[END CONTEXT DATA]"))
        assert ctx.rstrip().endswith("[END CONTEXT DATA]"), who
    after = await authority_snapshot(api, t)
    no_change(
        before, after, allowed=("entities", "memories")
    )  # data was added; no privilege, review, deletion, setting or ontology changed
    assert after["entities"] > before["entities"] and after["memories"] > before["memories"]
    audit = await audit_text(api, t)
    for probe in leak_probes():
        assert probe not in audit, probe  # the audit trail records ids and codes, never what was said
    # and none of it made an unprivileged caller more privileged
    assert (
        await t.req("POST", "/v1/deletion-requests", "agent", json={"kind": "workspace", "reason_code": "x", "confirm_slug": t.slug})
    ).status_code == 403
    assert (await t.get("/v1/workspaces/current/settings", "agent")).status_code == 403


async def test_a_hostile_review_rationale_decides_nothing_and_is_not_copied_into_the_audit_trail(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    rid = uuid.uuid4()
    await raw(
        api,
        t,
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, subject_entity_id) VALUES (:id, :ws, 'correction_request', 'correction', 'incorrect_extraction', :s)",
        id=rid,
        s=uuid.UUID(ids["sarah"]),
    )
    detail = await t.get(f"/v1/reviews/{rid}", "reviewer")
    assert detail.status_code == 200
    for p in CORPUS[:12]:
        r = await t.req(
            "POST",
            f"/v1/reviews/{rid}/decision",
            "reviewer",
            json={"decision": "not_a_decision", "rationale_code": "other", "rationale_text": p["text"][:900]},
            headers=rev_header(1),
        )
        assert r.status_code == 422  # free text cannot pick the decision: only the enumerated value does
    still = (await t.get(f"/v1/reviews/{rid}", "reviewer")).json()
    assert still["status"] == "open"
    for probe in leak_probes():
        assert probe not in await audit_text(api, t)


_ = (json, ctx_of, wait_terminal)
