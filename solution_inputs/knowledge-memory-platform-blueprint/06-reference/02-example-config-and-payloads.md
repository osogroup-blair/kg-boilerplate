# Example Configuration and Contracts

**Parent:** [Blueprint index](../README.md)  
**Status:** Illustrative target contracts; not directly executable configuration until the application parser, validations and Pydantic mappings exist.

## A. Example ontology YAML

```yaml
ontology:
  id: personal-knowledge
  version: 1.0.0
  mode: strict
entity_types:
  Person:
    identity:
      key: platform_uuid
      aliases: [name]
    attributes:
      name: {type: string, required: true}
      birth_date: {type: date, required: false, sensitivity: restricted}
  Organization:
    identity: {key: platform_uuid}
    attributes:
      name: {type: string, required: true}
  Topic:
    identity: {key: platform_uuid}
    attributes:
      name: {type: string, required: true}
relationship_types:
  WORKS_AT:
    source: Person
    target: Organization
    temporal: true
    attributes:
      role: {type: string, required: false}
    rules:
      - require_source
      - preserve_valid_time
  CONSIDERING:
    source: Person
    target: Topic
    temporal: true
    rules:
      - require_source
      - require_review_if_inferred
  KNOWS:
    source: Person
    target: Person
    temporal: true
    rules: [require_source]
context_profiles:
  planning:
    max_tokens: 1200
    include: [current_facts, episodic, semantic, procedural, uncertainty]
    evidence_required: true
memory_policies:
  default:
    inferred_shared_procedural: review_required
    archive_after_days: null
```

## B. Ingest an interaction

```http
POST /v1/interactions
Authorization: Bearer <agent-or-app-token>
Idempotency-Key: demo-evt-003
Content-Type: application/json
```

```json
{
  "source_system": "conversation_app",
  "source_ref": "chat-2091-turn-14",
  "kind": "message",
  "occurred_at": "2026-10-10T19:00:00Z",
  "subject_ids": ["person_sarah", "person_michael"],
  "sensitivity": "confidential",
  "content": "Sarah told Michael she was considering starting a company but had not decided to leave Acme."
}
```

Example response:

```json
{
  "event_id": "evt_003",
  "status": "accepted",
  "status_url": "/v1/interactions/evt_003",
  "request_id": "req_7c1f"
}
```

## C. Propose a curated memory

```json
{
  "type": "episodic",
  "content": "Sarah and Michael discussed a possible software company; no resignation decision was made.",
  "subject_ids": ["person_sarah"],
  "requested_scope": {"kind": "subject", "id": "person_sarah"},
  "source_event_ids": ["evt_003"],
  "occurred_at": "2026-10-10T19:00:00Z",
  "purpose": "Future career-planning context"
}
```

Expected response may be `proposed`, `active` or `rejected` depending on the configured approval policy. `requested_scope` is advisory and always checked against authenticated rights.

## D. Build agent context

```json
{
  "task": "Help Sarah assess the implications of starting a business",
  "subject_ids": ["person_sarah"],
  "profile": "planning",
  "max_tokens": 600,
  "include": ["current_facts", "episodic", "semantic", "procedural", "uncertainty"],
  "allow_partial": false
}
```

Example response shape:

```json
{
  "context_id": "ctx_0007",
  "status": "complete",
  "generated_at": "2026-10-11T16:00:00Z",
  "context_text": "Current: Sarah is an Engineering Manager at Acme. Relevant memory: Sarah discussed a possible company with Michael but no resignation was confirmed [E:evt_003]. Unknown: available financial runway is not verified.",
  "facts": [
    {
      "fact_id": "fact_003",
      "text": "Sarah works at Acme",
      "status": "active",
      "valid_from": "2022-01-10",
      "source_ids": ["evt_001"]
    }
  ],
  "memories": [
    {
      "memory_id": "mem_003",
      "type": "episodic",
      "source_ids": ["evt_003"]
    }
  ],
  "unknowns": ["Financial runway not established"],
  "budgets": {"max_tokens": 600, "used_tokens": 103},
  "retrieval_trace_id": "trace_018"
}
```

`used_tokens` is a **sample placeholder** and must be calculated using the actual configured tokenizer in the implementation. The example response omits several full production fields for clarity.

## E. Basic data tables (conceptual)

```sql
-- Illustrative design, not a production-ready migration.
CREATE TABLE interactions (
  id UUID PRIMARY KEY,
  workspace_id UUID NOT NULL,
  source_system TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  occurred_at TIMESTAMPTZ,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_kind TEXT NOT NULL,
  payload JSONB,
  processing_status TEXT NOT NULL,
  UNIQUE (workspace_id, source_system, idempotency_key)
);
CREATE TABLE curated_memories (
  id UUID PRIMARY KEY,
  workspace_id UUID NOT NULL,
  memory_type TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  scope JSONB NOT NULL,
  source_event_ids UUID[] NOT NULL DEFAULT '{}',
  embedding_model_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Add pgvector embedding column only after choosing model dimensions.
-- Add access/scope indexes, outbox, reviews, audit and retention tables.
```

**Database implementation warning:** The eventual physical schema should normalize ACLs and many-to-many provenance where warranted; the JSON arrays here communicate contracts, not final relational design. Scope must never be enforced by application-side filtering after unscoped vector retrieval.

## F. Context formatting convention

```text
# CURRENT VERIFIED FACTS
- Sarah is an Engineering Manager at Acme. [fact_002; evt_002]

# RELEVANT EXPERIENCES
- Discussed possible entrepreneurship with Michael on 2026-10-10; no departure decision. [mem_003; evt_003]

# PREFERENCES / INSTRUCTIONS
- Explicitly prefers risk/tradeoff comparisons. [mem_004; evt_004]

# MATERIAL UNKNOWN / CONFLICT
- Financial runway is not verified.
```

The formatter should output only authorized, temporal-valid and task-relevant items and may omit empty sections. The evidence markers are stable IDs, not implied proof of correctness.
