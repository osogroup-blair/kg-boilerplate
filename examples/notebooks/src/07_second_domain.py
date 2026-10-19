# %% [markdown]
# # 7. A second domain, with configuration only
#
# The same platform serves a **customer/project CRM**. Nothing in `apps/`, `packages/` or `sdk/` knows about customers: the domain is an
# ontology file, some data and a benchmark. This notebook builds it, prints a renewal-prep package, and sets it next to Sarah's.

# %%
import yaml

from examples._kit import notebook
from examples._kit.env import ROOT
from examples._kit.show import show_context
from examples.domains.crm.run import ONTOLOGY, Crm

lab = notebook.start("crm", ontology=ONTOLOGY, scenario=Crm)
crm = lab.s

# %% [markdown]
# ## The whole domain is one file (plus data)

# %%
spec = yaml.safe_load(ONTOLOGY.read_text())
print("entity types      :", sorted(spec["entity_types"]))
print("relationship types:", sorted(spec["relationship_types"]))
print("context profiles  :", sorted(spec["context_profiles"]))
print("signals the renewal profile expects:", [s["key"] for s in spec["context_profiles"]["renewal_prep"]["expected_signals"]])

# %% [markdown]
# ## Build the world
# System-of-record commands (with a backdated job change), then agents with different grants, then meeting notes and a support ticket
# through extraction. The same endpoints and the same pipeline as Sarah's.

# %%
for step in (crm.setup, crm.commands, crm.agents, crm.conversations):
    print(step.__name__, "->", {k: v for k, v in step().items() if k != "ontology"})

# %% [markdown]
# ## A renewal-prep package for Globex
# The account manager sees Globex's people, the support ticket, and the **account plan** that was published into the `acct-globex` project.

# %%
globex = crm.build(
    "globex-am",
    task="Prepare the Globex renewal",
    subject_ids=[crm.ids["globex"], crm.ids["dana"]],
    profile="renewal_prep",
    max_tokens=800,
    project="acct-globex",
)
print(show_context(globex, title="Globex renewal_prep"))
assert "8% uplift" in globex["context_text"] and "TCK-4411" in globex["context_text"] and not globex["unknowns"]

# %% [markdown]
# ## What is missing is reported as missing
# Initech has no renewal date or budget owner on file. The profile's `expected_signals` turn that into unknowns, the same mechanism that
# reported Sarah's missing financial runway.

# %%
initech = crm.build(
    "initech-am",
    task="Prepare the Initech renewal",
    subject_ids=[crm.ids["initech"], crm.ids["priya"]],
    profile="renewal_prep",
    max_tokens=800,
    project="acct-initech",
)
print([u["text"] for u in initech["unknowns"]])
assert len(initech["unknowns"]) == 2

# %% [markdown]
# ## Scope keeps customers apart
# Support sees the ticket but not the account plan; Initech's account manager asking about Globex gets none of its people or plan.

# %%
support = crm.build(
    "support-agent",
    task="Dana says the Atlas export keeps timing out",
    subject_ids=[crm.ids["globex"], crm.ids["dana"]],
    profile="support_triage",
    max_tokens=500,
)
other = crm.build(
    "initech-am",
    task="What is going on with Globex",
    subject_ids=[crm.ids["globex"]],
    profile="account_review",
    max_tokens=600,
    project="acct-initech",
)
print("support sees the ticket:", "TCK-4411" in support["context_text"], "| the plan:", "uplift" in support["context_text"])
print("Initech's manager sees Dana Reyes:", "Dana Reyes" in other["context_text"])
assert "TCK-4411" in support["context_text"] and "uplift" not in support["context_text"] and "Dana Reyes" not in other["context_text"]

# %% [markdown]
# ## The benchmark
# Nine frozen cases score recall, leakage, the token budget and attribution. The same thresholds as Sarah's: leakage 0, budget and attribution 100%.

# %%
result = crm.evaluate()
print({k: round(v, 2) for k, v in result["metrics"].items()}, "->", "PASS" if result["passed"] else "FAIL")
for case in result["cases"]:
    print(("ok  " if case["ok"] else "FAIL"), case["id"], case["tokens"])
assert result["passed"]

# %% [markdown]
# ## Same platform, different domain
# The packages above have the same shape as Sarah's; only the content differs.

# %%
print("keys identical:", sorted(globex) == sorted(initech))
print(
    "CRM-specific files:",
    sorted(str(p.relative_to(ROOT)) for p in (ROOT / "examples/domains/crm").glob("*") if p.is_file() and p.suffix in (".yaml", ".py")),
)

# %%
lab.close()
