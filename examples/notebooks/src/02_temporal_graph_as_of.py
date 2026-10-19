# %% [markdown]
# # 2. The temporal graph: `as_of` and `known_at`
#
# A fact has two clocks. **Valid time** (`as_of`) is when it was true in the world. **Record time** (`known_at`) is when the platform learned
# it. This notebook asks the same question at different points on both clocks, draws the graph at two moments, and walks a fact back to
# its evidence.

# %%
import time
from datetime import UTC, datetime

import matplotlib.pyplot as plt
import networkx as nx

from examples._kit import admin, notebook, waits
from examples.sarah.steps import EVT003

lab = notebook.start("temporal")
s = lab.s
s.s1_ontology()
s.s2_commands()  # Sarah's jobs, by command: Software Engineer from 2022-01-10, Engineering Manager from 2024-06-01
sarah = s.ids["sarah"]


def facts(**query):
    return admin.get(lab.ws, f"/v1/entities/{sarah}/relationships", **query).json()


def employment(**query):
    return [
        (f["attributes"].get("role"), f["valid_from"][:10], (f["valid_to"] or "open")[:10])
        for f in facts(**query)
        if f["rel_type"] == "WORKS_AT"
    ]


# %% [markdown]
# ## Valid time: where did Sarah work *then*?
# The second role **succeeds** the first (a rule in the ontology), so by default only the current one is current.

# %%
for label, when in [("2023-01-01", "2023-01-01T00:00:00Z"), ("2025-01-01", "2025-01-01T00:00:00Z"), ("now", None)]:
    print(f"as_of {label:<11}", employment(**({"as_of": when} if when else {})))
assert employment(as_of="2023-01-01T00:00:00Z")[0][0] == "Software Engineer"
assert employment(as_of="2025-01-01T00:00:00Z")[0][0] == "Engineering Manager"

# %% [markdown]
# ## Commands vs. extracted facts
# Commands come from a system of record and are authoritative. A conversation produces *extracted* facts that start **unverified**.
# Remember the moment before the conversation: we use it for `known_at` below.

# %%
before_conversation = datetime.now(UTC).isoformat()
time.sleep(1.2)
event = s.ingest("nb-evt-003", EVT003, [sarah, s.ids["michael"], s.ids["topic"]], "2026-10-10T09:00:00Z")
waits.wait_interaction(lab.ws, event, timeout=240)
s.settle()
for f in facts(include="history"):
    print(f"{f['rel_type']:<12} {f['source_type']:<10} {f['authority_class']:<20} {f['evidence_status']:<11} {f['status']}")

# %% [markdown]
# ## Record time: what did the platform *know*?
# Just before the conversation the platform did not know Sarah was considering anything. `known_at` answers as of a moment in the past.

# %%
considering_now = [f for f in facts() if f["rel_type"] == "CONSIDERING"]
considering_then = [f for f in facts(known_at=before_conversation) if f["rel_type"] == "CONSIDERING"]
print("known now:", len(considering_now), "| known before the conversation:", len(considering_then))
assert considering_now and not considering_then

# %% [markdown]
# ## A job change in 2027
# The source says Sarah left Acme on 2027-02-01 and that it learned this on 2027-02-05 (`source_recorded_at`, informational). Ending the
# fact keeps history: the old role is not deleted, it becomes *historical*.

# %%
admin.post(
    lab.ws,
    f"/v1/relationships/{s.ids['em']}/end",
    {"valid_to": "2027-02-01T00:00:00Z", "source_recorded_at": "2027-02-05T09:00:00Z", "source_system": "hr", "source_ref": "evt_006"},
    key=lab.ws.keys["conversation-app"],
)
for label, when in [("2026-12-01", "2026-12-01T00:00:00Z"), ("2027-03-01", "2027-03-01T00:00:00Z")]:
    print(f"as_of {label}:", employment(as_of=when) or "no current employer")
assert employment(as_of="2026-12-01T00:00:00Z") and not employment(as_of="2027-03-01T00:00:00Z")

# %% [markdown]
# ## The graph at two moments


# %%
def draw(ax, when):
    g = admin.get(lab.ws, f"/v1/entities/{sarah}/graph", as_of=when).json()
    nx_g = nx.DiGraph()
    names = {n["id"]: n["name"] for n in g["nodes"]}
    for e in g["edges"]:
        if e["phase"] == "current":
            nx_g.add_edge(names[e["source_id"]], names[e["target_id"]], label=e["rel_type"])
    nx_g.add_node("Sarah Johnson")
    pos = nx.spring_layout(nx_g, seed=3)
    nx.draw(nx_g, pos, ax=ax, with_labels=True, node_color="#cfe3ff", node_size=1800, font_size=8)
    nx.draw_networkx_edge_labels(nx_g, pos, edge_labels=nx.get_edge_attributes(nx_g, "label"), ax=ax, font_size=7)
    ax.set_title(f"as_of {when[:10]}")
    return nx_g


fig, axes = plt.subplots(1, 2, figsize=(10, 3.4))
before, after = draw(axes[0], "2026-12-01T00:00:00Z"), draw(axes[1], "2027-03-01T00:00:00Z")
plt.show()
assert ("Sarah Johnson", "Acme") in before.edges and ("Sarah Johnson", "Acme") not in after.edges

# %% [markdown]
# ## Provenance: why does the platform believe this?
# Every fact lists the commands and episodes behind it (metadata only).

# %%
for fact in [f for f in facts(include="history") if f["rel_type"] in ("WORKS_AT", "CONSIDERING")]:
    why = admin.get(lab.ws, f"/v1/relationships/{fact['id']}/evidence").json()
    print(f["rel_type"], "<-", [(e["kind"], e["source_system"], e["source_ref"]) for e in why])
    assert why

# %%
lab.close()
