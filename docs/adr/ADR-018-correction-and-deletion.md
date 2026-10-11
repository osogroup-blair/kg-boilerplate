# ADR-018: Correction and deletion strategy for Graphiti derivatives

**Status:** Accepted  **Decision refs:** DEC-04, SEC-05, MEM-06/07, AT-16  **Evidence:** GR-07, GR-08

## Findings
- **Corrections.** Edges can be invalidated (`invalid_at/expired_at` + save), edited in place (save re-embeds), or deleted. But: invalidated edges still appear in default searches unless an `expired_at IS NULL` filter is passed; in-place edits keep no history; a deleted edge's uuid stays in `EpisodicNode.entity_edges`.
- **Episode deletion** (`remove_episode`) removes the Episodic node, edges whose *first* episode is the removed one, and entities mentioned by no other episode. It does **not**: rewrite summaries/attributes on surviving entities (derived text survives), prune the episode uuid from other edges' `episodes` (dangling refs), protect an edge a later episode also supports when the *owner* episode is removed, or clean orphan entities created outside episodes.
- `clear_data(driver, [group_id])` purges one group and leaves others intact.

## Decision
1. **Correction projection:** Postgres fact status (`active/superseded/disputed/rejected/deleted`) is authoritative and is joined or filtered by every read path; the graph edge is updated best-effort (expire or delete). Every graph search carries the `expired_at IS NULL` filter and valid-time filters. A corrected fact is a **new superseding fact**, not an in-place edit; the old text is kept in Postgres with who/why.
2. **Deletion = a verified procedure** (P8), not `remove_episode` alone: (a) block reads immediately via Postgres; (b) `remove_episode` for each source episode; (c) find surviving entities whose summary/attributes cite the subject and re-summarise or delete them; (d) re-attach or recreate edges that were collateral damage of owner removal; (e) prune dangling `episodes`/`entity_edges` references; (f) orphan sweep (`MATCH (n:Entity {group_id:$g}) WHERE NOT (n)<-[:MENTIONS]-(:Episodic) AND NOT (n)-[:RELATES_TO]-() DETACH DELETE n`); (g) verification queries (no node/edge/episode text contains the subject) and a receipt.
3. Rebuild path: for a bounded namespace, `clear_data` + replay from the ledger (the raw interactions are the source of truth).
