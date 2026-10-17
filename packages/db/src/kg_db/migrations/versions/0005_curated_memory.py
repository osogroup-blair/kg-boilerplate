"""curated memory: records, scopes, subjects, evidence, lineage, versioned embeddings; memory review items and ledger commands

Revision ID: 0005
Revises: 0004

Conventions follow 0001-0004: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security, column-level grants,
DB-clock timestamps. A curated memory is a governed derivative (blueprint 02-domain/03): Postgres is the system of record, scopes are rows (visibility is
enforced in the SAME SQL statement that ranks, ADR-029/031), and the vector lives in its own versioned table with NO ANN index (ADR-019).

A memory's content is immutable (column grants): a change is a supersession. Memory writes are ledger commands (`memory.*`), so `commands.ontology_version_id`
becomes nullable for those types only (a workspace without an active ontology still has platform-default memory policy).

Downgrade is guarded: it refuses while memories exist unless force_drop_memories is set (`kg db downgrade --force-drop-memories`).
"""

from alembic import context, op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = ["curated_memories", "memory_scopes", "memory_subjects", "memory_evidence", "memory_lineage", "memory_embeddings"]
CLASSIFICATIONS = "('public','internal','confidential','restricted','local_only')"
AUTHORITY_ALL = "('authoritative_system','admin_asserted','user_asserted','conversation_report','agent_generated')"
REVIEW_TYPES_OLD = "('policy_violation','identity_match','contradiction','graphiti_invalidation')"
REVIEW_TYPES_NEW = "('policy_violation','identity_match','contradiction','graphiti_invalidation','memory_proposal','memory_conflict')"
MEMORY_COMMANDS = (
    "('memory.save','memory.propose','memory.extract','memory.supersede','memory.expire','memory.dispute','memory.publish','memory.review')"
)
COMMAND_TYPES_OLD = "('entity.create','entity.update','relationship.assert','relationship.end','review.decide','entity.merge')"
COMMAND_TYPES_NEW = (
    "('entity.create','entity.update','relationship.assert','relationship.end','review.decide','entity.merge',"
    "'memory.save','memory.propose','memory.extract','memory.supersede','memory.expire','memory.dispute','memory.publish','memory.review')"
)

UP_SQL = f"""
-- ---- the ledger: memory commands, and a command may exist without an ontology version only for them -----------------------------------------
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_NEW});
ALTER TABLE commands ALTER COLUMN ontology_version_id DROP NOT NULL;
-- a memory command can carry the authority of what it recorded (an extraction is a conversation report; an agent's proposal is agent-generated)
ALTER TABLE commands DROP CONSTRAINT commands_authority_class_check;
ALTER TABLE commands ADD CONSTRAINT commands_authority_class_check CHECK (authority_class IN {AUTHORITY_ALL});
ALTER TABLE commands ADD CONSTRAINT commands_ontology_required_check CHECK (ontology_version_id IS NOT NULL OR command_type LIKE 'memory.%');

-- ---- the outbox: jobs about a memory (embed, expire) ----------------------------------------------------------------------------------------
ALTER TABLE outbox_jobs DROP CONSTRAINT outbox_jobs_subject_type_check;
ALTER TABLE outbox_jobs ADD CONSTRAINT outbox_jobs_subject_type_check CHECK (subject_type IN ('interaction','command','memory'));

-- ---- scope grants: a classification ceiling is a grant too; one row per grant ------------------------------------------------------------------
ALTER TABLE principal_scopes DROP CONSTRAINT principal_scopes_scope_kind_check;
ALTER TABLE principal_scopes ADD CONSTRAINT principal_scopes_scope_kind_check CHECK (scope_kind IN ('workspace','subject','project','agent','session','classification'));
CREATE UNIQUE INDEX principal_scopes_grant_uq ON principal_scopes (workspace_id, principal_id, scope_kind, scope_ref);

-- ---- curated memories ------------------------------------------------------------------------------------------------------------------------
CREATE TABLE curated_memories (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    type text NOT NULL CHECK (type IN ('working','episodic','semantic','procedural')),
    origin text NOT NULL CHECK (origin IN ('explicit','proposal','extracted','working','published','consolidated')),
    revision integer NOT NULL DEFAULT 1,
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 10000),
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 280),
    assertion jsonb CHECK (assertion IS NULL OR (assertion ? 'predicate' AND assertion ? 'object')),
    status text NOT NULL CHECK (status IN ('proposed','active','superseded','disputed','expired','rejected','deleted')),
    importance numeric(4,3) NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
    confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    authority_class text NOT NULL CHECK (authority_class IN {AUTHORITY_ALL}),
    evidence_status text NOT NULL CHECK (evidence_status IN ('verified','unverified')),
    classification text NOT NULL CHECK (classification IN {CLASSIFICATIONS}),
    created_by uuid NOT NULL,
    command_id uuid NOT NULL,
    derived_by_alias text CHECK (derived_by_alias IS NULL OR derived_by_alias ~ '^[A-Za-z0-9_./-]{{1,64}}$'),
    pipeline_version text NOT NULL,
    prompt_version text CHECK (prompt_version IS NULL OR prompt_version ~ '^[A-Za-z0-9_.-]{{1,32}}$'),
    purpose text CHECK (purpose IS NULL OR length(purpose) BETWEEN 1 AND 200),   -- why it was saved (blueprint: "clear purpose"); scope-protected like the content, never logged
    occurred_at timestamptz,
    effective_from timestamptz NOT NULL DEFAULT now(),
    effective_until timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_at timestamptz,
    expires_at timestamptz,
    embedding_status text NOT NULL DEFAULT 'pending' CHECK (embedding_status IN ('pending','ready','skipped_by_policy','failed')),
    candidate_hmac text NOT NULL CHECK (candidate_hmac ~ '^[0-9a-f]{{64}}$'),
    run_id text CHECK (run_id IS NULL OR run_id ~ '^[A-Za-z0-9_-]{{1,64}}$'),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(summary, '') || ' ' || content)) STORED,
    UNIQUE (workspace_id, id),
    CHECK (run_id IS NULL OR type = 'working'),
    CHECK (type <> 'working' OR expires_at IS NOT NULL),
    CHECK (effective_until IS NULL OR effective_until >= effective_from),
    FOREIGN KEY (workspace_id, created_by) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, command_id) REFERENCES commands(workspace_id, id)
);
CREATE INDEX curated_memories_ws_status_type_idx ON curated_memories (workspace_id, status, type);
CREATE INDEX curated_memories_ws_recorded_idx ON curated_memories (workspace_id, recorded_at DESC, id DESC);
CREATE INDEX curated_memories_creator_idx ON curated_memories (workspace_id, created_by, recorded_at DESC);
CREATE INDEX curated_memories_tsv_idx ON curated_memories USING gin (tsv);
CREATE INDEX curated_memories_expiry_idx ON curated_memories (workspace_id, expires_at) WHERE expires_at IS NOT NULL AND status IN ('proposed','active');
-- an exact duplicate (same keyed hash of content, type, subjects and scopes) is one live memory with more evidence, never two
CREATE UNIQUE INDEX curated_memories_hmac_uq ON curated_memories (workspace_id, candidate_hmac) WHERE status IN ('proposed','active');

CREATE TABLE memory_scopes (
    memory_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    scope_kind text NOT NULL CHECK (scope_kind IN ('workspace','subject','project','agent','session')),
    scope_ref text NOT NULL CHECK (length(scope_ref) BETWEEN 1 AND 200),
    PRIMARY KEY (memory_id, scope_kind, scope_ref),
    CHECK ((scope_kind = 'workspace' AND scope_ref = '*') OR scope_kind <> 'workspace'),
    FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id)
);
CREATE INDEX memory_scopes_lookup_idx ON memory_scopes (workspace_id, scope_kind, scope_ref);

CREATE TABLE memory_subjects (
    memory_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    role text NOT NULL DEFAULT 'subject' CHECK (role IN ('subject','topic')),
    PRIMARY KEY (memory_id, entity_id),
    FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id)
);
CREATE INDEX memory_subjects_entity_idx ON memory_subjects (workspace_id, entity_id);

-- why does the platform hold this memory? Each row points at ONE thing; the content stays where it is (the ledger, the graph)
CREATE TABLE memory_evidence (
    id uuid PRIMARY KEY,
    memory_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_kind text NOT NULL CHECK (source_kind IN ('interaction','episode','fact','command','memory','external_ref')),
    interaction_id uuid,
    episode_uuid text,
    fact_id uuid,
    command_id uuid,
    source_memory_id uuid,
    external_ref text CHECK (external_ref IS NULL OR length(external_ref) BETWEEN 1 AND 256),
    added_by uuid,
    added_at timestamptz NOT NULL DEFAULT now(),
    evidence_key text GENERATED ALWAYS AS (
        source_kind || ':' || CASE source_kind
            WHEN 'interaction' THEN interaction_id::text WHEN 'episode' THEN episode_uuid WHEN 'fact' THEN fact_id::text
            WHEN 'command' THEN command_id::text WHEN 'memory' THEN source_memory_id::text ELSE external_ref END) STORED,
    CHECK ((source_kind = 'interaction' AND interaction_id IS NOT NULL)
        OR (source_kind = 'episode' AND interaction_id IS NOT NULL AND episode_uuid IS NOT NULL)
        OR (source_kind = 'fact' AND fact_id IS NOT NULL)
        OR (source_kind = 'command' AND command_id IS NOT NULL)
        OR (source_kind = 'memory' AND source_memory_id IS NOT NULL)
        OR (source_kind = 'external_ref' AND external_ref IS NOT NULL)),
    UNIQUE (memory_id, evidence_key),
    FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id),
    FOREIGN KEY (workspace_id, fact_id) REFERENCES facts(workspace_id, id),
    FOREIGN KEY (workspace_id, command_id) REFERENCES commands(workspace_id, id),
    FOREIGN KEY (workspace_id, source_memory_id) REFERENCES curated_memories(workspace_id, id)
);
CREATE INDEX memory_evidence_memory_idx ON memory_evidence (workspace_id, memory_id, added_at);
CREATE INDEX memory_evidence_interaction_idx ON memory_evidence (workspace_id, interaction_id) WHERE interaction_id IS NOT NULL;
CREATE INDEX memory_evidence_fact_idx ON memory_evidence (workspace_id, fact_id) WHERE fact_id IS NOT NULL;

-- `memory_id` replaces / was published from / was consolidated from `related_id`
CREATE TABLE memory_lineage (
    memory_id uuid NOT NULL,
    related_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    relation text NOT NULL CHECK (relation IN ('replaces','consolidated_from','published_from')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, related_id, relation),
    CHECK (memory_id <> related_id),
    FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id),
    FOREIGN KEY (workspace_id, related_id) REFERENCES curated_memories(workspace_id, id)
);
CREATE INDEX memory_lineage_related_idx ON memory_lineage (workspace_id, related_id);

-- versioned vectors (ADR-013, PV-04): a read pins ONE embedding_version, so vectors of different models are never compared. No ANN index (ADR-019).
CREATE TABLE memory_embeddings (
    memory_id uuid NOT NULL,
    embedding_version integer NOT NULL CHECK (embedding_version >= 1),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    embedding vector(384) NOT NULL,
    embedding_model_id text NOT NULL,
    embedding_dim integer NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, embedding_version),
    CONSTRAINT memory_embeddings_dim_matches CHECK (vector_dims(embedding) = embedding_dim),
    FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id)
);
CREATE INDEX memory_embeddings_version_idx ON memory_embeddings (workspace_id, embedding_version);

-- ---- review items: memory proposals and conflicts ----------------------------------------------------------------------------------------------
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_NEW});
ALTER TABLE review_items ADD COLUMN memory_id uuid, ADD COLUMN competing_memory_id uuid,
    ADD CONSTRAINT review_items_memory_fk FOREIGN KEY (workspace_id, memory_id) REFERENCES curated_memories(workspace_id, id),
    ADD CONSTRAINT review_items_competing_memory_fk FOREIGN KEY (workspace_id, competing_memory_id) REFERENCES curated_memories(workspace_id, id);
ALTER TABLE review_items DROP CONSTRAINT review_items_source_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL)
    OR (item_type IN ('memory_proposal','memory_conflict') AND memory_id IS NOT NULL));
CREATE UNIQUE INDEX review_items_open_memory_uq ON review_items (workspace_id, item_type, memory_id) WHERE status = 'open' AND memory_id IS NOT NULL;

-- ---- gauges: aggregates across workspaces, NO workspace ids and no content (like ops.review_stats) ---------------------------------------------------
CREATE FUNCTION ops.memory_stats() RETURNS TABLE (type text, status text, embedding_status text, memories bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT m.type, m.status, m.embedding_status, count(*)::bigint FROM public.curated_memories m GROUP BY m.type, m.status, m.embedding_status
$$;
REVOKE ALL ON FUNCTION ops.memory_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.memory_stats() TO kg_app;
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
GRANT SELECT, INSERT ON curated_memories TO kg_app;                                     -- content, scopes and provenance are immutable: a change is a supersession
GRANT UPDATE (status, revision, last_confirmed_at, embedding_status, expires_at, effective_until) ON curated_memories TO kg_app;
GRANT SELECT, INSERT ON memory_scopes, memory_subjects, memory_evidence, memory_lineage TO kg_app;
GRANT SELECT, INSERT ON memory_embeddings TO kg_app;
GRANT UPDATE (embedding, embedding_model_id, embedding_dim, embedded_at) ON memory_embeddings TO kg_app;   -- a re-embed to the SAME version overwrites in place
GRANT DELETE ON memory_embeddings TO kg_app;                                            -- the old version goes after a re-embed switch (PV-04 path A)
"""


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + GRANTS_SQL)


def _force() -> bool:
    return bool(context.config.attributes.get("force_drop_memories"))


DOWN_GUARD_SQL = "SELECT (SELECT count(*) FROM curated_memories) AS n"

DOWN_SQL = f"""
DROP FUNCTION IF EXISTS ops.memory_stats();
-- memory data first (guarded above); review items and commands of the new types go with it
DELETE FROM review_items WHERE item_type IN ('memory_proposal','memory_conflict');
ALTER TABLE review_items DROP CONSTRAINT review_items_source_check, DROP CONSTRAINT review_items_competing_memory_fk, DROP CONSTRAINT review_items_memory_fk;
DROP INDEX IF EXISTS review_items_open_memory_uq;
ALTER TABLE review_items DROP COLUMN memory_id, DROP COLUMN competing_memory_id;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL));
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_OLD});

DROP TABLE IF EXISTS memory_embeddings, memory_lineage, memory_evidence, memory_subjects, memory_scopes, curated_memories CASCADE;

-- job_attempts is append-only, so memory jobs cannot be deleted: they are cancelled and re-labelled (nothing consumes them once the memory tables are gone)
UPDATE outbox_jobs SET status = 'cancelled', subject_type = 'command', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
    finished_at = COALESCE(finished_at, now()) WHERE subject_type = 'memory';
ALTER TABLE outbox_jobs DROP CONSTRAINT outbox_jobs_subject_type_check;
ALTER TABLE outbox_jobs ADD CONSTRAINT outbox_jobs_subject_type_check CHECK (subject_type IN ('interaction','command'));

DELETE FROM commands WHERE command_type LIKE 'memory.%';
ALTER TABLE commands DROP CONSTRAINT commands_authority_class_check;
ALTER TABLE commands ADD CONSTRAINT commands_authority_class_check CHECK (authority_class IN ('authoritative_system','admin_asserted','user_asserted'));
ALTER TABLE commands DROP CONSTRAINT commands_ontology_required_check;
ALTER TABLE commands ALTER COLUMN ontology_version_id SET NOT NULL;
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_OLD});

DROP INDEX IF EXISTS principal_scopes_grant_uq;
DELETE FROM principal_scopes WHERE scope_kind = 'classification';
ALTER TABLE principal_scopes DROP CONSTRAINT principal_scopes_scope_kind_check;
ALTER TABLE principal_scopes ADD CONSTRAINT principal_scopes_scope_kind_check CHECK (scope_kind IN ('workspace','subject','project','agent','session'));
"""  # noqa: S608


def downgrade() -> None:
    bind = op.get_bind()
    existing = bind.exec_driver_sql(DOWN_GUARD_SQL).scalar_one()
    if existing and not _force():
        raise RuntimeError(
            f"refusing to downgrade 0005: {existing} curated memories exist. Pass --force-drop-memories to delete them (memories are not recoverable)."
        )
    op.execute(DOWN_SQL)
