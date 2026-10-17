"""P8 WP7 (SEC-05, MEM-07): after a deletion finishes, the subject is nowhere. Not in any text, JSON or array column of any table, not in the process logs the deletion wrote, not in the
metrics, not in the audit trail, not in the out-of-band journal, and the embeddings of the memories that went are gone with them (so a similarity search cannot find them)."""

from __future__ import annotations

import uuid

import pytest
from kg_obs.metrics import REGISTRY
from prometheus_client import generate_latest

from tests.support.p5 import search
from tests.support.p8 import SENTINEL, SENTINEL_EXT, deletion_world, owner_exec, raw, request_deletion, reviews_of, run_deletion

pytestmark = [pytest.mark.integration, pytest.mark.security]
TERMS = (SENTINEL, "Quillfeather", SENTINEL_EXT, "Zeb Q.")


async def finish_clean(api, t):  # type: ignore[no-untyped-def]
    """Deletes Zebulon, decides the one exception a person has to (the memory of Sarah's that names him), and runs to `completed`."""
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "subject", w["zeb"], "admin")).json()["request_id"]
    first = await run_deletion(api, t, rid)
    assert first["status"] == "completed_with_exceptions", first
    (exc,) = await reviews_of(api, t, "deletion_exception")
    detail = await t.get(f"/v1/reviews/{exc.id}", "admin")
    r = await t.req(
        "POST",
        f"/v1/reviews/{exc.id}/decision",
        "admin",
        json={"decision": "redact_text", "rationale_code": "other", "edited_content": "Sarah thanked a colleague for the introduction."},
        headers={"if-match": detail.headers["etag"]},
    )
    assert r.status_code == 200, r.text
    final = await run_deletion(api, t, rid, until=("completed",))
    assert final["status"] == "completed", final
    return w, rid


async def text_columns(query_rows: list) -> list[tuple[str, str]]:  # type: ignore[type-arg]
    return [(r.table_name, r.column_name) for r in query_rows]


async def test_no_table_holds_the_subject_in_any_text_json_or_array_column(api, tenant_governance):
    t = tenant_governance
    await finish_clean(api, t)
    cols = await owner_exec(
        "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c JOIN information_schema.tables tb ON tb.table_schema = c.table_schema AND tb.table_name = c.table_name "
        "WHERE c.table_schema = 'public' AND tb.table_type = 'BASE TABLE' AND c.data_type IN ('text','character varying','jsonb','json','ARRAY') "
        "AND EXISTS (SELECT 1 FROM information_schema.columns w WHERE w.table_schema = c.table_schema AND w.table_name = c.table_name AND w.column_name = 'workspace_id') ORDER BY 1, 2"
    )  # every tenant table (the database is shared with other tests' workspaces, which keep their own data)
    assert len(cols) > 100
    hits = []
    for c in cols:
        if c.column_name in {"name_embedding", "fact_embedding", "embedding"}:
            continue
        # every row of this workspace, whoever wrote it: the owner role sees all of it
        n = (
            await owner_exec(
                f'SELECT count(*) AS n FROM "{c.table_name}" WHERE workspace_id = :ws AND "{c.column_name}"::text ILIKE ANY(CAST(:terms AS text[]))',  # noqa: S608
                terms=[f"%{x}%" for x in TERMS],
                ws=t.ws,
            )
        )[0].n
        if n:
            hits.append((c.table_name, c.column_name, n))
    assert hits == [], hits


async def test_the_logs_the_metrics_the_audit_trail_and_the_journal_never_held_a_name(api, tenant_governance, capfd):
    t = tenant_governance
    w, rid = await finish_clean(api, t)
    out, err = capfd.readouterr()
    for term in TERMS:
        assert term not in out and term not in err, f"{term!r} reached the process logs"
    metrics_text = generate_latest(REGISTRY).decode()
    assert not any(term in metrics_text for term in TERMS)
    assert w["zeb"] not in metrics_text  # and no id either: labels are bounded sets
    audit = await raw(
        api, t, "SELECT action, metadata::text AS m, resource_type, resource_id::text AS rid FROM audit_log WHERE workspace_id = :ws"
    )
    assert audit and not any(term.lower() in f"{a.m} {a.action}".lower() for a in audit for term in TERMS)
    actions = {a.action for a in audit}
    assert {"deletion.requested", "deletion.purged", "deletion.completed"} <= actions, actions
    journal = api.app.state.kg.deletion.journal
    text = journal.path.read_text()
    assert not any(term in text for term in TERMS) and rid in text
    assert journal.verify() >= 1


async def test_the_embeddings_of_deleted_memories_are_gone_and_search_cannot_find_them(api, tenant_governance):
    t = tenant_governance
    w, _ = await finish_clean(api, t)
    gone = [uuid.UUID(w["m_zeb"]), uuid.UUID(w["m_from_zeb"])]
    assert (await raw(api, t, "SELECT count(*) AS n FROM memory_embeddings WHERE memory_id = ANY(:m)", m=gone))[0].n == 0
    for who in ("admin", "reviewer"):
        for q in ("Zebulon likes green tea in the afternoon", "Sarah greeted a colleague on Monday morning"):
            hits = (await search(t, who, q))["hits"]
            assert not {h["memory_id"] for h in hits} & {str(x) for x in gone}, (who, q)
            assert "green tea" not in str(hits) and "greeted a colleague" not in str(hits)
    # the survivors are still searchable: the deletion removed a subject, not the index
    kept = (await search(t, "admin", "Sarah rode far on Saturday"))["hits"]
    assert w["m_keep"] in {h["memory_id"] for h in kept}
