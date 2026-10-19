# %% [markdown]
# # 5. Sharing memory between agents
#
# Two agents work on the same project. What one **keeps private** stays private; what it **publishes** is labelled and reaches the other.
# Access comes from grants on subjects and projects, enforced where the data is read. The same rules hold over the SDK and over MCP.

# %%
import httpx

from examples._kit import admin, notebook, waits
from examples.agents.common import McpBackend, SdkBackend, handles
from examples.sarah.steps import EVT005, FINDING, NOTE, PROJECT, TASK

lab = notebook.start("sharing")
s = lab.s
s.s1_ontology()
s.s2_commands()
s.s3_conversation()  # also registers research-agent, planning-agent and an agent with no grants at all
sarah = s.ids["sarah"]
print(sorted(h for h in lab.ws.keys if h.endswith("agent")))

# %% [markdown]
# ## Grants decide what each agent can see
# The Research agent may read and propose on Sarah and read/propose/publish in the project. The Planning agent may only read. The third
# agent has no grants.

# %%
for me in ("research-agent", "planning-agent", "ungranted-agent"):
    who = admin.get(lab.ws, "/v1/me", key=lab.ws.keys[me]).json()["principal"]
    grants = admin.get(lab.ws, f"/v1/principals/{who['id']}/grants").json()
    print(f"{me:<16}", [(g["scope_kind"], g["scope_ref"][:8], g["privileges"]) for g in grants])

# %% [markdown]
# ## Private working notes and published findings
# The Research agent keeps a private note for its run, then proposes a finding backed by a source it stored itself.

# %%
with s.sdk("research-agent") as kg:
    source = kg.ingest_interaction(
        source_system="research-notes",
        source_ref="nb-evt-005",
        kind="document",
        content_type="text/plain",
        content=EVT005,
        occurred_at="2026-10-13T09:00:00Z",
        subject_refs=[{"entity_id": sarah}],
    )
    waits.wait_interaction(lab.ws, str(source.event_id), key=lab.ws.keys["research-agent"], timeout=240)
    note = kg.save_working_memory("run-1", NOTE)
    finding = kg.propose_memory(
        type="semantic",
        content=FINDING,
        scopes=[{"kind": "project", "ref": PROJECT}],
        source_event_ids=[str(source.event_id)],
        subject_ids=[sarah],
        purpose="research finding",
    )
s.settle()
print("note   :", note.status)
print("finding:", finding.status)
row = admin.get(lab.ws, f"/v1/memories/{finding.memory_id}").json()
print("labels :", row["authority_class"], "/", row["evidence_status"])
assert note.status == "active" and finding.status == "active" and row["authority_class"] == "agent_generated"

# %% [markdown]
# ## What the Planning agent sees
# It gets the finding, labelled as agent-generated and unverified. It never gets the private note, not even by searching for its words.

# %%
ctx = s.build("planning-agent", task=TASK, subject_ids=[sarah], profile="planning", max_tokens=600, project=PROJECT)
with s.sdk("planning-agent") as kg:
    hits = [h.summary for h in kg.search_memory("licensing rules for the plan", project=PROJECT, limit=10).hits]
print([ln for ln in ctx["context_text"].splitlines() if "licence" in ln])
assert "state licence" in ctx["context_text"] and NOTE not in ctx["context_text"]
assert not any("private to the research agent" in h for h in hits)

# %% [markdown]
# ## The same agent over MCP
# MCP is only a transport. The server holds no credential and forwards the caller's key, so the Planning agent is handed exactly the same items.

# %%
via_sdk = SdkBackend(lab.env.base_url, lab.ws.keys["planning-agent"]).call(
    "build_context", {"task": TASK, "subject_ids": [sarah], "profile": "planning", "max_tokens": 600, "project": PROJECT}
)
via_mcp = McpBackend(lab.env.mcp_url, lab.ws.keys["planning-agent"]).call(
    "build_context", {"task": TASK, "subject_ids": [sarah], "profile": "planning", "max_tokens": 600, "project": PROJECT}
)
print(
    "same items:",
    sorted(handles(via_sdk["context_text"])) == sorted(handles(via_mcp["context_text"])),
    sorted(handles(via_mcp["context_text"])),
)
assert handles(via_sdk["context_text"]) == handles(via_mcp["context_text"]) != set()

# %% [markdown]
# ## An agent with no grants
# It is registered and authenticated, but it has no right to Sarah: no memories, and none of her private facts.

# %%
none = s.build("ungranted-agent", task=TASK, subject_ids=[sarah], profile="planning", max_tokens=600)
print("memories:", len(none["memories"]), "| mentions the finding:", FINDING in none["context_text"])
assert not none["memories"] and FINDING not in none["context_text"]

# %% [markdown]
# ## Revocation
# Retiring an agent stops its key at once. Revoking one as **compromised** also puts what it shared into doubt: its published memories are
# disputed and a review item is opened for a person.

# %%
rogue_key = admin.register_principal(
    lab.ws,
    "rogue-agent",
    "agent",
    grants=[
        {"scope_kind": "subject", "scope_ref": sarah, "privileges": ["read", "propose"]},
        {"scope_kind": "project", "scope_ref": PROJECT, "privileges": ["read", "propose", "publish"]},
    ],
)
with s.sdk("rogue-agent") as kg:
    ev = kg.ingest_interaction(
        source_system="rogue",
        source_ref="r1",
        kind="document",
        content_type="text/plain",
        content=EVT005,
        occurred_at="2026-10-14T09:00:00Z",
        subject_refs=[{"entity_id": sarah}],
    )
    waits.wait_interaction(lab.ws, str(ev.event_id), key=rogue_key, timeout=240)
    bad = kg.propose_memory(
        type="semantic",
        content="Rogue claim: Sarah has already resigned.",
        scopes=[{"kind": "project", "ref": PROJECT}],
        source_event_ids=[str(ev.event_id)],
        subject_ids=[sarah],
        purpose="x",
    )
rogue_id = next(p["id"] for p in admin.get(lab.ws, "/v1/principals").json()["items"] if p["handle"] == "rogue-agent")
effects = admin.post(lab.ws, f"/v1/principals/{rogue_id}/revoke", {"reason": "compromised"})
print({k: effects[k] for k in ("keys_revoked", "shared_memories_disputed", "review_items_opened")})
status = admin.get(lab.ws, f"/v1/memories/{bad.memory_id}").json()["status"]
print("the rogue agent's finding is now:", status)
call = httpx.get(f"{lab.env.base_url}/v1/me", headers={"authorization": f"Bearer {rogue_key}"})
print("its key now answers:", call.status_code)
assert effects["shared_memories_disputed"] >= 1 and status == "disputed" and call.status_code == 401

# %%
lab.close()
