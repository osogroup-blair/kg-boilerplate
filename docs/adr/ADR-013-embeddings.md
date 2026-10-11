# ADR-013: Embeddings

**Status:** Provisional (pending LV-05 and GR-11)  **Decision refs:** DEC-08 (part), DEC-02
**Evidence:** G-04, PV-04, GR-02, GR-10, MR-01 · `docs/router-contract.md` §6

## Context
`oso/embedding` resolves to `intfloat/multilingual-e5-small` on Vertex (us-central1 only, no fallback, router-advertised 8192-token context, model limit 512).

## Decision
1. **384 dimensions.** pgvector columns are `vector(384)`; Graphiti's embedder uses `embedding_dim=384`; the gateway asserts the length of every returned vector (G-04). A mismatch is a hard error.
2. **E5 prefixes.** Platform memory search embeds queries with `query: ` and stored memories with `passage: ` (asymmetric). **Graphiti's embedder cannot distinguish query from passage**, so its wrapper uses `query: ` on both sides (E5's recommendation for symmetric tasks). The wrapper also handles Graphiti passing a one-element list (found in GR-02).
3. **Chunking.** Inputs are split into ≤ (512 − prefix − 2) token windows by the gateway and the chunk vectors are mean-pooled and re-normalised; the result records `chunked=True`. This never relies on the router to truncate (LV-05 will show what it does).
4. **Versioning.** Every vector row stores `embedding_model_id`, `embedding_dim`, `embedding_version` with `CHECK (vector_dims(embedding) = embedding_dim)`. Same-dimension model changes are re-embedded in batches under a new version and reads are pinned to the current version; dimension changes use add-column / backfill / swap (PV-04). **Graphiti stores its own embeddings in Neo4j**, so a model change is one "embedding generation" change covering both stores.
5. **Single deployment risk.** Embeddings have no router fallback; a write that needs an embedding is retried by the worker and the stage stays retryable (`*_failed` is not terminal). Optional router change request: a second 384-dim deployment.
6. **Thresholds are not portable.** Graphiti's `sim_min_score=0.6` is tuned for other embedding models; E5 similarities cluster high, so thresholds must be re-tuned from live data (LV-05 records related vs unrelated cosine).

## Live follow-up
LV-05: observed dimension, >512-token behaviour (silent truncation vs error), cosine ranges. If E5-small proves too weak for entity matching (GR-11), the fix is the version mechanism above plus a router change, not a code redesign.
