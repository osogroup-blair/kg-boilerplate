# %% [markdown]
# # 4. Building a context under a hard budget
#
# `POST /v1/context/build` is the product: the smallest sufficiently informative, authorised, **cited** context for a task, packed into a
# token budget that is enforced on the rendered text. This notebook builds the same context at different budgets, shows the deterministic
# "unknowns", reads the trace behind a package, and shows what happens to a package when the world changes afterwards.

# %%
from examples._kit import admin, notebook, waits
from examples._kit.show import show_context
from examples.sarah.steps import PROJECT, TASK

lab = notebook.start("context")
s = lab.s
s.s1_ontology()
s.s2_commands()
s.s3_conversation()
s.s5_finding()  # Sarah's world, built as in the Sarah example
sarah = s.ids["sarah"]


def build(tokens, **kw):
    return s.build("planning-agent", task=TASK, subject_ids=[sarah], profile="planning", max_tokens=tokens, project=PROJECT, **kw)


# %% [markdown]
# ## The same task at three budgets
# The packer keeps the *required* items first (current facts, the unknowns), then adds what is most relevant until the budget is full.

# %%
for tokens in (120, 300, 600):
    out = build(tokens)
    b = out["budgets"]
    print(
        f"max {tokens:>4}: used {b['used_tokens']:>3} tokens, {len(out['facts'])} facts, {len(out['memories'])} memories, {len(out['unknowns'])} unknown(s), cut: {b['truncated']}"
    )
    assert b["used_tokens"] <= tokens
small, large = build(120), build(600)
assert len(small["memories"]) < len(large["memories"])

# %% [markdown]
# ## When the required items do not fit
# Below the size of the required items the platform says so, instead of silently returning a useless fragment.

# %%
tiny = build(64)
print(tiny["status"], "- required:", tiny["budgets"]["required_tokens"], "tokens, budget:", tiny["budgets"]["max_tokens"], "tokens")
assert tiny["status"] == "insufficient_budget" and tiny["budgets"]["used_tokens"] <= 64

# %% [markdown]
# ## What the engine knows it does not know
# The planning profile *expects* a financial runway for a person. Nothing in the ontology's data provides one, so the engine reports it
# as a **material unknown**: no model decides what is missing.

# %%
full = build(600)
print(show_context(full, title="planning context"))
assert any("runway" in u["text"] for u in full["unknowns"])

# %% [markdown]
# ## The trace
# Every package has a metadata-only trace: the versions that produced it, how many candidates each source offered, how many were
# selected and why others were excluded. It holds ids and counts, never the text.

# %%
trace = admin.get(lab.ws, f"/v1/context/{full['context_id']}/trace", key=lab.ws.keys["planning-agent"]).json()
print("versions:", trace["versions"])
print(
    "candidates by source:",
    trace["counts"]["candidates_by_source"],
    "| selected:",
    trace["counts"]["selected"],
    "| exclusions:",
    trace["counts"]["exclusions"],
)
assert trace["versions"]["ranker"] == "ctx-rank.v1" and trace["counts"]["selected"] > 0

# %% [markdown]
# ## A package is explained, never stored
# A wrong claim arrives ("Michael is Sarah's manager"). A context built *now* mentions it, labelled unverified. A reviewer rejects it. Asking
# for the **old** package again re-materialises it under today's rules: the rejected item comes back as `withdrawn`.

# %%
import uuid

import httpx

michael = s.ids["michael"]
waits.wait_interaction(lab.ws, s.ingest("nb-manager", "Michael is Sarah's manager.", [sarah, michael], "2026-10-14T09:00:00Z"), timeout=240)
s.settle()
ask = dict(task="What do we know about Michael and Sarah?", subject_ids=[sarah, michael], profile="planning", max_tokens=600)
old = s.build("admin", **ask)
print([ln for ln in old["context_text"].splitlines() if "manages" in ln.lower()])
fact = next(f for f in admin.get(lab.ws, f"/v1/entities/{michael}/relationships", include="proposed").json() if f["rel_type"] == "MANAGES")
etag = f'W/"rev-{admin.get(lab.ws, f"/v1/relationships/{fact['id']}").json()["revision"]}"'
with httpx.Client(
    base_url=lab.env.base_url,
    headers={"authorization": f"Bearer {lab.ws.admin_key}", "idempotency-key": str(uuid.uuid4()), "if-match": etag},
) as c:
    assert (
        c.post(f"/v1/relationships/{fact['id']}/corrections", json={"kind": "reject", "reason_code": "incorrect_extraction"}).status_code
        == 200
    )
again = admin.get(lab.ws, f"/v1/context/{old['context_id']}").json()
print("old package re-read:", {k: again[k] for k in ("rematerialized", "withdrawn")})
assert again["rematerialized"] and again["withdrawn"] >= 1

# %%
lab.close()
