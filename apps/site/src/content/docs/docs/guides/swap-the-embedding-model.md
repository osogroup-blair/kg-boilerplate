---
title: Swap the embedding model
description: How embeddings are pinned and versioned, and how to change the model safely with a managed re-embed.
sidebar:
  order: 5
---

Curated memory is searched with hybrid lexical and vector ranking, so the embedding model matters, and it is **pinned**.

## What is pinned

- **384 dimensions.** pgvector columns are `vector(384)`, the Graphiti embedder is configured for 384, and the gateway asserts the length of every returned vector: a mismatch is a hard error.
- **E5-style prefixes.** Memory search embeds queries with `query: ` and stored memories with `passage: `.
- **Versions.** Every vector row stores the model id, dimension and an `embedding_version`, and reads are pinned to the current version.
- **Chunking.** Long inputs are split into windows the model accepts and mean-pooled.

## Changing the model

1. **Same dimension (384):** point the embedding purpose at the new alias, then run the managed re-embed: batches re-embed every active memory under a **new version**, search keeps reading the old version until coverage is complete, then reads switch. Runbook: [memory-reembed](/docs/operations/memory-reembed/). CLI: `kg memories reembed`.
2. **Different dimension:** this is a migration (a new column or table at the new size), not a re-embed. Plan it like a schema change.
3. **Retune thresholds.** Similarity scores differ per model: `MEMORY_VEC_MIN_COSINE` (and Graphiti's similarity thresholds) are not portable. Measure the cosine distribution of gold, distractor and filler pairs with `make eval-memory-live` and set them from that.
4. **Run the live gate** (`make eval-gate-live`): an embedding change is model-affecting.

## Failure behaviour

Embeddings have no router fallback by default. A write that needs an embedding is retried by the worker and the stage stays retryable. If the query embedding fails at search time, search **degrades to lexical-only and says so** in the response.

See [ADR-013](/docs/reference/adrs/adr-013-embeddings/) and [models and the router](/docs/concepts/models-and-router/).
