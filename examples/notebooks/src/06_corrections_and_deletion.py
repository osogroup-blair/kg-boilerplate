# %% [markdown]
# # 6. Corrections and deletion
#
# Extraction can be wrong, and people have the right to be forgotten. Both are **governed commands**: previewed, audited, applied
# everywhere the platform keeps a derivative, and visible in the very next context.

# %%
import uuid

import httpx

from examples._kit import admin, notebook, waits
from examples.sarah.steps import PROJECT

lab = notebook.start("governance")
s = lab.s
s.s1_ontology()
s.s2_commands()
s.s3_conversation()
sarah, michael = s.ids["sarah"], s.ids["michael"]


def michael_manages(context_text):
    return [ln for ln in context_text.splitlines() if "manages" in ln.lower()]


# %% [markdown]
# ## A wrong claim arrives
# A message says "Michael is Sarah's manager". The model extracts a `MANAGES` fact. It is **unverified**: the context says so.

# %%
event = s.ingest("nb-manager", "Michael is Sarah's manager.", [sarah, michael], "2026-10-14T09:00:00Z")
episode = waits.wait_interaction(lab.ws, event, timeout=240)
s.settle()
ask = dict(task="What do we know about Michael and Sarah?", subject_ids=[sarah, michael], profile="planning", max_tokens=600)
before = s.build("admin", **ask)
print(michael_manages(before["context_text"]))
fact = next(f for f in admin.get(lab.ws, f"/v1/entities/{michael}/relationships", include="proposed").json() if f["rel_type"] == "MANAGES")
print(fact["status"], fact["evidence_status"], fact["source_type"])
assert michael_manages(before["context_text"]) and fact["evidence_status"] == "unverified"

# %% [markdown]
# ## Preview, then correct
# A reviewer first asks what rejecting it would touch. Then applies the correction with `If-Match`, so nobody overwrites a change they have not seen.

# %%
preview = admin.get(lab.ws, f"/v1/relationships/{fact['id']}/corrections/preview", kind="reject").json()
print("graph operations :", preview["graph_operations"])
print("dependent records:", preview["dependent_memory_counts"], "| recent contexts:", preview.get("recent_context_count"))
etag = f'W/"rev-{admin.get(lab.ws, f"/v1/relationships/{fact['id']}").json()["revision"]}"'
with httpx.Client(
    base_url=lab.env.base_url,
    headers={"authorization": f"Bearer {lab.ws.admin_key}", "idempotency-key": str(uuid.uuid4()), "if-match": etag},
) as c:
    done = c.post(f"/v1/relationships/{fact['id']}/corrections", json={"kind": "reject", "reason_code": "incorrect_extraction"})
print("correction:", done.status_code)
assert done.status_code == 200

# %% [markdown]
# ## The next context reflects it; the evidence stays
# The rejected claim is gone from new packages. The original conversation is **retained** as evidence under the source's retention policy.

# %%
after = s.build("admin", **ask)
print("claim in a new context:", michael_manages(after["context_text"]))
print("original episode still readable:", admin.get(lab.ws, f"/v1/interactions/{event}").status_code)
assert not michael_manages(after["context_text"]) and admin.get(lab.ws, f"/v1/interactions/{event}").status_code == 200

# %% [markdown]
# ## Deletion
# A data-subject request about Michael. Reads are blocked **immediately**; a purge then walks every store (Postgres, the vector index,
# the graph, traces) and ends with a receipt that lists what was removed and verified.

# %%
import time

request = admin.post(
    lab.ws, "/v1/deletion-requests", {"kind": "subject", "target_id": michael, "reason_code": "data_subject_request"}, expect=(202,)
)
t0 = time.time()
print("entity read right after the request:", admin.get(lab.ws, f"/v1/entities/{michael}").status_code, f"({time.time() - t0:.1f}s)")
now = s.build(
    "planning-agent", task="What do we know about Michael?", subject_ids=[sarah], profile="planning", max_tokens=600, project=PROJECT
)
print("Michael in a new context:", "Michael" in now["context_text"])
assert admin.get(lab.ws, f"/v1/entities/{michael}").status_code == 404 and "Michael" not in now["context_text"]

# %%
final = waits.wait_until(
    lambda: (
        (d := admin.get(lab.ws, f"/v1/deletion-requests/{request['request_id']}").json())["status"]
        in ("completed", "completed_with_exceptions", "failed"),
        d,
    ),
    timeout=300,
    what="the purge",
)
receipt = final["receipt"]
print("purge:", final["status"])
print("stores:", sorted(receipt["stores"]))
print("residual ids:", receipt["verification"].get("id_residuals"), "| backup copies expire by:", receipt.get("backup_copies_expire_by"))
assert final["status"] in ("completed", "completed_with_exceptions") and receipt["verification"].get("id_residuals") == 0

# %% [markdown]
# Not shown here: **legal holds** (a hold blocks a purge and records why) and **identity split** (undoing a wrong merge). Both are in
# `docs/runbooks/legal-hold.md` and `docs/runbooks/identity-correction.md`.

# %%
lab.close()
