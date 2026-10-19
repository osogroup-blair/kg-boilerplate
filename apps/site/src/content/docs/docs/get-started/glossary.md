---
title: Glossary
description: The terms used across the documentation, in plain words.
sidebar:
  order: 6
---

| Term | Meaning |
|---|---|
| **Workspace** | One tenant. Every record belongs to exactly one workspace, and the workspace comes from the caller's key, never from a request field. |
| **Principal** | An identity that can call the API: a person, an **agent** (an AI worker) or an **app** (a system of record or conversation source). Each has roles, grants, quotas and keys. |
| **Role** | `workspace_admin`, `operator`, `reviewer`, `app_writer`, `agent` or `reader`. |
| **Grant** | A scoped permission on a subject, a project or a classification, with privileges (`read`, `propose`, `publish`). |
| **Subject** | A thing facts and memories are about, usually an entity such as a person. A *subject-scoped* type hides everything about it from callers without a grant. |
| **Ontology** | The YAML schema of a domain: entity types, relationship types, rules, the memory policy and context profiles. Published as immutable versions. |
| **Entity** | A node: a person, organization, product, ticket. |
| **Relationship / fact** | An edge between two entities with valid time, evidence and authority. |
| **Command** | A deterministic, validated write from a system of record (`POST /v1/entities`, `/v1/relationships`). No model is called. |
| **Interaction** | An event (message, conversation, document) submitted for the platform to learn from. Idempotent per `Idempotency-Key`. |
| **Ledger** | The Postgres record of commands and interactions; the source of truth. |
| **Projection** | The graph in Neo4j, rebuilt from the ledger. |
| **Outbox / worker / stage** | Each interaction becomes jobs that workers run in stages (normalize, graph_ingest, memory_propose, memory_embed) with leases, retries and a dead-letter queue. |
| **Graphiti** | The library that extracts a temporal graph from text, used behind one adapter. |
| **Authority class** | How trustworthy a fact's source is, from `authoritative_system` down to `conversation_report`. |
| **Evidence status** | `verified` or `unverified`. Extracted facts and memories start unverified. |
| **Valid time (`as_of`)** | When a fact was true in the world. |
| **Record time (`known_at`)** | When the platform learned it, set by the database. |
| **Succession / supersession** | A later fact that follows an earlier one (a new role) vs a correction that replaces it. |
| **Curated memory** | Episodic, semantic, procedural and working notes with scopes, visibility, provenance and a lifecycle. |
| **Scope** | Where a memory is visible: a subject, a project, a team, a session or an agent. |
| **Working memory** | A private note for one agent run, expiring on its own. |
| **Context package** | The response of `POST /v1/context/build`: text for the prompt plus facts, memories, unknowns, conflicts, citations and the budget it respected. |
| **Profile** | A named recipe in the ontology for what a context should contain, its caps and its expected signals. |
| **Unknown** | Something a complete answer needs that is absent; computed deterministically from `expected_signals`, never guessed by a model. |
| **Trace** | Metadata about how a context was built, never its content. |
| **Review item** | A proposal waiting for a person: a contradiction, an identity match, a memory proposal, a deletion exception. |
| **Correction** | An audited ledger command that rejects, supersedes, ends or restores a fact, with an impact preview. |
| **Deletion request / receipt** | The lifecycle that freezes, plans, purges and verifies a deletion, ending with a receipt. |
| **Legal hold** | Freezes records so a deletion cannot purge them until released. |
| **Router** | The OpenAI-compatible service all model calls go through. The **mock router** answers deterministically for development. |
| **Strict mode** | The mock router fails an unanswered structured request instead of inventing an answer. |
| **MCP** | Model Context Protocol: how an MCP-capable host such as Claude Code calls the platform's nine tools. |
| **Tape** | A recording of real platform traffic that the site's playgrounds replay. |
