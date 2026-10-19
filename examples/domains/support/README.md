# Third domain: a support desk (the tutorial domain)

```bash
make up seed
make demo-support   # about 20 s: the world, three printed triage packages, 8 pass/fail steps
```

This is the domain built step by step in the site's tutorial, [Model your own domain](https://github.com/osogroup-blair/kg-boilerplate/blob/main/apps/site/src/content/docs/docs/tutorials/first-domain.md). It is **configuration and data only**: a test fails if platform source ever mentions it.

| File | What it is |
|---|---|
| `ontology.yaml` | Customer, Requester (subject-scoped), Engineer, Product, Ticket; four relationships (`ASSIGNED_TO` has the `succession` rule); the `ticket_triage` profile with an expected `severity` signal |
| `events/timeline.json` | system-of-record commands, a reassignment, two support-call notes |
| `run.py` | fresh workspace, publish, register a triage agent and an agent with no grants, ingest, build, assert |

What the eight steps prove: commands make no model call; a new assignment ends the previous one (`as_of 2026-10-05` still shows the old engineer); the triage agent sees Alex Kim's ticket and call note; TCK-102 has no `severity`, so the engine reports it as an **unknown**; a requester the agent holds no grant on appears nowhere in its package; an agent with no grants learns nothing.

The offline model answers come from `examples/domains/crm/mock_rules.py::rules_for`, a generic helper that turns a timeline into the structured answers the strict mock router needs.
