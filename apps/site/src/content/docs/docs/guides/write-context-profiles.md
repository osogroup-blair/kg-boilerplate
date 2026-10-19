---
title: Write context profiles
description: Define what a context for a task contains, its caps, and the expected signals that make the engine report unknowns.
sidebar:
  order: 6
---

A **profile** is a recipe in the ontology for what a context should contain. The agent names a profile when it builds a context; the profile bounds what it can get.

## Anatomy

```yaml
context_profiles:
  renewal_prep:
    max_tokens: 800            # the cap; a request may lower it, never raise it
    max_items: 24
    include: [current_facts, relevant_facts, episodic, semantic, procedural, uncertainty]
    evidence_required: false   # true: leave unverified agent findings out
    compression: [structured, extractive]
    graph: {enabled: true, hops: 1, limit: 20}
    memory: {types: [episodic, semantic, procedural], limit_per_type: 6}
    current_facts: always
    required_current_k: 3      # the facts the package must keep even when the budget is tight
    expected_signals:          # deterministic unknowns
      - {key: renewal_date, label: renewal date, for_types: [Customer], satisfied_by: [{attribute: renewal_date}]}
```

| Field | Effect |
|---|---|
| `max_tokens`, `max_items` | Hard caps. Packing enforces the budget on the rendered text |
| `include` | Which sections to build: current and relevant facts, memory types, `uncertainty` |
| `evidence_required` | Excludes unverified agent-generated findings (the `research` built-in works this way) |
| `compression` | Allowed modes: `structured`, `extractive`, `summarized` (the last uses a model) |
| `graph`, `memory` | How far to traverse and how many memories per type |
| `required_current_k` | Facts that are kept first. If even these do not fit, the status is `insufficient_budget` |
| `expected_signals` | What a complete answer needs. Whatever is absent is listed under MATERIAL UNKNOWNS |

## Names and inheritance

A new name starts from the built-in `quick_answer` and overrides field by field. An existing built-in name (`planning`, `research`, ...) overrides the built-in. A request, or an agent's registration (`allowed_profiles`), can only **narrow** a profile: lower caps or fewer sections. Asking for more is a 422 `exceeds_profile`.

## Expected signals: unknowns without a model

`expected_signals` say what *should* be known. For each subject of a listed type (`for_types`), the signal is satisfied if an attribute exists or a fact of a given type is present. If not, the context says so ("No verified renewal date for Initech") and the agent should ask. No model is involved, so the unknown is the same every time.

## Try it before you publish

1. Edit the ontology and **validate** it.
2. **Simulate** the draft (console: Ontology → the draft → *Simulate…*, or `POST /v1/ontologies/versions/{id}/simulate`).
3. Publish, then use the console's **Context Lab** to build as a target agent and read the trace.
4. Check `budgets` and `excluded_source_counts` in the response when something is missing: `below_threshold` means it was not relevant enough to the task wording.

Ranking weights and prompts are model-affecting and need a recorded live gate; profiles are configuration. See [the context engine](/docs/concepts/context-engine/) and the [first-domain tutorial](/docs/tutorials/first-domain/).
