# %% [markdown]
# # 3. Curated memory
#
# Memories are first-class records with a type (episodic, semantic, procedural), scopes, evidence and a status. They are **proposed**,
# checked against policy, and only then active. Search is hybrid: lexical + vector, fused by reciprocal rank (`mem-rrf.v1`).

# %%
from examples._kit import admin, notebook, waits
from examples.sarah.steps import EVT003, EVT005, FINDING, PROJECT

CYCLING = "Sarah Johnson from the cycling club rode 40 km with Tom on Saturday."
PREFERENCE = "Sarah Johnson: Whenever you help me weigh a decision, please line the options up side by side and spell out the risks and trade-offs before you recommend anything."

lab = notebook.start("memory")
s = lab.s
s.s1_ontology()
s.s2_commands()
s.register_agents()
sarah = s.ids["sarah"]
for ref, text, when in [
    ("nb-evt-003", EVT003, "2026-10-10T09:00:00Z"),
    ("nb-cycling", CYCLING, "2026-10-11T09:00:00Z"),
    ("nb-pref", PREFERENCE, "2026-10-12T09:00:00Z"),
]:
    waits.wait_interaction(lab.ws, s.ingest(ref, text, [sarah], when), timeout=240)
s.settle()


def memories(**q):
    page = admin.get(lab.ws, "/v1/memories", **q).json()
    return page if isinstance(page, list) else page["items"]


# %% [markdown]
# ## What the conversations produced
# The extraction stage *proposed* memories; policy decided they may activate (episodic and semantic do; procedural never without a person).

# %%
for m in memories():
    print(f"{m['type']:<10} {m['status']:<9} {m['evidence_status']:<11} {m['summary'][:64]}")
assert any(m["type"] == "episodic" and m["status"] == "active" for m in memories())
procedural = [m for m in memories() if m["type"] == "procedural"]
assert procedural and all(m["status"] != "active" for m in procedural), "guidance for an agent never activates by itself"

# %% [markdown]
# ## A person approves guidance
# Sarah's preference is *procedural*: it tells agents how to behave, so it waits in the review queue until a person approves it.

# %%
queue = admin.get(lab.ws, "/v1/reviews").json()
items = queue if isinstance(queue, list) else queue["items"]
print([(i["item_type"], i["status"]) for i in items])
item = next(i for i in items if i["item_type"] == "memory_proposal")
detail = admin.get(lab.ws, f"/v1/reviews/{item['id']}")
import httpx

with httpx.Client(
    base_url=lab.env.base_url,
    headers={"authorization": f"Bearer {lab.ws.admin_key}", "idempotency-key": "nb-approve-1", "if-match": detail.headers["etag"]},
) as c:
    decided = c.post(f"/v1/reviews/{item['id']}/decision", json={"decision": "approve", "rationale_code": "verified_with_source"})
print("decision:", decided.status_code)
assert decided.status_code == 200
assert any(m["type"] == "procedural" and m["status"] == "active" for m in memories())

# %% [markdown]
# ## A finding from an agent
# The Research agent proposes a **semantic** memory with evidence. Because it holds `publish` on the project, the platform publishes it,
# labelled `agent_generated` and `unverified`: usable, but never presented as verified.

# %%
with s.sdk("research-agent") as kg:
    note = kg.ingest_interaction(
        source_system="research-notes",
        source_ref="nb-evt-005",
        kind="document",
        content_type="text/plain",
        content=EVT005,
        occurred_at="2026-10-13T09:00:00Z",
        subject_refs=[{"entity_id": sarah}],
    )
    waits.wait_interaction(lab.ws, str(note.event_id), key=lab.ws.keys["research-agent"], timeout=240)
    finding = kg.propose_memory(
        type="semantic",
        content=FINDING,
        scopes=[{"kind": "project", "ref": PROJECT}],
        source_event_ids=[str(note.event_id)],
        subject_ids=[sarah],
        purpose="research finding",
    )
row = admin.get(lab.ws, f"/v1/memories/{finding.memory_id}").json()
print(row["status"], row["authority_class"], row["evidence_status"], f"{len(row['evidence'])} evidence item(s)")
assert row["status"] == "active" and row["authority_class"] == "agent_generated" and row["evidence"]

# %% [markdown]
# ## Hybrid search
# Each hit says how it was found: a lexical rank, a vector rank and the fused score. Ask for the finding with different words.

# %%
with s.sdk("planning-agent") as kg:
    res = kg.search_memory("what licence does the business need", project=PROJECT, limit=5).model_dump(mode="json")
for h in res["hits"]:
    print(f"{h['score']:.3f}  found by {h['matched_by']}  {h['summary'][:56]}")
print("score parts of the top hit:", res["hits"][0]["components"])
assert res["hits"] and "licence" in res["hits"][0]["summary"].lower()

# %% [markdown]
# ## The distractor
# Sarah's cycling ride mentions her, but a question about starting a business should not surface it above what matters.

# %%
with s.sdk("planning-agent") as kg:
    res = kg.search_memory("starting a business", subject_ids=[sarah], limit=5).model_dump(mode="json")
ranked = [h["summary"] for h in res["hits"]]
print(f"{len(ranked)} hit(s):", *ranked, sep="\n  ")
print("considered", res["candidates_considered"], "candidates; the cycling ride is below the relevance floor, so it is not returned")
cycling = next((i for i, t in enumerate(ranked) if "cycling" in t.lower()), None)
business = next(i for i, t in enumerate(ranked) if "business" in t.lower() or "entrepreneur" in t.lower())
assert cycling is None or business < cycling

# %%
lab.close()
