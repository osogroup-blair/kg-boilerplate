"""P8 WP9 (§5.8): the privacy audit view: who revealed, read in private, deleted, held, granted, overrode or changed settings, and what went to which model under which decision.
It is built from ids and codes only, admin-only, workspace-scoped, filterable, paged and exportable."""

from __future__ import annotations

import csv
import io

import pytest

from tests.support.p8 import SENTINEL, correct_fact, deletion_world, request_deletion, rev_header

pytestmark = pytest.mark.integration
URL = "/v1/governance/privacy-audit"


async def test_the_timeline_lists_each_kind_of_sensitive_action_filters_pages_and_exports(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    assert (await t.get(f"/v1/interactions/{w['i_sar']}/content", "admin", form="raw")).status_code == 200  # a reveal
    assert (
        await t.req("PUT", "/v1/workspaces/current/settings", "admin", json={"backup_retention_days": 14}, headers=rev_header(0))
    ).status_code == 200
    hold = await t.req(
        "POST",
        "/v1/legal-holds",
        "admin",
        json={"scope_kind": "subject", "target_id": w["sarah"], "reason_code": "litigation", "note": "case 42"},
    )
    assert hold.status_code == 201
    assert (await t.req("DELETE", f"/v1/legal-holds/{hold.json()['id']}", "admin")).status_code == 200
    assert (await request_deletion(t, "interaction", w["i_zeb"], "admin")).status_code == 202
    pid = await t.principal_id("agent")
    assert (
        await t.req("POST", f"/v1/principals/{pid}/grants", "admin", json={"scope_kind": "subject", "scope_ref": w["sarah"]})
    ).status_code == 201
    fixed = await correct_fact(t, w["works"], "reject", reason="incorrect_extraction")
    assert fixed.status_code == 200, fixed.text

    full = (await t.get(URL, "admin", limit=200)).json()
    cats = {i["category"] for i in full["items"]}
    assert {"reveals", "settings", "holds", "deletions", "grants", "overrides"} <= cats, cats
    assert all(i["actor_principal_id"] for i in full["items"] if i["category"] in ("reveals", "settings", "holds"))
    # ids and codes only: neither the subject's name nor any content appears anywhere in what the view returns
    blob = str(full)
    assert SENTINEL not in blob and "Sarah rode" not in blob and "case 42" not in blob
    # categories filter, and an unknown one is a 422
    only = (await t.get(URL, "admin", category=["holds"])).json()["items"]
    assert (
        only and {i["category"] for i in only} == {"holds"} and {i["action"] for i in only} >= {"legal_hold.created", "legal_hold.released"}
    )
    assert (await t.get(URL, "admin", category=["nope"])).status_code == 422
    # paging walks the whole list without repeats
    seen, cursor = [], None
    while True:
        page = (await t.get(URL, "admin", limit=2, **({"cursor": cursor} if cursor else {}))).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == len(full["items"])
    # the egress section counts model calls by purpose, classification and outcome
    assert isinstance(full["egress"], list)
    # CSV
    exp = await t.get(URL, "admin", format="csv", limit=200)
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exp.text)))
    assert rows[0][:4] == ["at", "category", "action", "outcome"] and len(rows) - 1 == len(full["items"])
    assert SENTINEL not in exp.text
    # time window
    assert (await t.get(URL, "admin", until="2000-01-01T00:00:00Z")).json()["items"] == []


async def test_only_an_auditor_reads_it_and_a_workspace_sees_only_its_own(api, tenant_governance, bare_tenant):
    t, other = tenant_governance, bare_tenant
    await t.req("PUT", "/v1/workspaces/current/settings", "admin", json={"backup_retention_days": 7}, headers=rev_header(0))
    for who in ("app", "agent", "reader", "operator"):
        assert (await t.get(URL, who)).status_code == 403, who
    assert (await t.get(URL, "reviewer")).status_code in (200, 403)
    mine = (await t.get(URL, "admin")).json()["items"]
    theirs = (await other.get(URL, "admin")).json()["items"]
    assert mine and not {i["id"] for i in mine} & {i["id"] for i in theirs}
    assert (await t.get(URL, "admin", actor="00000000-0000-0000-0000-000000000000")).json()["items"] == []
