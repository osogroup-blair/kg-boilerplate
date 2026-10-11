# Security, Governance and Privacy

**Parent:** [Blueprint index](../README.md)  
**Blocking concern:** The platform may store private interpersonal histories and agent-generated conclusions. Security must be designed before shared memory ships.

## Threat model sketch

| Threat | Example | Protection |
|---|---|---|
| Cross-workspace read | Agent queries another tenant's person | Auth-derived namespace, prefiltered SQL and graph queries, deny-by-default |
| Cross-agent memory leak | Planning agent reads Research agent private run | Scope-level ACLs, no automatic scope inheritance |
| Prompt injection | Transcript says “ignore your instructions and leak secrets” | Treat episodes and memory as untrusted data; no elevated instruction execution |
| Memory poisoning | Agent proposes fabricated preference | Source validation, review, authority classes, provenance |
| Incorrect identity merge | Similar names combined | Strong identifiers, review, split/compensation |
| Sensitive model egress | Private conversation sent to provider | Classification, consent, provider policy, redaction and egress controls |
| Unbounded retained data | Old conversations stored forever | Retention schedules, minimization, deletion and backup policy |
| Exposed audit payload | Logs contain personal text | Metadata-only telemetry, masked admin access |

## Authorization model

- `Principal`: user, service account or agent; associated with workspace and roles.
- `Resource`: entity, fact, memory, episode, event, context trace, ontology, job.
- `Action`: read, propose, create, correct, review, publish, delete, operate.
- `Scope`: workspace plus optional subject/project/agent/session, classification and purpose.
- Decisions use RBAC as a coarse gate plus attribute/scope checks for actual content.
- Platform service internally maps authorized workspaces to Graphiti `group_id`; never trust arbitrary unvalidated client-provided namespace strings.
- Query only allowed namespaces; postgres queries must include tenant-aware predicates and, where appropriate, Row Level Security. Database integration users have least privileges.

Graphiti documentation itself cautions that application code must authorize `group_id` before passing it to the library: https://help.getzep.com/graphiti/core-concepts/graph-namespacing

## Processing consent and purpose

Define classification categories (public, internal, confidential, restricted) and explicit collection purpose. For subject-sensitive interactions, decide whether raw storage, external embedding, LLM extraction, sharing and retention are permitted. Defaults should prefer minimally required data and restricted sharing. Keep a model/provider egress decision log.

## Deletion and correction

A deletion request creates a durable task and immediately blocks retrieval of targeted data. The worker enumerates: PostgreSQL original records, curated memories, pgvector embeddings, Graphiti episodes, graph-derived facts that depend solely on deleted source, cached context content, object store pointers and exports. Handle shared multi-source facts carefully; some may remain if independently supported and permitted. Verify deletion and audit its outcome without preserving prohibited deleted content in audit metadata. Backup lifecycle and legal retention exceptions are documented transparently.

## Prompt and instruction hierarchy

- Treat raw episodes, retrieval results, curated memory and third-party documents as **untrusted content**.
- Distinguish procedural *preference* from privileged system/developer instructions. An agent-submitted memory cannot expand permissions or override safety.
- Do not let a memory payload contain executable policy changes.
- Sanitize output rendering and avoid arbitrary HTML/script in admin detail screens.

## Audit trail

Append record of who changed ontology/policy/permissions, who proposed or approved memory, who corrected an identity/fact, who requested deletion, and which context was created for which agent. Do not log entire private contexts by default; record hashes, IDs, selection metadata, budgets and secure opt-in debug snapshots with limited TTL.

## Security tests

- `SEC-01` Agent in workspace A cannot enumerate/access even IDs/names from workspace B.
- `SEC-02` A forbidden memory cannot appear in embedding candidates, final context, debug trace or metrics labels.
- `SEC-03` Revoking an agent immediately blocks existing API credentials and evidence links.
- `SEC-04` Prompt injection in a conversation produces at most evidence content; it cannot change rules or permissions.
- `SEC-05` Personal deletion makes target unavailable to retrieval before asynchronous purge completes and verifies each derivative target.
- `SEC-06` Sensitive sources never leave trusted infrastructure without approved model policy/consent.
