# Platform Brief

**Parent:** [Blueprint index](../README.md)  
**Status:** Proposed / baseline requirements

## Problem

AI agents usually operate with fragmented, repeated context: task prompts, chat histories, disconnected documents and unstructured saved facts. They often re-research facts, use superseded information, fail to share validated findings, or receive too much irrelevant context.

## Vision

Provide a configurable foundation that understands people, organizations, projects, things, and relationships over time, remembers consequential interactions, and supplies each agent with compact, evidence-backed context for its current task.

## Primary actors

| Actor | Goal | Interface |
|---|---|---|
| Application developer | Reuse knowledge/memory without building it | HTTP API, Python SDK, optional MCP |
| AI agent | Retrieve task-relevant context and publish findings | Context build, memory proposal, episode ingestion |
| Workspace admin | Define ontology, policies, permissions, quality rules | Admin UI |
| Knowledge reviewer | Resolve conflicts, mistaken identity, suspect memories | Review queue, entity inspector |
| Platform operator | Keep pipelines reliable and costs visible | Operations dashboard, logs/metrics |
| End subject | Inspect/correct/delete personal data where permitted | End-user-facing service APIs; optional later UI |

## Core job stories

- **Before action:** Agent requests a context package given its task, subject, scope and token budget.
- **After action:** Agent submits durable findings with evidence, without directly mutating authoritative truth.
- **On interaction:** The application records an event; the platform extracts graph facts and curated memories according to policies.
- **On change:** The platform can indicate what was believed at a prior date and what is presently supported.
- **On correction:** Admin/reviewer fixes an entity or fact and can inspect the impact on retrieval.
- **On configuration:** Admin publishes an ontology and policy version after validation and impact checks.

## Success definition

An end-to-end demonstration has two independently operating agents. Agent A learns Sarah changed her career goal; the memory persists with its source and privacy scope. Agent B receives that goal when relevant, sees it as provisional if appropriate, excludes obsolete facts, and stays within a hard context token budget. An unauthorized agent cannot retrieve it. A correction or deletion propagates across the relevant stores.

## Product requirements

- `P-01` Reusable across knowledge domains, not bound to the Sarah sample.
- `P-02` Admin-configurable ontology and knowledge rules, with immutable published versions.
- `P-03` Typed temporal facts with provenance, review, correction and history.
- `P-04` First-class memory creation, search, consolidation and access scopes.
- `P-05` Shared multi-agent context API with concise, attributable results.
- `P-06` Reliable ingestion, retries, observability, operations and recovery.
- `P-07` Tenant/workspace-safe authorization with sensitive data safeguards.
- `P-08` Evaluable retrieval quality and auditable context decisions.

## Explicit non-goals for v1

- General-purpose autonomous agent orchestration; the platform supplies memory/context, not full agent task management.
- Designing arbitrary executable business workflows in an admin scripting language.
- Real-time CRDT collaboration or distributed knowledge synchronization.
- Treating an LLM's inferred claim as an unquestioned truth.
- Native multimodal indexing for audio/video; files may be accepted via text extraction adapters later.
- Fully automated ontology migrations for any schema change.
- Guaranteed accurate reconstruction of every historical graph state without validation and snapshot strategy.

## Measures to establish in evaluation

Relevance / recall on a labeled set; unsupported statement rate; stale-fact use; access-control leakage; context token count; median and p95 read latency; write-to-search freshness; correction/deletion propagation; per-event LLM cost. Establish target values in [acceptance and evaluations](../05-delivery/02-acceptance-and-evaluations.md) before production approval.
