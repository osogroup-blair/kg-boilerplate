# %% [markdown]
# # 1. Ingest and the ledger
#
# Everything the platform learns arrives as an **interaction**: a source-linked, idempotent event written to an encrypted ledger and
# processed later by a worker. This notebook sends an event, replays it safely, reads its stage timeline, shows who may read the raw
# content, and recovers an event that was caught in a model outage.
#
# Offline, the model behind extraction is the mock router (strict mode), so every run gives the same answers.

# %%
import time
import uuid

import httpx

from examples._kit import admin, mock, notebook, waits
from examples.sarah.steps import EVT003

lab = notebook.start("ledger")
s = lab.s
s.s1_ontology()  # publishes nothing new here: the workspace was created with the ontology; this registers the trusted app and the grants
s.s2_commands()  # Sarah, Acme, Michael, Entrepreneurship by command (no model call)
app = lab.ws.keys["conversation-app"]
print({k: v for k, v in s.ids.items() if k in ("sarah", "michael", "topic")})

# %% [markdown]
# ## The event envelope
# `source_system` + `source_ref` say where it came from, `occurred_at` when it happened, `subject_refs` who it is about.
# The `Idempotency-Key` header makes a retry safe: same key and same body is the **same** logical event.

# %%
key = str(uuid.uuid4())
body = {
    "source_system": "chat",
    "source_ref": "nb-evt-003",
    "kind": "message",
    "content": EVT003,
    "occurred_at": "2026-10-10T09:00:00Z",
    "subject_refs": [{"entity_id": s.ids[k]} for k in ("sarah", "michael", "topic")],
}
headers = {"authorization": f"Bearer {app}", "idempotency-key": key}
with httpx.Client(base_url=lab.env.base_url, timeout=60) as c:
    first = c.post("/v1/interactions", headers=headers, json=body)
    again = c.post("/v1/interactions", headers=headers, json=body)
    changed = c.post("/v1/interactions", headers=headers, json={**body, "content": body["content"] + " (edited)"})
event_id = first.json()["event_id"]
print("first :", first.status_code, event_id)
print("again :", again.status_code, again.json()["event_id"], "replay header:", again.headers.get("idempotent-replay"))
print("edited:", changed.status_code, changed.json()["error"]["code"])
assert first.status_code == 202 and again.json()["event_id"] == event_id
assert again.headers.get("idempotent-replay") == "true"
assert changed.status_code == 409  # the same key with a different body is refused, never merged

# %% [markdown]
# ## The stage timeline
# The worker normalises the event, extracts graph facts and proposes memories. `GET /v1/interactions/{id}` shows where it is.

# %%
detail = waits.wait_interaction(lab.ws, event_id, timeout=240)
print("processing status:", detail["processing_status"])
for st in detail["stages"]:
    print(f"  {st['stage']:<16} {st['status']}")
assert detail["processing_status"] == "indexed"

# %% [markdown]
# ## Who may read the raw content?
# The content is encrypted at rest and readable only with the right role. The trusted *app* that wrote it cannot read it back; an admin can.

# %%
as_app = admin.get(lab.ws, f"/v1/interactions/{event_id}/content", key=app)
as_admin = admin.get(lab.ws, f"/v1/interactions/{event_id}/content")
print("app  :", as_app.status_code)
print("admin:", as_admin.status_code, as_admin.json()["content"][:60], "...")
assert as_app.status_code == 403 and as_admin.status_code == 200

# %% [markdown]
# ## A model outage, and an audited replay
# With the router failing, the event is **accepted and kept**; its stage waits and retries. Once the fault clears, an operator replays it
# with a reason code, and the replay is written to the audit log.

# %%
mock.inject_fault(lab.env, "provider_unavailable", 200, "chat")
try:
    outage_id = s.ingest("nb-outage", "Sarah Johnson: I work at NewCo now.", [s.ids["sarah"]], "2026-10-15T09:00:00Z")
    time.sleep(8)
    stuck = admin.get(lab.ws, f"/v1/interactions/{outage_id}").json()
    print("during the outage:", stuck["processing_status"])
    assert stuck["processing_status"] != "indexed"
finally:
    mock.clear_faults(lab.env)
replay = admin.post(lab.ws, f"/v1/interactions/{outage_id}/replay", {"reason_code": "fault_cleared"})
done = waits.wait_interaction(lab.ws, outage_id, timeout=240)
print("after the replay :", done["processing_status"])
assert done["processing_status"] in ("indexed", "partial")

audit = admin.get(lab.ws, "/v1/audit", limit=50).json()
rows = audit["items"] if isinstance(audit, dict) else audit
replays = [r for r in rows if "replay" in r["action"]]
print([(r["action"], r.get("reason_code") or r.get("details", {}).get("reason_code")) for r in replays][:3])
assert replays, "the replay is audited"

# %%
lab.close()
