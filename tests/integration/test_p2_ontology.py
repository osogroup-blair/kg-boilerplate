"""WP5: ontology registry API. ONT-01 (draft, validate, fix), ONT-02 (immutable publish, hash, audit, rollback), ONT-05 (diff + impact), ONT-06 (rules)."""

from __future__ import annotations

import copy
import json
import uuid

import pytest
import yaml
from sqlalchemy import text

from tests.support.tenants import EXAMPLE_YAML

pytestmark = pytest.mark.integration


def doc(version: str = "1.0.0") -> dict:
    d = json.loads(json.dumps(yaml.safe_load(EXAMPLE_YAML), default=str))  # a JSON document, as a client would send it
    d["ontology"]["version"] = version
    return d  # type: ignore[no-any-return]


async def draft(t, content, who="admin", fmt=None, key=None):  # type: ignore[no-untyped-def]
    body = {"content": content}
    if fmt:
        body["format"] = fmt
    return await t.req("POST", "/v1/ontologies/versions", who, json=body, key=key)


async def audit_actions(api, ws) -> list[str]:  # type: ignore[no-untyped-def]
    from kg_db.session import tenant_session
    from kg_domain.principal import WorkspaceContext

    async with tenant_session(api.engine, WorkspaceContext.for_system_task(ws, reason="test")) as c:
        return [
            r[0]
            for r in (
                await c.execute(text("SELECT action FROM audit_log WHERE workspace_id = :w ORDER BY created_at, id"), {"w": ws})
            ).all()
        ]


# ---- ONT-01 -------------------------------------------------------------------------------------------------------------------------------


async def test_ont01_an_invalid_draft_is_saved_with_a_report_of_paths_and_can_be_fixed(bare_tenant):
    t = bare_tenant
    bad = doc()
    bad["relationship_types"]["WORKS_AT"]["target"] = "Company"
    bad["entity_types"]["Person"]["attributes"]["labels"] = {"type": "string"}  # collides with a Graphiti node field
    r = await draft(t, bad)
    assert r.status_code == 201, r.text
    v = r.json()
    errors = {e["path"]: e["code"] for e in v["validation_report"]["errors"]}
    assert errors == {"relationship_types.WORKS_AT.target": "unknown_entity_type"}  # the compile check waits until the structure is sound
    assert v["state"] == "draft" and v["error_count"] == 1 and v["content_hash"] is None and r.headers["etag"] == 'W/"rev-1"'

    fixed = doc()
    fixed["entity_types"]["Person"]["attributes"]["labels"] = {"type": "string"}
    r2 = await t.req("PUT", f"/v1/ontologies/versions/{v['id']}", "admin", json={"content": fixed}, headers={"If-Match": r.headers["etag"]})
    assert r2.status_code == 200
    assert {e["path"]: e["code"] for e in r2.json()["validation_report"]["errors"]} == {
        "entity_types.Person.attributes.labels": "reserved_graphiti_field"
    }

    del fixed["entity_types"]["Person"]["attributes"]["labels"]
    r3 = await t.req(
        "PUT", f"/v1/ontologies/versions/{v['id']}", "admin", json={"content": fixed}, headers={"If-Match": r2.headers["etag"]}
    )
    assert r3.status_code == 200 and r3.json()["validation_report"]["errors"] == [] and r3.headers["etag"] == 'W/"rev-3"'
    rv = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/validate", "reviewer")  # a read-only role can validate
    assert rv.status_code == 200 and rv.json()["errors"] == [] and len(rv.json()["content_hash"]) == 64


async def test_ont01_yaml_and_json_documents_and_syntax_errors(bare_tenant):
    t = bare_tenant
    r = await draft(t, EXAMPLE_YAML)
    assert (
        r.status_code == 201 and r.json()["validation_report"]["errors"] == [] and r.json()["source_text"] == EXAMPLE_YAML
    )  # comments survive
    broken = await draft(t, "ontology: [unclosed")
    assert broken.status_code == 201 and broken.json()["validation_report"]["errors"][0]["code"] == "yaml_syntax"
    assert broken.json()["ontology_key"] is None and broken.json()["version"] is None
    as_json = await draft(t, doc("2.0.0"), fmt="json")
    assert as_json.status_code == 201 and as_json.json()["version"] == "2.0.0" and "ontology:" in as_json.json()["source_text"]
    dup = await draft(t, EXAMPLE_YAML)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "version_exists"
    listing = await t.get("/v1/ontologies/versions")
    assert listing.status_code == 200 and len(listing.json()["items"]) == 3


async def test_ont01_a_draft_can_be_copied_and_deleted(bare_tenant):
    t = bare_tenant
    v1 = (await draft(t, EXAMPLE_YAML)).json()
    cp = await t.req("POST", "/v1/ontologies/versions", "admin", json={"copy_of": v1["id"]})
    assert cp.status_code == 201 and cp.json()["version"] == "1.1.0" and cp.json()["base_version_id"] == v1["id"]
    gone = await t.req("DELETE", f"/v1/ontologies/versions/{cp.json()['id']}", "admin")
    assert gone.status_code == 200 and gone.json()["deleted"] is True
    assert (await t.get(f"/v1/ontologies/versions/{cp.json()['id']}")).status_code == 404


# ---- ONT-02 -------------------------------------------------------------------------------------------------------------------------------


async def test_ont02_publish_freezes_the_version_hashes_it_and_audits(bare_tenant, api):
    t = bare_tenant
    v = (await draft(t, EXAMPLE_YAML)).json()
    r = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={"notes": "first release"})
    assert r.status_code == 200, r.text
    pub = r.json()
    assert (
        pub["state"] == "published"
        and pub["active"]
        and pub["activated"]
        and len(pub["content_hash"]) == 64
        and pub["notes"] == "first release"
    )
    assert pub["published_at"] and pub["spec"]["ontology"]["version"] == "1.0.0"
    put = await t.req(
        "PUT", f"/v1/ontologies/versions/{v['id']}", "admin", json={"content": doc("9.9.9")}, headers={"If-Match": r.headers["etag"]}
    )
    assert put.status_code == 409 and put.json()["error"]["code"] == "immutable_version"
    assert (await t.req("DELETE", f"/v1/ontologies/versions/{v['id']}", "admin")).status_code == 409
    again = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "immutable_version"
    active = (await t.get("/v1/ontologies/active", "agent")).json()
    assert active["version"]["id"] == v["id"] and active["entity_types"] == ["Organization", "Person", "Topic"]
    assert {"type": "WORKS_AT", "source": "Person", "target": "Organization"} in active["relationship_types"]
    actions = await audit_actions(api, t.ws)
    assert {"ontology.draft_created", "ontology.published", "ontology.activated"} <= set(actions)
    audit = (await t.get("/v1/audit", "admin", limit=50)).json()["items"]
    published = next(a for a in audit if a["action"] == "ontology.published")
    assert published["metadata"]["content_hash"] == pub["content_hash"] and published["metadata"]["version_id"] == v["id"]


async def test_ont02_rollback_moves_the_pointer_and_never_edits_a_version(bare_tenant, api):
    t = bare_tenant
    v1 = (await draft(t, EXAMPLE_YAML)).json()
    p1 = (await t.req("POST", f"/v1/ontologies/versions/{v1['id']}/publish", "admin", json={})).json()
    cp = (await t.req("POST", "/v1/ontologies/versions", "admin", json={"copy_of": v1["id"]})).json()
    p2 = (await t.req("POST", f"/v1/ontologies/versions/{cp['id']}/publish", "admin", json={"notes": "second"})).json()
    assert p2["version"] == "1.1.0" and (await t.get("/v1/ontologies/active")).json()["version"]["id"] == cp["id"]

    rb = await t.req("POST", "/v1/ontologies/active", "admin", json={"version_id": v1["id"], "reason_code": "rollback_regression"})
    assert rb.status_code == 200 and rb.json()["version"]["id"] == v1["id"]
    after1 = (await t.get(f"/v1/ontologies/versions/{v1['id']}")).json()
    after2 = (await t.get(f"/v1/ontologies/versions/{cp['id']}")).json()
    assert after1["content_hash"] == p1["content_hash"] and after1["spec"] == p1["spec"]  # neither version changed
    assert after2["content_hash"] == p2["content_hash"] and after2["spec"] == p2["spec"] and after2["state"] == "published"
    assert after1["active"] and not after2["active"]
    from kg_db.session import tenant_session
    from kg_domain.principal import WorkspaceContext

    async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
        rows = (
            await c.execute(
                text(
                    "SELECT version_id, previous_version_id, reason_code FROM ontology_activations WHERE workspace_id = :w ORDER BY activated_at, id"
                ),
                {"w": t.ws},
            )
        ).all()
    assert [(str(r.version_id), r.reason_code) for r in rows] == [
        (v1["id"], "publish"),
        (cp["id"], "publish"),
        (v1["id"], "rollback_regression"),
    ]
    assert str(rows[2].previous_version_id) == cp["id"]
    assert (await t.req("POST", "/v1/ontologies/active", "admin", json={"version_id": v1["id"], "reason_code": "again"})).json()["error"][
        "code"
    ] == "already_active"
    draft_only = (await draft(t, doc("3.0.0"))).json()
    assert (await t.req("POST", "/v1/ontologies/active", "admin", json={"version_id": draft_only["id"], "reason_code": "x"})).json()[
        "error"
    ]["code"] == "version_not_published"


async def test_ont02_retire_needs_a_published_non_active_version(bare_tenant):
    t = bare_tenant
    v1 = (await draft(t, EXAMPLE_YAML)).json()
    await t.req("POST", f"/v1/ontologies/versions/{v1['id']}/publish", "admin", json={})
    assert (await t.req("POST", f"/v1/ontologies/versions/{v1['id']}/retire", "admin")).json()["error"]["code"] == "version_active"
    cp = (await t.req("POST", "/v1/ontologies/versions", "admin", json={"copy_of": v1["id"]})).json()
    await t.req("POST", f"/v1/ontologies/versions/{cp['id']}/publish", "admin", json={})
    r = await t.req("POST", f"/v1/ontologies/versions/{v1['id']}/retire", "admin")
    assert r.status_code == 200 and r.json()["state"] == "retired" and r.json()["retired_at"]
    assert (await t.req("POST", "/v1/ontologies/active", "admin", json={"version_id": v1["id"], "reason_code": "x"})).json()["error"][
        "code"
    ] == "version_not_published"


async def test_publish_with_errors_is_refused_and_leaves_the_draft_a_draft(bare_tenant):
    t = bare_tenant
    bad = doc()
    bad["relationship_types"]["WORKS_AT"]["target"] = "Company"
    v = (await draft(t, bad)).json()
    r = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ontology_invalid"
    assert r.json()["error"]["details"]["errors"][0]["path"] == "relationship_types.WORKS_AT.target"
    assert (await t.get(f"/v1/ontologies/versions/{v['id']}")).json()["state"] == "draft"
    assert (await t.get("/v1/ontologies/active")).json()["version"] is None


async def test_a_failing_rule_test_blocks_publish(bare_tenant):
    t = bare_tenant
    d = doc()
    succession = next(r for r in d["relationship_types"]["WORKS_AT"]["rules"] if isinstance(r, dict))
    succession["tests"][0]["expect"] = "reject"  # the scenario accepts, so this expectation is wrong
    v = (await draft(t, d)).json()
    assert [e["code"] for e in v["validation_report"]["errors"]] == ["rule_test_failed"]
    r = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={})
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["path"].endswith("tests[0]")


async def test_publish_is_idempotent_and_a_reused_key_with_another_body_conflicts(bare_tenant):
    t = bare_tenant
    v = (await draft(t, EXAMPLE_YAML)).json()
    first = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={"notes": "n"}, key="pub-1")
    replay = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={"notes": "n"}, key="pub-1")
    assert first.status_code == replay.status_code == 200 and replay.headers["idempotent-replay"] == "true"
    assert replay.json() == first.json()  # not a 409 immutable_version: the stored response is replayed
    clash = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={"notes": "different"}, key="pub-1")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_mismatch"
    other_route = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/retire", "admin", key="pub-1")
    assert other_route.status_code == 409 and other_route.json()["error"]["code"] == "idempotency_mismatch"
    missing = await t.req("POST", f"/v1/ontologies/versions/{v['id']}/retire", "admin", key="")
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "idempotency_key_required"


async def test_etag_and_if_match_flow(bare_tenant):
    t = bare_tenant
    created = await draft(t, EXAMPLE_YAML)
    vid = created.json()["id"]
    got = await t.get(f"/v1/ontologies/versions/{vid}")
    assert got.headers["etag"] == created.headers["etag"] == 'W/"rev-1"'
    no_header = await t.req("PUT", f"/v1/ontologies/versions/{vid}", "admin", json={"content": EXAMPLE_YAML})
    assert no_header.status_code == 428 and no_header.json()["error"]["code"] == "precondition_required"
    stale = await t.req("PUT", f"/v1/ontologies/versions/{vid}", "admin", json={"content": EXAMPLE_YAML}, headers={"If-Match": 'W/"rev-7"'})
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "precondition_failed"
    junk = await t.req("PUT", f"/v1/ontologies/versions/{vid}", "admin", json={"content": EXAMPLE_YAML}, headers={"If-Match": "banana"})
    assert junk.status_code == 412
    ok = await t.req("PUT", f"/v1/ontologies/versions/{vid}", "admin", json={"content": EXAMPLE_YAML}, headers={"If-Match": 'W/"rev-1"'})
    assert ok.status_code == 200 and ok.headers["etag"] == 'W/"rev-2"'


# ---- ONT-05, ONT-06 -----------------------------------------------------------------------------------------------------------------------


async def test_ont05_diff_flags_breaking_changes_and_counts_the_rows_they_touch(tenant):
    t = tenant
    active = (await t.get("/v1/ontologies/active")).json()["version"]
    sarah = (await t.entity("Person", "Sarah", classification="internal", source_system="hr", source_ref="x")).json()["entity_id"]
    assert sarah
    next_doc = doc("2.0.0")
    next_doc["entity_types"]["Person"]["attributes"]["nickname"] = {"type": "string", "max_length": 20}  # additive
    next_doc["entity_types"]["Person"]["attributes"]["employee_no"] = {"type": "string", "required": True}  # breaking
    del next_doc["entity_types"]["Topic"]  # breaking (and CONSIDERING loses its target)
    next_doc["relationship_types"].pop("CONSIDERING")
    v = (await draft(t, next_doc)).json()
    d = await t.get(f"/v1/ontologies/versions/{v['id']}/diff", against="active")
    assert d.status_code == 200
    body = d.json()
    assert body["from_version"] == "1.0.0" and body["to_version"] == "2.0.0"
    flags = {c["path"]: c["breaking"] for c in body["changes"]}
    assert flags["entity_types.Person.attributes.nickname"] is False
    assert flags["entity_types.Person.attributes.employee_no"] is True
    assert flags["entity_types.Topic"] is True and flags["relationship_types.CONSIDERING"] is True
    assert {(i["type_key"], i["kind"]): i["active"] for i in body["impact"]} == {
        ("Person", "entity"): 1,
        ("Topic", "entity"): 0,
        ("CONSIDERING", "relationship"): 0,
    }
    vs_id = await t.get(f"/v1/ontologies/versions/{v['id']}/diff", against=active["id"])
    assert vs_id.json()["changes"] == body["changes"]
    assert (await t.get(f"/v1/ontologies/versions/{v['id']}/diff", against="nonsense")).status_code == 422
    assert (await t.get(f"/v1/ontologies/versions/{uuid.uuid4()}/diff")).status_code == 404


async def test_ont06_rule_catalog_lists_every_kind_with_a_schema_and_a_conflict_policy(tenant):
    r = await tenant.get("/v1/ontologies/rule-catalog", "reader")
    assert r.status_code == 200
    kinds = {k["kind"]: k for k in r.json()["kinds"]}
    assert {"succession", "max_active_per_source", "queue_if", "strict_types", "min_authority"} <= set(kinds)
    assert all(k["conflict_policy"] and k["description"] and "properties" in k["params_schema"] or k["implicit"] for k in kinds.values())
    assert kinds["max_active_per_source"]["params_schema"]["required"] == ["n"] and kinds["strict_types"]["implicit"] is True
    assert r.json()["stages"][0] == "schema_validation"


# ---- authorization and isolation ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["reviewer", "operator", "app", "agent", "reader"])
async def test_only_workspace_admins_can_write_or_publish(bare_tenant, who):
    t = bare_tenant
    assert (await draft(t, EXAMPLE_YAML, who=who)).status_code == 403
    v = (await draft(t, EXAMPLE_YAML)).json()
    assert (
        await t.req("PUT", f"/v1/ontologies/versions/{v['id']}", who, json={"content": EXAMPLE_YAML}, headers={"If-Match": 'W/"rev-1"'})
    ).status_code == 403
    assert (await t.req("DELETE", f"/v1/ontologies/versions/{v['id']}", who)).status_code == 403
    assert (await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", who, json={})).status_code == 403
    assert (await t.req("POST", "/v1/ontologies/active", who, json={"version_id": v["id"], "reason_code": "x"})).status_code == 403
    assert (await t.req("POST", f"/v1/ontologies/versions/{v['id']}/retire", who)).status_code == 403
    for path in ("/v1/ontologies/versions", f"/v1/ontologies/versions/{v['id']}", "/v1/ontologies/active", "/v1/ontologies/rule-catalog"):
        assert (await t.get(path, who)).status_code == 200  # every role reads the ontology
    assert (await t.req("POST", f"/v1/ontologies/versions/{v['id']}/validate", who)).status_code == 200


async def test_another_workspaces_versions_are_not_found(api, tenant):
    mine = (await tenant.get("/v1/ontologies/active")).json()["version"]["id"]
    from tests.support.tenants import clear_graph, make_tenant

    other = await make_tenant(api, ontology=None)
    try:
        assert (await other.get("/v1/ontologies/versions")).json()["items"] == []
        probes = [
            ("GET", f"/v1/ontologies/versions/{mine}"),
            ("POST", f"/v1/ontologies/versions/{mine}/validate"),
            ("GET", f"/v1/ontologies/versions/{mine}/diff"),
            ("POST", f"/v1/ontologies/versions/{mine}/publish"),
            ("POST", f"/v1/ontologies/versions/{mine}/retire"),
            ("DELETE", f"/v1/ontologies/versions/{mine}"),
        ]
        for method, path in probes:
            foreign = await other.req(method, path, "admin", json={} if method == "POST" and "publish" in path else None)
            random = await other.req(
                method, path.replace(mine, str(uuid.uuid4())), "admin", json={} if method == "POST" and "publish" in path else None
            )
            assert foreign.status_code == random.status_code == 404, (method, path)
            assert {k: v for k, v in foreign.json()["error"].items() if k != "request_id"} == {
                k: v for k, v in random.json()["error"].items() if k != "request_id"
            }
        copy_foreign = await other.req("POST", "/v1/ontologies/versions", "admin", json={"copy_of": mine})
        assert copy_foreign.status_code == 404
        act = await other.req("POST", "/v1/ontologies/active", "admin", json={"version_id": mine, "reason_code": "x"})
        assert act.status_code == 404
        against = await other.get(f"/v1/ontologies/versions/{(await draft(other, EXAMPLE_YAML)).json()['id']}/diff", against=mine)
        assert against.status_code == 404
    finally:
        await clear_graph(api, other.group_id)


async def test_the_draft_document_is_not_mutated_by_a_failed_publish(bare_tenant):
    t = bare_tenant
    original = doc()
    original["entity_types"]["Person"]["attributes"]["age"] = {"type": "integer", "default": "forty"}
    v = (await draft(t, original)).json()
    before = copy.deepcopy(v["spec"])
    assert (await t.req("POST", f"/v1/ontologies/versions/{v['id']}/publish", "admin", json={})).status_code == 422
    assert (await t.get(f"/v1/ontologies/versions/{v['id']}")).json()["spec"] == before


async def test_seeding_an_ontology_is_idempotent_and_goes_through_the_same_validation(api, bare_tenant):
    from kg_cli.seed import publish_ontology

    t = bare_tenant
    assert await publish_ontology(api.engine, EXAMPLE_YAML, slug=t.slug) is True
    assert await publish_ontology(api.engine, EXAMPLE_YAML, slug=t.slug) is False  # `make seed` can run again
    active = (await t.get("/v1/ontologies/active")).json()["version"]
    assert active["version"] == "1.0.0" and active["state"] == "published" and len(active["content_hash"]) == 64
    with pytest.raises(SystemExit):
        await publish_ontology(api.engine, "ontology: {key: x}", slug=t.slug)  # an invalid document is refused, not stored
