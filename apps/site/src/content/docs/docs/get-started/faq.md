---
title: FAQ
description: Why not just RAG, why both Postgres and Neo4j, which models, what it costs, how big it gets, and how honest the limits are.
sidebar:
  order: 5
---

## Why not just RAG, or a vector database?

A vector store answers "which chunks look like this question?". That is useful, and this platform has hybrid (lexical plus vector) memory search for it. But an agent acting on people and customers also needs:

- **Verified vs inferred.** A fact written by your HR system and a claim a model extracted from a chat are different things. Here they carry different `authority_class` and `evidence_status` labels, and the context says "unverified" next to the second.
- **Time.** "Sarah works at Acme" has a `valid_from` and `valid_to`, and the platform records when it learned it (`known_at`). A vector store returns last year's chunk with this year's.
- **Permissions inside the retrieval.** The authorization predicate is part of the candidate scan (SQL and Cypher), not a filter applied afterwards, and a hidden record answers the same 404 as a missing one.
- **Correction and deletion that propagate.** Reject a fact and the next context changes; delete a person and get a receipt that proves nothing remains.
- **A budget you can rely on.** The context is packed under a hard token cap, measured on the rendered text, with the unknowns listed.

If a single-user chatbot with chat history is enough for you, you do not need this.

## Why both Postgres and Neo4j?

**Postgres is authoritative.** The ledger, facts, curated memory, embeddings, policies, traces and audit all live there under row-level security. **Neo4j (through Graphiti) is a projection**: it gives you graph traversal and Graphiti's temporal extraction, and it can be rebuilt or repaired from Postgres at any time (`make reconcile`). Keeping the truth in Postgres is what makes deletion, backup and isolation tractable. See [ledger and graph](/docs/concepts/ledger-and-graph/).

## Do I need Graphiti?

It is used behind one adapter for conversation extraction and graph search. Facts that come in as **commands** are projected into the graph deterministically without it making any model call. The version is pinned ([ADR-011](/docs/reference/adrs/adr-011-pinned-stack/)).

## Which models does it need, and can I use my own provider?

All model calls go through one door, `ModelGateway`, to an **OpenAI-compatible router** you configure: chat completions with structured (JSON Schema) output for extraction, and embeddings (the embedding size is pinned at 384 dimensions). Any router that follows the [router contract](https://github.com/osogroup-blair/kg-boilerplate/blob/main/docs/router-contract.md) works. In development a mock router stands in; see [going live with a real router](/docs/guides/going-live-with-a-real-router/).

## What does it cost to run?

Infrastructure: the production reference is sized in three tiers; the small one is 8 vCPU and 16 GB, the reference one 16 vCPU and 32 GB ([deploy to production](/docs/guides/deploy-to-production/)). Model cost on the load baseline was about **$0.0004 per ingested event**, but that figure used modelled prices and a modelled router, so treat it as provisional until you measure against your own router.

## How big can a workspace get?

Search is the capacity limit, and we say so plainly. Search is an exact scan, so its p95 was 0.75 s at 10,000 memories and 2.9 s at 50,000 in the baseline. An approximate-nearest-neighbour index is the planned fix, and it should land before any workspace passes about 50,000 memories. Context builds were steadier (p95 0.35 s at 10,000, 1.65 s at 50,000). The numbers are on the [landing page](/#proof) and in the [baseline report](https://github.com/osogroup-blair/kg-boilerplate/blob/main/eval/reports/baseline-2026-10-18.md).

## How is tenant data isolated?

Every table is under row-level security keyed by workspace, with two database roles so the application cannot bypass it. In the graph, one Neo4j database holds every workspace, separated by `group_id`, and every graph result is re-checked against Postgres permissions. That is **application-enforced** isolation, and whether to move to a database or an instance per tenant is the one open production decision (DEC-06, see [open decisions](/docs/project/decisions/)). The leak suites and route walkers test it, but if you have a regulatory need for stronger isolation, plan for it.

## Can I delete someone completely?

Yes, with a receipt. A deletion request freezes the person's data (reads stop immediately), plans every derivative across Postgres, the vector index, the graph and traces, purges them, verifies with id and text scans, and issues a receipt. The ledger keeps redacted rows and the audit log keeps ids and codes, and the receipt says so. Restored backups forget again through a deletion journal. See [governance](/docs/concepts/governance/).

## Can an agent be trusted not to leak or obey injected text?

The platform neutralises structure in retrieved text (nothing can close the context envelope or forge a citation) and labels everything, but it cannot make a sentence harmless: your agent must treat the context as quoted data. The [integration contract](/docs/guides/agent-integration/) says how.

## Is it production-ready?

It is **pre-1.0**. The platform, console, SDK, MCP server, evaluation gate and examples are built and tested; CI is green. The release-candidate run, the live-router session, the SLOs and the retention windows are owner actions still to do, and DEC-06 is open. The [release checklist](/docs/project/release-checklist/) is the honest list.

## How do I contribute or report a problem?

See [contributing](/docs/project/contributing/) and the [security policy](/docs/project/security/).
