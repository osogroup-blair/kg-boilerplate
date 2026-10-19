---
title: Troubleshooting
description: The problems that cost time before, in plain words, with the fix for each.
sidebar:
  order: 7
---

## The stack will not start, or Docker stops answering

- **Disk full.** A full host disk wedges Docker (the daemon stops answering). Keep several GB free; 20 GB is the working figure. `docker system df` shows what is using it.
- **Memory.** Give Docker about 6 GB. Neo4j is capped at a 768 MB heap in development.
- **Ports.** The stack uses 8000 (API), 5544 (Postgres), 7475 and 7688 (Neo4j), 8782 (mock router), 8010 (MCP), 9100 and 9101 (metrics), 5273 (console dev server). If something else holds one, stop it; they are fixed on purpose.
- **Start clean.** `make reset up seed` drops the volumes and rebuilds (and removes every example workspace).

## An example hangs, then times out, instead of failing

The strict mock router answers only what an example prepared. If the pipeline asks a structured question nobody prepared an answer for (`ExtractedEntities`, `ExtractedEdges`, `MemoryCandidates`, and once the workspace has nodes `NodeResolutions` and `SummarizedEntities`, plus one request named after an entity type that has attributes beyond its name), the job retries and the run only times out waiting. `Runner.settle()` waits for the outbox and names the stuck stage. When you write your own domain, every conversation needs a rule for each of those: see [add a domain](/docs/guides/add-a-domain/) and the [first-domain tutorial](/docs/tutorials/first-domain/).

## `fixture_missing` (HTTP 500) from the mock router

Same cause, seen directly: strict mode is on and no rule matched. The error names the schema. Add the rule, or turn strict mode off (`POST /__mock/strict {"enabled": false}`) to get a schema-valid placeholder, which is empty extraction.

## A context says `insufficient_budget`, or lacks the memory I expect

`insufficient_budget` means the records the profile *requires* do not fit; ask for `budgets.required_tokens` or more. A memory that exists but is not in the package is usually below the relevance threshold for the task text (the mock embeddings are lexical), or hidden because you hold no grant on its subject. Check `excluded_source_counts` in the response and use the console's Context Lab with act-as.

## `make examples` or `make mcp-up` broke my test run

`make mcp-up` recreates the API and the mock router. Never run it, `make down` or `make reset` in the middle of a test run: the integration suites share one Postgres and one Neo4j. Run them one after another.

## One integration test fails on my long-lived dev stack

`test_gr01_scoped_search_never_returns_another_groups_data` asserts that an unscoped Graphiti search sees data from both test groups, and fails on a dev Neo4j that has accumulated many example workspaces. CI's fresh database passes it. `make reset` clears the accumulation.

## 403 with "error 1010" calling a hosted router

Cloudflare rejects the default `Python-urllib` user agent. Use `httpx` or the OpenAI SDK, which the platform's own clients do.

## `make test-mock` fails on `kg_common`

It is a known problem in the spikes environment. Run the mock router's tests with `uv run pytest tools/mock_router/tests`.

## Docs or tapes look out of date

The reference pages and the recordings are generated. After an API change run `make openapi`, then `make site-gen`; to re-record the playgrounds run `make tapes` (needs `make up seed`).
