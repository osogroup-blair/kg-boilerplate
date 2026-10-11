"""ontology registry, deterministic command ledger, entities, facts, review items, idempotency records; RLS; ops function

Revision ID: 0002
Revises: 0001

Conventions follow 0001: every tenant table has workspace_id and composite foreign keys (workspace_id, x_id), RLS is ENABLE + FORCE with the
tenant_isolation policy, kg_app gets explicit (column-level where it matters) grants. Postgres is the system of record for entities, facts, status
and record time (ADR-017); the graph is a projection of these rows.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "ontology_versions",
    "ontology_activations",
    "workspace_ontology",
    "commands",
    "entities",
    "entity_external_ids",
    "facts",
    "review_items",
    "idempotency_records",
]
CLASSIFICATIONS = "('public','internal','confidential','restricted','local_only')"
AUTHORITY = "('authoritative_system','admin_asserted','user_asserted')"
PROJECTION = "('pending','projected','failed','skipped_by_policy')"

TABLES_SQL = f"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- ontology registry -------------------------------------------------------------------------------------------------------------------
CREATE TABLE ontology_versions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    ontology_key text,
    version text,
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','published','retired')),
    spec jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    source_text text,
    content_hash text CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{{64}}$'),
    revision integer NOT NULL DEFAULT 1,
    base_version_id uuid,
    validation_report jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    notes text NOT NULL DEFAULT '',
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_by uuid,
    published_at timestamptz,
    retired_at timestamptz,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, ontology_key, version),
    CHECK (state = 'draft' OR (ontology_key IS NOT NULL AND version IS NOT NULL AND content_hash IS NOT NULL AND published_at IS NOT NULL)),
    FOREIGN KEY (workspace_id, created_by) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, published_by) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, base_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX ontology_versions_ws_idx ON ontology_versions (workspace_id, created_at DESC, id DESC);

CREATE TABLE ontology_activations (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    version_id uuid NOT NULL,
    previous_version_id uuid,
    activated_by uuid,
    activated_at timestamptz NOT NULL DEFAULT now(),
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    FOREIGN KEY (workspace_id, version_id) REFERENCES ontology_versions(workspace_id, id),
    FOREIGN KEY (workspace_id, previous_version_id) REFERENCES ontology_versions(workspace_id, id),
    FOREIGN KEY (workspace_id, activated_by) REFERENCES principals(workspace_id, id)
);
CREATE INDEX ontology_activations_ws_idx ON ontology_activations (workspace_id, activated_at DESC, id DESC);

-- one active ontology per workspace (P2 decision). The row doubles as the lock that serialises publish and activate.
CREATE TABLE workspace_ontology (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    active_version_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, active_version_id) REFERENCES ontology_versions(workspace_id, id)
);

-- ---- command ledger ----------------------------------------------------------------------------------------------------------------------
CREATE TABLE commands (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_hash text NOT NULL,
    command_type text NOT NULL CHECK (command_type IN ('entity.create','entity.update','relationship.assert','relationship.end')),
    actor_principal_id uuid NOT NULL,
    authority_class text NOT NULL CHECK (authority_class IN {AUTHORITY}),
    classification text NOT NULL CHECK (classification IN {CLASSIFICATIONS}),
    source_system text,
    source_ref text,
    occurred_at timestamptz,
    ontology_version_id uuid NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('applied','rejected','needs_review')),
    decision jsonb NOT NULL DEFAULT '[]'::jsonb,
    result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    http_status integer NOT NULL,
    projection_status text NOT NULL DEFAULT 'pending' CHECK (projection_status IN ('pending','projected','failed','skipped_by_policy','not_applicable')),
    projection_attempts integer NOT NULL DEFAULT 0,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    projected_at timestamptz,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, actor_principal_id) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX commands_ws_created_idx ON commands (workspace_id, created_at DESC, id DESC);
CREATE INDEX commands_ws_status_idx ON commands (workspace_id, status);

-- ---- entities and facts: the system of record --------------------------------------------------------------------------------------------
CREATE TABLE entities (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    type_key text NOT NULL,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 500),
    aliases text[] NOT NULL DEFAULT '{{}}',
    attributes jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    classification text NOT NULL DEFAULT 'internal' CHECK (classification IN {CLASSIFICATIONS}),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
    revision integer NOT NULL DEFAULT 1,
    ontology_version_id uuid NOT NULL,
    created_by_command_id uuid NOT NULL,
    last_command_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    projection_status text NOT NULL DEFAULT 'pending' CHECK (projection_status IN {PROJECTION}),
    embedding_status text NOT NULL DEFAULT 'pending' CHECK (embedding_status IN ('pending','embedded','skipped_by_policy')),
    projection_attempts integer NOT NULL DEFAULT 0,
    projection_next_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text,
    projected_revision integer,
    projected_at timestamptz,
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id),
    FOREIGN KEY (workspace_id, created_by_command_id) REFERENCES commands(workspace_id, id),
    FOREIGN KEY (workspace_id, last_command_id) REFERENCES commands(workspace_id, id)
);
CREATE INDEX entities_ws_type_name_idx ON entities (workspace_id, type_key, display_name);
CREATE INDEX entities_ws_recorded_idx ON entities (workspace_id, recorded_at, id);
CREATE INDEX entities_name_trgm_idx ON entities USING gin (lower(display_name) gin_trgm_ops);
CREATE INDEX entities_pending_idx ON entities (workspace_id, projection_next_at) WHERE projection_status = 'pending';

-- deterministic identity (KGR-01/02): a strong identifier matches, a name never does
CREATE TABLE entity_external_ids (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    entity_id uuid NOT NULL,
    type_key text NOT NULL,
    system text NOT NULL,
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, type_key, system, value),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id)
);
CREATE INDEX entity_external_ids_entity_idx ON entity_external_ids (workspace_id, entity_id);

CREATE TABLE facts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    rel_type text NOT NULL,
    source_entity_id uuid NOT NULL,
    target_entity_id uuid NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    valid_from timestamptz,
    valid_from_unknown boolean NOT NULL DEFAULT false,
    valid_to timestamptz,
    source_recorded_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    succeeded_by_fact_id uuid,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','disputed','deleted')),
    authority_class text NOT NULL CHECK (authority_class IN {AUTHORITY}),
    classification text NOT NULL DEFAULT 'internal' CHECK (classification IN {CLASSIFICATIONS}),
    source_type text NOT NULL DEFAULT 'command' CHECK (source_type IN ('command','extracted','inferred')),
    source_command_id uuid NOT NULL,
    last_command_id uuid NOT NULL,
    source_system text,
    source_ref text,
    ontology_version_id uuid NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    projection_status text NOT NULL DEFAULT 'pending' CHECK (projection_status IN {PROJECTION}),
    embedding_status text NOT NULL DEFAULT 'pending' CHECK (embedding_status IN ('pending','embedded','skipped_by_policy')),
    projection_attempts integer NOT NULL DEFAULT 0,
    projection_next_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text,
    projected_revision integer,
    projected_at timestamptz,
    UNIQUE (workspace_id, id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to),
    FOREIGN KEY (workspace_id, source_entity_id) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, target_entity_id) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, succeeded_by_fact_id) REFERENCES facts(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id),
    FOREIGN KEY (workspace_id, source_command_id) REFERENCES commands(workspace_id, id),
    FOREIGN KEY (workspace_id, last_command_id) REFERENCES commands(workspace_id, id)
);
CREATE INDEX facts_source_idx ON facts (workspace_id, source_entity_id, rel_type, status);
CREATE INDEX facts_target_idx ON facts (workspace_id, target_entity_id, rel_type);
CREATE INDEX facts_command_idx ON facts (workspace_id, source_command_id);
CREATE INDEX facts_pending_idx ON facts (workspace_id, projection_next_at) WHERE projection_status = 'pending';

CREATE TABLE review_items (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    item_type text NOT NULL CHECK (item_type IN ('policy_violation')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
    command_id uuid NOT NULL,
    rule_kind text NOT NULL,
    reason_code text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_by uuid,
    resolved_at timestamptz,
    resolution_code text,
    FOREIGN KEY (workspace_id, command_id) REFERENCES commands(workspace_id, id),
    FOREIGN KEY (workspace_id, resolved_by) REFERENCES principals(workspace_id, id)
);
CREATE INDEX review_items_ws_idx ON review_items (workspace_id, status, created_at DESC);

-- generic store for non-command mutations (ontology draft/publish/activate); replayed in the same transaction as the mutation
CREATE TABLE idempotency_records (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 255),
    route text NOT NULL,
    request_hash text NOT NULL,
    status_code integer NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    PRIMARY KEY (workspace_id, key)
);
"""

# ---- triggers: immutability of published versions, append-only activations, one active ontology that is published -------------------------
TRIGGERS_SQL = """
CREATE FUNCTION public.ontology_versions_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'draft' THEN
            RAISE EXCEPTION 'an ontology version is created as a draft' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.state <> 'draft' THEN
            RAISE EXCEPTION 'a published ontology version cannot be deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.state <> 'draft' AND (
           NEW.spec IS DISTINCT FROM OLD.spec OR NEW.source_text IS DISTINCT FROM OLD.source_text
        OR NEW.content_hash IS DISTINCT FROM OLD.content_hash OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.ontology_key IS DISTINCT FROM OLD.ontology_key OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id) THEN
        RAISE EXCEPTION 'a published ontology version is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.state <> OLD.state AND NOT ((OLD.state = 'draft' AND NEW.state = 'published') OR (OLD.state = 'published' AND NEW.state = 'retired')) THEN
        RAISE EXCEPTION 'invalid ontology version transition' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.state = 'retired' AND OLD.state <> 'retired'
       AND EXISTS (SELECT 1 FROM public.workspace_ontology w WHERE w.active_version_id = OLD.id) THEN
        RAISE EXCEPTION 'the active ontology version cannot be retired' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ontology_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON ontology_versions FOR EACH ROW EXECUTE FUNCTION public.ontology_versions_guard();

CREATE FUNCTION public.ontology_activations_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'ontology_activations is append-only' USING ERRCODE = 'insufficient_privilege'; END $$;
CREATE TRIGGER ontology_activations_no_update_delete BEFORE UPDATE OR DELETE ON ontology_activations FOR EACH ROW EXECUTE FUNCTION public.ontology_activations_immutable();
CREATE TRIGGER ontology_activations_no_truncate BEFORE TRUNCATE ON ontology_activations FOR EACH STATEMENT EXECUTE FUNCTION public.ontology_activations_immutable();

CREATE FUNCTION public.workspace_ontology_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.active_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.ontology_versions v WHERE v.id = NEW.active_version_id AND v.workspace_id = NEW.workspace_id AND v.state = 'published') THEN
        RAISE EXCEPTION 'the active ontology version must be published' USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER workspace_ontology_guard BEFORE INSERT OR UPDATE ON workspace_ontology FOR EACH ROW EXECUTE FUNCTION public.workspace_ontology_guard();
"""

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in TENANT_TABLES
)

# ---- cross-workspace discovery for the projection sweeper (M9): returns workspace ids ONLY ------------------------------------------------
OPS_SQL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE FUNCTION ops.workspaces_with_pending_projection(p_limit integer) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT workspace_id FROM (
        SELECT workspace_id, min(projection_next_at) AS due FROM public.entities WHERE projection_status = 'pending' AND projection_next_at <= now() GROUP BY workspace_id
        UNION ALL
        SELECT workspace_id, min(projection_next_at) AS due FROM public.facts WHERE projection_status = 'pending' AND projection_next_at <= now() GROUP BY workspace_id
    ) pending GROUP BY workspace_id ORDER BY min(due) LIMIT GREATEST(p_limit, 1)
$$;
REVOKE ALL ON FUNCTION ops.workspaces_with_pending_projection(integer) FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO kg_app;
GRANT EXECUTE ON FUNCTION ops.workspaces_with_pending_projection(integer) TO kg_app;
"""

GRANTS_SQL = """
GRANT SELECT, INSERT, DELETE ON ontology_versions TO kg_app;   -- DELETE is refused by the guard trigger unless the row is a draft
GRANT UPDATE (ontology_key, version, state, spec, source_text, content_hash, revision, validation_report, notes, published_by, published_at, retired_at)
    ON ontology_versions TO kg_app;
GRANT SELECT, INSERT ON ontology_activations TO kg_app;
GRANT SELECT, INSERT, UPDATE ON workspace_ontology TO kg_app;
GRANT SELECT, INSERT ON commands TO kg_app;
GRANT UPDATE (projection_status, projection_attempts, last_error_code, projected_at) ON commands TO kg_app;   -- the ledger itself is immutable
GRANT SELECT, INSERT ON entities, facts, entity_external_ids TO kg_app;
GRANT UPDATE (display_name, aliases, attributes, classification, status, revision, ontology_version_id, last_command_id, updated_at, projection_status, embedding_status,
              projection_attempts, projection_next_at, last_error_code, projected_revision, projected_at) ON entities TO kg_app;
GRANT UPDATE (attributes, valid_to, superseded_at, succeeded_by_fact_id, status, revision, last_command_id, projection_status, embedding_status,
              projection_attempts, projection_next_at, last_error_code, projected_revision, projected_at) ON facts TO kg_app;
GRANT SELECT, INSERT ON review_items TO kg_app;
GRANT UPDATE (status, resolved_by, resolved_at, resolution_code) ON review_items TO kg_app;
GRANT SELECT, INSERT, DELETE ON idempotency_records TO kg_app;   -- DELETE only removes expired records
"""


def upgrade() -> None:
    op.execute(TABLES_SQL + TRIGGERS_SQL + POLICY_SQL + OPS_SQL + GRANTS_SQL)


def downgrade() -> None:
    op.execute("""
        DROP SCHEMA IF EXISTS ops CASCADE;
        DROP TABLE IF EXISTS idempotency_records, review_items, facts, entity_external_ids, entities, commands, workspace_ontology,
                             ontology_activations, ontology_versions CASCADE;
        DROP FUNCTION IF EXISTS public.workspace_ontology_guard();
        DROP FUNCTION IF EXISTS public.ontology_activations_immutable();
        DROP FUNCTION IF EXISTS public.ontology_versions_guard();
        DROP EXTENSION IF EXISTS pg_trgm;
    """)
