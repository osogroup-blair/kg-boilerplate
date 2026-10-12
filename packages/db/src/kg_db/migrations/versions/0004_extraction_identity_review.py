"""extracted facts and entities, evidence, mentions, identity non-matches, attribute claims, graph episodes, review items for identity and contradictions

Revision ID: 0004
Revises: 0003

Conventions follow 0001-0003: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security, column-level grants,
DB-clock timestamps. Postgres stays the system of record (ADR-017/018): Graphiti's output is a PROPOSAL that the platform records here, with its
provenance, before the projector makes the graph equal to these rows (ADR-027).

Rows that did not come from a command (extracted entities and facts) have no command: the command columns become nullable and CHECK constraints keep
them mandatory for command-sourced rows. The P2 sweeper's discovery function is dropped (the outbox is the only projection driver since P3).

Downgrade is guarded: it refuses while extracted data exists unless force_drop_extracted is set (`kg db downgrade --force-drop-extracted`).
"""

from alembic import context, op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = ["fact_evidence", "entity_mentions", "identity_non_matches", "attribute_claims", "graph_episodes"]
CLASSIFICATIONS = "('public','internal','confidential','restricted','local_only')"
AUTHORITY_ALL = "('authoritative_system','admin_asserted','user_asserted','conversation_report','agent_generated')"
AUTHORITY_OLD = "('authoritative_system','admin_asserted','user_asserted')"
FACT_STATUS = "('active','superseded','disputed','deleted','proposed','rejected')"
ENTITY_STATUS = "('active','deleted','candidate','untyped','merged')"
REVIEW_TYPES = "('policy_violation','identity_match','contradiction','graphiti_invalidation')"
COMMAND_TYPES = "('entity.create','entity.update','relationship.assert','relationship.end','review.decide','entity.merge')"
NEW_COMMAND_TYPES = "('review.decide','entity.merge')"

UP_SQL = f"""
-- ---- facts: extracted facts carry their text, an evidence status and the interaction/episode they came from -------------------------------------
ALTER TABLE facts
    ADD COLUMN fact_text text,
    ADD COLUMN evidence_status text NOT NULL DEFAULT 'verified' CHECK (evidence_status IN ('verified','unverified')),
    ADD COLUMN source_interaction_id uuid,
    ADD COLUMN source_episode_uuid text,
    ADD COLUMN decision_code text CHECK (decision_code IS NULL OR decision_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    ADD COLUMN decided_at timestamptz,
    ADD CONSTRAINT facts_source_interaction_fk FOREIGN KEY (workspace_id, source_interaction_id) REFERENCES interactions(workspace_id, id);
ALTER TABLE facts DROP CONSTRAINT facts_status_check;
ALTER TABLE facts ADD CONSTRAINT facts_status_check CHECK (status IN {FACT_STATUS});
ALTER TABLE facts DROP CONSTRAINT facts_authority_class_check;
ALTER TABLE facts ADD CONSTRAINT facts_authority_class_check CHECK (authority_class IN {AUTHORITY_ALL});
ALTER TABLE facts ALTER COLUMN source_command_id DROP NOT NULL, ALTER COLUMN last_command_id DROP NOT NULL;
ALTER TABLE facts ADD CONSTRAINT facts_command_source_check
    CHECK (source_type <> 'command' OR (source_command_id IS NOT NULL AND last_command_id IS NOT NULL));
ALTER TABLE facts ADD CONSTRAINT facts_extracted_source_check
    CHECK (source_type <> 'extracted' OR (source_interaction_id IS NOT NULL AND fact_text IS NOT NULL));
CREATE INDEX facts_interaction_idx ON facts (workspace_id, source_interaction_id) WHERE source_interaction_id IS NOT NULL;
CREATE INDEX facts_status_idx ON facts (workspace_id, status) WHERE status IN ('proposed','disputed','rejected');

-- ---- entities: extracted entities have an origin, a candidate/untyped/merged status and no command -----------------------------------------------
ALTER TABLE entities
    ADD COLUMN origin text NOT NULL DEFAULT 'command' CHECK (origin IN ('command','extracted')),
    ADD COLUMN merged_into_id uuid,
    ADD COLUMN merged_at timestamptz,
    ADD CONSTRAINT entities_merged_into_fk FOREIGN KEY (workspace_id, merged_into_id) REFERENCES entities(workspace_id, id);
ALTER TABLE entities DROP CONSTRAINT entities_status_check;
ALTER TABLE entities ADD CONSTRAINT entities_status_check CHECK (status IN {ENTITY_STATUS});
ALTER TABLE entities ALTER COLUMN created_by_command_id DROP NOT NULL, ALTER COLUMN last_command_id DROP NOT NULL;
ALTER TABLE entities ADD CONSTRAINT entities_command_origin_check
    CHECK (origin <> 'command' OR (created_by_command_id IS NOT NULL AND last_command_id IS NOT NULL));
ALTER TABLE entities ADD CONSTRAINT entities_merged_check CHECK ((status = 'merged') = (merged_into_id IS NOT NULL));
CREATE INDEX entities_merged_idx ON entities (workspace_id, merged_into_id) WHERE merged_into_id IS NOT NULL;

-- ---- commands: review decisions and merges are ledger commands (Q13) --------------------------------------------------------------------------
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES});

-- ---- evidence: why does the system believe this fact? (KGR-05) -----------------------------------------------------------------------------
CREATE TABLE fact_evidence (
    id uuid PRIMARY KEY,
    fact_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_kind text NOT NULL CHECK (source_kind IN ('command','episode')),
    command_id uuid,
    interaction_id uuid,
    episode_uuid text,
    added_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((source_kind = 'command' AND command_id IS NOT NULL AND interaction_id IS NULL)
        OR (source_kind = 'episode' AND interaction_id IS NOT NULL AND episode_uuid IS NOT NULL AND command_id IS NULL)),
    FOREIGN KEY (workspace_id, fact_id) REFERENCES facts(workspace_id, id),
    FOREIGN KEY (workspace_id, command_id) REFERENCES commands(workspace_id, id),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id)
);
CREATE UNIQUE INDEX fact_evidence_unique ON fact_evidence (fact_id, source_kind, COALESCE(command_id, interaction_id));
CREATE INDEX fact_evidence_interaction_idx ON fact_evidence (workspace_id, interaction_id) WHERE interaction_id IS NOT NULL;
INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, command_id, added_at)
    SELECT gen_random_uuid(), f.id, f.workspace_id, 'command', f.source_command_id, f.recorded_at FROM facts f WHERE f.source_command_id IS NOT NULL;

-- ---- mentions: which interactions talk about an entity ("events about Sarah") --------------------------------------------------------------
CREATE TABLE entity_mentions (
    id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    interaction_id uuid NOT NULL,
    episode_uuid text NOT NULL,
    node_uuid text NOT NULL,
    resolution text NOT NULL CHECK (resolution IN ('subject_ref','external_id','graphiti_auto','reviewer','merge','new')),
    status text NOT NULL CHECK (status IN ('accepted','needs_review','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, interaction_id, episode_uuid, node_uuid),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id)
);
CREATE INDEX entity_mentions_entity_idx ON entity_mentions (workspace_id, entity_id, created_at DESC);
CREATE INDEX entity_mentions_interaction_idx ON entity_mentions (workspace_id, interaction_id);

-- ---- "these two are different" is remembered, so the same pair never comes back as a candidate -----------------------------------------------
CREATE TABLE identity_non_matches (
    entity_a uuid NOT NULL,
    entity_b uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    decided_by uuid,
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, entity_a, entity_b),
    CHECK (entity_a < entity_b),
    FOREIGN KEY (workspace_id, entity_a) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, entity_b) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, decided_by) REFERENCES principals(workspace_id, id)
);

-- ---- attribute values a model proposed for an entity: claims, never writes (non-sensitive attributes only; enforced in code and by a test) ----
CREATE TABLE attribute_claims (
    id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    attribute_key text NOT NULL CHECK (attribute_key ~ '^[A-Za-z][A-Za-z0-9_]{{0,63}}$'),
    value jsonb NOT NULL,
    interaction_id uuid NOT NULL,
    episode_uuid text NOT NULL,
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, entity_id, attribute_key, interaction_id),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id)
);
CREATE INDEX attribute_claims_entity_idx ON attribute_claims (workspace_id, entity_id, created_at DESC);

-- ---- one row per (interaction, generation) episode: provenance, cost evidence (GR-11) and the handle P8 deletion needs -----------------------
CREATE TABLE graph_episodes (
    interaction_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation >= 0),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    episode_uuid text NOT NULL,
    ontology_version_id uuid NOT NULL,
    compiled_hash text NOT NULL,
    chat_calls integer NOT NULL DEFAULT 0,
    embedding_calls integer NOT NULL DEFAULT 0,
    prompt_tokens integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (interaction_id, generation),
    UNIQUE (workspace_id, episode_uuid),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);

-- ---- review items: identity matches, contradictions, Graphiti invalidations ----------------------------------------------------------------------
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES});
ALTER TABLE review_items ALTER COLUMN command_id DROP NOT NULL;
ALTER TABLE review_items
    ADD COLUMN subject_entity_id uuid,
    ADD COLUMN candidate_entity_id uuid,
    ADD COLUMN fact_id uuid,
    ADD COLUMN competing_fact_id uuid,
    ADD COLUMN interaction_id uuid,
    ADD COLUMN revision integer NOT NULL DEFAULT 1,
    ADD COLUMN decision text CHECK (decision IS NULL OR decision ~ '^[a-z][a-z_]{{0,63}}$'),
    ADD COLUMN rationale_code text CHECK (rationale_code IS NULL OR rationale_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    ADD COLUMN rationale_text text CHECK (rationale_text IS NULL OR length(rationale_text) <= 1000),
    ADD COLUMN decided_by uuid,
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN due_at timestamptz,
    ADD COLUMN detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    ADD CONSTRAINT review_items_subject_fk FOREIGN KEY (workspace_id, subject_entity_id) REFERENCES entities(workspace_id, id),
    ADD CONSTRAINT review_items_candidate_fk FOREIGN KEY (workspace_id, candidate_entity_id) REFERENCES entities(workspace_id, id),
    ADD CONSTRAINT review_items_fact_fk FOREIGN KEY (workspace_id, fact_id) REFERENCES facts(workspace_id, id),
    ADD CONSTRAINT review_items_competing_fk FOREIGN KEY (workspace_id, competing_fact_id) REFERENCES facts(workspace_id, id),
    ADD CONSTRAINT review_items_interaction_fk FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id),
    ADD CONSTRAINT review_items_decided_by_fk FOREIGN KEY (workspace_id, decided_by) REFERENCES principals(workspace_id, id),
    ADD CONSTRAINT review_items_source_check CHECK (
        (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
        OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
        OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL));
-- a replay of the same extraction never opens a second item for the same thing
CREATE UNIQUE INDEX review_items_open_fact_uq ON review_items (workspace_id, item_type, fact_id) WHERE status = 'open' AND fact_id IS NOT NULL;
CREATE UNIQUE INDEX review_items_open_candidate_uq ON review_items (workspace_id, item_type, candidate_entity_id)
    WHERE status = 'open' AND candidate_entity_id IS NOT NULL;
CREATE INDEX review_items_type_idx ON review_items (workspace_id, item_type, status, created_at DESC);
CREATE INDEX review_items_interaction_idx ON review_items (workspace_id, interaction_id) WHERE interaction_id IS NOT NULL;

-- ---- review gauges: aggregates across all workspaces, NO workspace ids, no subjects (like ops.backlog_stats) -------------------------------
CREATE FUNCTION ops.review_stats() RETURNS TABLE (item_type text, open_items bigint, oldest_age_seconds double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT r.item_type, count(*)::bigint, EXTRACT(EPOCH FROM now() - min(r.created_at))::double precision
      FROM public.review_items r WHERE r.status = 'open' GROUP BY r.item_type
$$;
REVOKE ALL ON FUNCTION ops.review_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.review_stats() TO kg_app;

-- ---- the P2 sweeper is gone: its cross-workspace discovery function goes with it ------------------------------------------------------------
DROP FUNCTION IF EXISTS ops.workspaces_with_pending_projection(integer);
"""  # noqa: S608

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in NEW_TENANT_TABLES
)

GRANTS_SQL = """
GRANT SELECT, INSERT ON fact_evidence TO kg_app;                                        -- evidence is append-only
GRANT SELECT, INSERT ON entity_mentions TO kg_app;
GRANT UPDATE (entity_id, resolution, status) ON entity_mentions TO kg_app;              -- a merge re-points mentions; a reviewer resolves them
GRANT SELECT, INSERT ON identity_non_matches TO kg_app;
GRANT SELECT, INSERT ON attribute_claims TO kg_app;
GRANT UPDATE (entity_id, status) ON attribute_claims TO kg_app;
GRANT SELECT, INSERT ON graph_episodes TO kg_app;
GRANT UPDATE (chat_calls, embedding_calls, prompt_tokens, completion_tokens) ON graph_episodes TO kg_app;
GRANT UPDATE (evidence_status, decision_code, decided_at, source_entity_id, target_entity_id, authority_class, valid_from, valid_from_unknown, fact_text)
    ON facts TO kg_app;
GRANT UPDATE (merged_into_id, merged_at, classification) ON entities TO kg_app;
GRANT UPDATE (entity_id) ON entity_external_ids TO kg_app;                              -- a merge moves strong identifiers to the canonical entity
GRANT UPDATE (revision, decision, rationale_code, rationale_text, decided_by, decided_at, due_at, detail, subject_entity_id) ON review_items TO kg_app;
"""


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + GRANTS_SQL)


def _force() -> bool:
    return bool(context.config.attributes.get("force_drop_extracted"))


DOWN_GUARD_SQL = """
SELECT (SELECT count(*) FROM facts WHERE source_type = 'extracted') + (SELECT count(*) FROM entities WHERE origin = 'extracted')
     + (SELECT count(*) FROM graph_episodes) AS n
"""

DOWN_SQL = f"""
DROP FUNCTION IF EXISTS ops.review_stats();
-- extracted data goes first (guarded above); commands of the new types are detached from the rows that point at them, then removed
DELETE FROM review_items WHERE item_type <> 'policy_violation';
DROP TABLE IF EXISTS graph_episodes, attribute_claims, identity_non_matches, entity_mentions, fact_evidence CASCADE;
UPDATE facts SET succeeded_by_fact_id = NULL WHERE succeeded_by_fact_id IN (SELECT id FROM facts WHERE source_type = 'extracted');
DELETE FROM facts WHERE source_type = 'extracted'
    OR source_entity_id IN (SELECT id FROM entities WHERE origin = 'extracted') OR target_entity_id IN (SELECT id FROM entities WHERE origin = 'extracted');
UPDATE facts SET last_command_id = source_command_id WHERE last_command_id IN (SELECT id FROM commands WHERE command_type IN {NEW_COMMAND_TYPES});
UPDATE entities SET last_command_id = created_by_command_id WHERE origin = 'command' AND last_command_id IN (SELECT id FROM commands WHERE command_type IN {NEW_COMMAND_TYPES});
UPDATE entities SET status = 'active', merged_into_id = NULL, merged_at = NULL WHERE status = 'merged' AND origin = 'command';
DELETE FROM entity_external_ids WHERE entity_id IN (SELECT id FROM entities WHERE origin = 'extracted');
DELETE FROM entities WHERE origin = 'extracted';
UPDATE entities SET status = 'active' WHERE status IN ('candidate','untyped');
DELETE FROM commands WHERE command_type IN {NEW_COMMAND_TYPES};

ALTER TABLE review_items DROP CONSTRAINT review_items_source_check, DROP CONSTRAINT review_items_decided_by_fk, DROP CONSTRAINT review_items_interaction_fk,
    DROP CONSTRAINT review_items_competing_fk, DROP CONSTRAINT review_items_fact_fk, DROP CONSTRAINT review_items_candidate_fk, DROP CONSTRAINT review_items_subject_fk;
DROP INDEX IF EXISTS review_items_open_fact_uq, review_items_open_candidate_uq, review_items_type_idx, review_items_interaction_idx;
ALTER TABLE review_items DROP COLUMN subject_entity_id, DROP COLUMN candidate_entity_id, DROP COLUMN fact_id, DROP COLUMN competing_fact_id,
    DROP COLUMN interaction_id, DROP COLUMN revision, DROP COLUMN decision, DROP COLUMN rationale_code, DROP COLUMN rationale_text, DROP COLUMN decided_by,
    DROP COLUMN decided_at, DROP COLUMN due_at, DROP COLUMN detail;
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN ('policy_violation'));
ALTER TABLE review_items ALTER COLUMN command_id SET NOT NULL;

ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN ('entity.create','entity.update','relationship.assert','relationship.end'));

DROP INDEX IF EXISTS entities_merged_idx;
ALTER TABLE entities DROP CONSTRAINT entities_merged_check, DROP CONSTRAINT entities_command_origin_check, DROP CONSTRAINT entities_merged_into_fk;
ALTER TABLE entities ALTER COLUMN created_by_command_id SET NOT NULL, ALTER COLUMN last_command_id SET NOT NULL;
ALTER TABLE entities DROP CONSTRAINT entities_status_check;
ALTER TABLE entities ADD CONSTRAINT entities_status_check CHECK (status IN ('active','deleted'));
ALTER TABLE entities DROP COLUMN origin, DROP COLUMN merged_into_id, DROP COLUMN merged_at;

DROP INDEX IF EXISTS facts_interaction_idx, facts_status_idx;
ALTER TABLE facts DROP CONSTRAINT facts_extracted_source_check, DROP CONSTRAINT facts_command_source_check, DROP CONSTRAINT facts_source_interaction_fk;
ALTER TABLE facts ALTER COLUMN source_command_id SET NOT NULL, ALTER COLUMN last_command_id SET NOT NULL;
UPDATE facts SET status = 'active' WHERE status = 'proposed';
DELETE FROM facts WHERE status = 'rejected';
ALTER TABLE facts DROP CONSTRAINT facts_status_check;
ALTER TABLE facts ADD CONSTRAINT facts_status_check CHECK (status IN ('active','superseded','disputed','deleted'));
ALTER TABLE facts DROP CONSTRAINT facts_authority_class_check;
ALTER TABLE facts ADD CONSTRAINT facts_authority_class_check CHECK (authority_class IN {AUTHORITY_OLD});
ALTER TABLE facts DROP COLUMN fact_text, DROP COLUMN evidence_status, DROP COLUMN source_interaction_id, DROP COLUMN source_episode_uuid,
    DROP COLUMN decision_code, DROP COLUMN decided_at;

CREATE FUNCTION ops.workspaces_with_pending_projection(p_limit integer) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT workspace_id FROM (
        SELECT workspace_id, min(projection_next_at) AS due FROM public.entities WHERE projection_status = 'pending' AND projection_next_at <= now() GROUP BY workspace_id
        UNION ALL
        SELECT workspace_id, min(projection_next_at) AS due FROM public.facts WHERE projection_status = 'pending' AND projection_next_at <= now() GROUP BY workspace_id
    ) pending GROUP BY workspace_id ORDER BY min(due) LIMIT GREATEST(p_limit, 1)
$$;
REVOKE ALL ON FUNCTION ops.workspaces_with_pending_projection(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.workspaces_with_pending_projection(integer) TO kg_app;
"""  # noqa: S608


def downgrade() -> None:
    bind = op.get_bind()
    existing = bind.exec_driver_sql(DOWN_GUARD_SQL).scalar_one()
    if existing and not _force():
        raise RuntimeError(
            f"refusing to downgrade 0004: {existing} extracted entities, facts or episodes exist. "
            "Run `kg extraction purge --workspace <slug>` first, or pass --force-drop-extracted to delete them."
        )
    op.execute(DOWN_SQL)
