"""governance: corrections, deletion orchestration and the purge gate, legal holds, consolidation runs, ontology simulations, workspace settings

Revision ID: 0008
Revises: 0007

Conventions follow 0001-0007: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security, column-level grants, DB-clock timestamps.

* **The purge gate (phase-8 plan U5).** Until now the application role could not delete or rewrite evidence at all, because it was never granted DELETE. Deletion needs a
  controlled exception, and the exception lives in the database: a BEFORE DELETE / UPDATE trigger on every immutable table calls `public.purge_gate()`, which says yes only when
  the transaction named a deletion request (`set_config('app.purge_request_id', ...)`), that request is in state `purging` in the SAME workspace as the row, and a
  `deletion_items` row lists exactly this row with the matching action (or the request is a workspace deletion). `audit_log` stays fully immutable (ids and codes only).
* **Freeze = a status** (`deletion_pending`) on entities, facts and memories (interactions already had it); the existing status filters then hide the item everywhere at once.
* **Governance tables:** `deletion_requests`, `deletion_items`, `deletion_manifests` (encrypted, removed at completion), `deletion_tombstones`, `deletion_suppressions`
  (HMACs of external ids), `legal_holds`, `corrections`, `attribute_corrections`, `entity_lineage`, `consolidation_runs`, `ontology_simulations`, `workspace_settings`;
  `platform_deletion_receipts` has no RLS and no grant to the application role (only the `ops` functions reach it).
* **Facts and entities made by a correction** carry `source_type = 'correction'` / `origin = 'correction'`; new review item types; new command types.

Downgrade is guarded: it refuses while a deletion request is not terminal, and (unless `force_drop_governance`, which keeps those rows as ordinary ones) while correction-made rows exist.
"""

from alembic import context, op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = [
    "deletion_requests",
    "deletion_items",
    "deletion_manifests",
    "deletion_tombstones",
    "deletion_suppressions",
    "legal_holds",
    "corrections",
    "attribute_corrections",
    "entity_lineage",
    "consolidation_runs",
    "ontology_simulations",
    "workspace_settings",
]

REVIEW_TYPES_OLD = (
    "('policy_violation','identity_match','contradiction','graphiti_invalidation','memory_proposal','memory_conflict','source_compromised')"
)
REVIEW_TYPES_NEW = (
    "('policy_violation','identity_match','contradiction','graphiti_invalidation','memory_proposal','memory_conflict','source_compromised',"
    "'deletion_exception','consolidation_proposal','correction_request','authoritative_override')"
)
COMMAND_TYPES_OLD = (
    "('entity.create','entity.update','relationship.assert','relationship.end','review.decide','entity.merge',"
    "'memory.save','memory.propose','memory.extract','memory.supersede','memory.expire','memory.dispute','memory.publish','memory.review',"
    "'principal.register','principal.update','principal.revoke','key.create','key.revoke','grant.add','grant.remove')"
)
GOVERNANCE_COMMANDS = (
    "'correction.fact_reject','correction.fact_supersede','correction.fact_end','correction.fact_restore','correction.attribute','correction.rename',"
    "'correction.memory','entity.split','memory.consolidate','settings.update','deletion.request'"
)
COMMAND_TYPES_NEW = COMMAND_TYPES_OLD[:-1] + "," + GOVERNANCE_COMMANDS + ")"

STATUS = (
    "'received','frozen','planned','purging','verifying','completed','completed_with_exceptions','blocked_legal_hold','failed','cancelled'"
)
KINDS = "'subject','interaction','memory','principal_data','workspace','retention'"

UP_SQL = f"""
-- ---- statuses ---------------------------------------------------------------------------------------------------------------------------------------
ALTER TABLE entities DROP CONSTRAINT entities_status_check;
ALTER TABLE entities ADD CONSTRAINT entities_status_check CHECK (status IN ('active','deleted','candidate','untyped','merged','deletion_pending'));
ALTER TABLE entities DROP CONSTRAINT entities_origin_check;
ALTER TABLE entities ADD CONSTRAINT entities_origin_check CHECK (origin IN ('command','extracted','correction'));
ALTER TABLE facts DROP CONSTRAINT facts_status_check;
ALTER TABLE facts ADD CONSTRAINT facts_status_check CHECK (status IN ('active','superseded','disputed','deleted','proposed','rejected','deletion_pending'));
ALTER TABLE facts DROP CONSTRAINT facts_source_type_check;
ALTER TABLE facts ADD CONSTRAINT facts_source_type_check CHECK (source_type IN ('command','extracted','inferred','correction'));
ALTER TABLE facts DROP CONSTRAINT facts_command_source_check;
ALTER TABLE facts ADD CONSTRAINT facts_command_source_check CHECK (source_type NOT IN ('command','correction') OR (source_command_id IS NOT NULL AND last_command_id IS NOT NULL));
ALTER TABLE curated_memories DROP CONSTRAINT curated_memories_status_check;
ALTER TABLE curated_memories ADD CONSTRAINT curated_memories_status_check
    CHECK (status IN ('proposed','active','superseded','disputed','expired','rejected','deleted','deletion_pending'));
ALTER TABLE workspaces DROP CONSTRAINT workspaces_status_check;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_status_check CHECK (status IN ('active','suspended','deleted'));
-- a fact made by a correction cites its correction command as its evidence
ALTER TABLE review_items ADD COLUMN deletion_request_id uuid;

-- ---- the ledger: governance commands -------------------------------------------------------------------------------------------------------
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_NEW});
ALTER TABLE commands DROP CONSTRAINT commands_ontology_required_check;
ALTER TABLE commands ADD CONSTRAINT commands_ontology_required_check CHECK (
    ontology_version_id IS NOT NULL OR command_type LIKE 'memory.%' OR command_type LIKE 'principal.%' OR command_type LIKE 'key.%' OR command_type LIKE 'grant.%'
    OR command_type LIKE 'settings.%' OR command_type LIKE 'deletion.%' OR command_type LIKE 'correction.%');

-- ---- deletion ---------------------------------------------------------------------------------------------------------------------------------------
CREATE TABLE deletion_requests (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kind text NOT NULL CHECK (kind IN ({KINDS})),
    target_id uuid,
    requested_by uuid,
    on_behalf_of_hmac text CHECK (on_behalf_of_hmac IS NULL OR on_behalf_of_hmac ~ '^[0-9a-f]{{64}}$'),
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    legal_basis text CHECK (legal_basis IS NULL OR length(legal_basis) <= 200),
    retention_class text CHECK (retention_class IS NULL OR retention_class ~ '^[a-z][a-z_]{{0,31}}$'),
    include_submissions boolean NOT NULL DEFAULT false,
    status text NOT NULL CHECK (status IN ({STATUS})),
    error_code text,
    received_at timestamptz NOT NULL DEFAULT now(),
    frozen_at timestamptz,
    planned_at timestamptz,
    purge_started_at timestamptz,
    verified_at timestamptz,
    completed_at timestamptz,
    cancelled_at timestamptz,
    cooling_off_until timestamptz,
    receipt jsonb,
    journal_ref text,
    revision integer NOT NULL DEFAULT 1,
    UNIQUE (workspace_id, id),
    CHECK (kind IN ('workspace','retention') OR target_id IS NOT NULL)
);
CREATE INDEX deletion_requests_ws_idx ON deletion_requests (workspace_id, received_at DESC, id DESC);
CREATE INDEX deletion_requests_open_idx ON deletion_requests (workspace_id, status) WHERE status NOT IN ('completed','completed_with_exceptions','cancelled');

CREATE TABLE deletion_items (
    request_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    store text NOT NULL CHECK (store IN ('postgres','vector','graph','trace','job','idempotency')),
    item_kind text NOT NULL CHECK (item_kind ~ '^[a-z][a-z_]{{0,63}}$'),
    item_id text NOT NULL CHECK (length(item_id) BETWEEN 1 AND 300),
    action text NOT NULL CHECK (action IN ('delete','redact','retain')),
    reason text CHECK (reason IS NULL OR reason ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed','retained')),
    attempts integer NOT NULL DEFAULT 0,
    PRIMARY KEY (request_id, store, item_kind, item_id),
    FOREIGN KEY (workspace_id, request_id) REFERENCES deletion_requests(workspace_id, id)
);
CREATE INDEX deletion_items_pending_idx ON deletion_items (request_id, store, status);

CREATE TABLE deletion_manifests (
    request_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kid text NOT NULL CHECK (kid ~ '^[A-Za-z0-9_.-]{{1,32}}$'),
    nonce bytea NOT NULL CHECK (length(nonce) = 12),
    ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, request_id) REFERENCES deletion_requests(workspace_id, id)
);

-- no content: a deleted id answers like a missing one, an old idempotent replay answers 410, and a restored backup can re-apply the deletion (P9)
CREATE TABLE deletion_tombstones (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kind text NOT NULL CHECK (kind IN ('entity','fact','interaction','memory')),
    id uuid NOT NULL,
    request_id uuid NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, kind, id),
    FOREIGN KEY (workspace_id, request_id) REFERENCES deletion_requests(workspace_id, id)
);

-- re-collection suppression: HMACs of a deleted subject's strong identifiers. A name is not an identity (KGR-02), so a name is never suppressed.
CREATE TABLE deletion_suppressions (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kind text NOT NULL CHECK (kind IN ('external_id')),
    value_hmac text NOT NULL CHECK (value_hmac ~ '^[0-9a-f]{{64}}$'),
    request_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    lifted_by uuid,
    lifted_at timestamptz,
    PRIMARY KEY (workspace_id, kind, value_hmac),
    FOREIGN KEY (workspace_id, request_id) REFERENCES deletion_requests(workspace_id, id)
);

CREATE TABLE legal_holds (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    scope_kind text NOT NULL CHECK (scope_kind IN ('subject','interaction','workspace')),
    target_id uuid,
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    note text NOT NULL DEFAULT '' CHECK (length(note) <= 500),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    released_by uuid,
    released_at timestamptz,
    CHECK ((scope_kind = 'workspace') = (target_id IS NULL))
);
CREATE INDEX legal_holds_active_idx ON legal_holds (workspace_id, scope_kind, target_id) WHERE released_at IS NULL;

-- ---- corrections -------------------------------------------------------------------------------------------------------------------------------------
CREATE TABLE corrections (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    command_id uuid NOT NULL,
    target_kind text NOT NULL CHECK (target_kind IN ('entity','fact','memory')),
    target_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z_.]{{0,63}}$'),
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    reason_text text CHECK (reason_text IS NULL OR length(reason_text) <= 1000),
    new_target_id uuid,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, command_id) REFERENCES commands(workspace_id, id)
);
CREATE INDEX corrections_target_idx ON corrections (workspace_id, target_kind, target_id, created_at DESC);
CREATE INDEX corrections_ws_idx ON corrections (workspace_id, created_at DESC, id DESC);

CREATE TABLE attribute_corrections (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    correction_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    attribute_key text NOT NULL CHECK (attribute_key ~ '^[A-Za-z][A-Za-z0-9_]{{0,63}}$'),
    entity_revision integer NOT NULL,
    old_value_hmac text CHECK (old_value_hmac IS NULL OR old_value_hmac ~ '^[0-9a-f]{{64}}$'),
    new_value_hmac text CHECK (new_value_hmac IS NULL OR new_value_hmac ~ '^[0-9a-f]{{64}}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, correction_id) REFERENCES corrections(workspace_id, id),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id)
);

CREATE TABLE entity_lineage (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    entity_id uuid NOT NULL,
    related_id uuid NOT NULL,
    relation text NOT NULL CHECK (relation IN ('split_from','merged_into')),
    command_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, entity_id, related_id, relation),
    CHECK (entity_id <> related_id),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id),
    FOREIGN KEY (workspace_id, related_id) REFERENCES entities(workspace_id, id)
);
INSERT INTO entity_lineage (workspace_id, entity_id, related_id, relation, created_at)
    SELECT workspace_id, id, merged_into_id, 'merged_into', COALESCE(merged_at, now()) FROM entities WHERE status = 'merged' AND merged_into_id IS NOT NULL;

-- ---- consolidation ----------------------------------------------------------------------------------------------------------------------------------
CREATE TABLE consolidation_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    key_hash text NOT NULL CHECK (key_hash ~ '^[0-9a-f]{{64}}$'),
    mode text NOT NULL CHECK (mode IN ('reinforce','synthesize')),
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{{64}}$'),
    outcome text NOT NULL CHECK (outcome ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    proposal_memory_id uuid,
    source_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, mode, input_hash)
);

-- ---- ontology simulation ---------------------------------------------------------------------------------------------------------------------------
CREATE TABLE ontology_simulations (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    draft_version_id uuid NOT NULL,
    staging_group text CHECK (staging_group IS NULL OR staging_group ~ '^ws-[A-Za-z0-9_-]+$'),
    status text NOT NULL CHECK (status IN ('previewed','running','completed','failed','cleaned')),
    sample_ids uuid[] NOT NULL DEFAULT '{{}}',
    report jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    cost_estimate numeric(14,6),
    cost_actual numeric(14,6),
    requested_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    cleaned_at timestamptz,
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, draft_version_id) REFERENCES ontology_versions(workspace_id, id)
);

-- ---- settings: a new version per change, the latest wins ----------------------------------------------------------------------------------
CREATE TABLE workspace_settings (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    version integer NOT NULL CHECK (version >= 1),
    settings jsonb NOT NULL,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, version)
);

-- ---- review items: the four new kinds ------------------------------------------------------------------------------------------------------------
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_NEW});
ALTER TABLE review_items DROP CONSTRAINT review_items_source_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL)
    OR (item_type IN ('memory_proposal','memory_conflict','consolidation_proposal') AND memory_id IS NOT NULL)
    OR (item_type = 'source_compromised' AND (memory_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'deletion_exception' AND deletion_request_id IS NOT NULL)
    OR (item_type IN ('correction_request','authoritative_override') AND (fact_id IS NOT NULL OR subject_entity_id IS NOT NULL OR memory_id IS NOT NULL)));
"""  # noqa: S608

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in NEW_TENANT_TABLES
)

# ---- the purge gate -----------------------------------------------------------------------------------------------------------------------------------
GATE_SQL = """
-- true only inside a transaction that named an approved, purging deletion request of THIS workspace that lists exactly this row with this action.
-- SECURITY INVOKER on purpose: it runs as the caller, so (1) the application role sees only its own workspace's requests (row-level security), which makes "another
-- workspace's request cannot unlock this workspace's rows" true twice over, and (2) code that runs as the OWNER (a SECURITY DEFINER ops function, a migration) is
-- recognised by `rolbypassrls` and passes: the gate guards the application role's own SQL, not the reviewed functions the platform owns (the context-trace retention, the
-- workspace purge, which re-checks its request itself).
CREATE FUNCTION public.purge_gate(p_ws uuid, p_store text, p_kind text, p_id text, p_action text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
    SELECT COALESCE((SELECT r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user), false) OR EXISTS (
        SELECT 1 FROM public.deletion_requests r
        WHERE r.id::text = current_setting('app.purge_request_id', true)
          AND r.workspace_id = p_ws AND r.status = 'purging'
          AND (r.kind = 'workspace'
               OR EXISTS (SELECT 1 FROM public.deletion_items i
                          WHERE i.request_id = r.id AND i.workspace_id = p_ws AND i.store = p_store AND i.item_kind = p_kind AND i.item_id = p_id AND i.action = p_action)))
$$;
-- the same test WITHOUT the role shortcut: for the tables whose immutability holds "even for the owner" (the ontology registry): only a purging request opens them
CREATE FUNCTION public.purge_request_allows(p_ws uuid, p_store text, p_kind text, p_id text, p_action text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.deletion_requests r
        WHERE r.id::text = current_setting('app.purge_request_id', true)
          AND r.workspace_id = p_ws AND r.status = 'purging'
          AND (r.kind = 'workspace'
               OR EXISTS (SELECT 1 FROM public.deletion_items i
                          WHERE i.request_id = r.id AND i.workspace_id = p_ws AND i.store = p_store AND i.item_kind = p_kind AND i.item_id = p_id AND i.action = p_action)))
$$;
REVOKE ALL ON FUNCTION public.purge_request_allows(uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.purge_request_allows(uuid, text, text, text, text) TO kg_app;
REVOKE ALL ON FUNCTION public.purge_gate(uuid, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.purge_gate(uuid, text, text, text, text) TO kg_app;

-- generic row trigger. TG_ARGV: store, item kind, then the key columns that make up the item id ('#col' is the md5 of col, so a value never lands in deletion_items).
CREATE FUNCTION public.purge_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j jsonb := to_jsonb(OLD);
    key text := '';
    i integer;
    spec text;
BEGIN
    FOR i IN 2 .. TG_NARGS - 1 LOOP
        spec := TG_ARGV[i];
        key := key || CASE WHEN i > 2 THEN '|' ELSE '' END ||
               CASE WHEN left(spec, 1) = '#' THEN md5(COALESCE(j ->> substr(spec, 2), '')) ELSE COALESCE(j ->> spec, '') END;
    END LOOP;
    IF NOT public.purge_gate((j ->> 'workspace_id')::uuid, TG_ARGV[0], TG_ARGV[1], key, CASE TG_OP WHEN 'DELETE' THEN 'delete' ELSE 'redact' END) THEN
        RAISE EXCEPTION '% rows are removed or redacted only through an approved deletion plan', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
"""

# (table, store, item kind, key columns). The kind is the table name, so an item id is unambiguous.
GATED = [
    ("interaction_contents", "postgres", "interaction_contents", ["interaction_id", "form"]),
    ("interaction_stages", "postgres", "interaction_stages", ["interaction_id", "stage"]),
    ("interaction_subjects", "postgres", "interaction_subjects", ["interaction_id", "role", "ordinal"]),
    ("attribute_claims", "postgres", "attribute_claims", ["id"]),
    ("entity_mentions", "postgres", "entity_mentions", ["id"]),
    ("entities", "postgres", "entities", ["id"]),
    ("entity_external_ids", "postgres", "entity_external_ids", ["entity_id", "type_key", "system", "#value"]),
    ("facts", "postgres", "facts", ["id"]),
    ("fact_evidence", "postgres", "fact_evidence", ["id"]),
    ("graph_episodes", "postgres", "graph_episodes", ["interaction_id", "generation"]),
    ("identity_non_matches", "postgres", "identity_non_matches", ["entity_a", "entity_b"]),
    ("review_items", "postgres", "review_items", ["id"]),
    ("commands", "postgres", "commands", ["id"]),
    ("curated_memories", "postgres", "curated_memories", ["id"]),
    ("memory_scopes", "postgres", "memory_scopes", ["memory_id", "scope_kind", "#scope_ref"]),
    ("memory_subjects", "postgres", "memory_subjects", ["memory_id", "entity_id"]),
    ("memory_evidence", "postgres", "memory_evidence", ["id"]),
    ("memory_lineage", "postgres", "memory_lineage", ["memory_id", "related_id", "relation"]),
    ("outbox_jobs", "job", "outbox_jobs", ["id"]),
    ("context_items", "trace", "context_items", ["context_id", "position"]),
    ("context_debug_snapshots", "trace", "context_debug_snapshots", ["context_id"]),
    ("context_traces", "trace", "context_traces", ["id"]),
    ("corrections", "postgres", "corrections", ["id"]),
    ("attribute_corrections", "postgres", "attribute_corrections", ["id"]),
    ("entity_lineage", "postgres", "entity_lineage", ["entity_id", "related_id", "relation"]),
]
# tables that get the application role DELETE (always behind purge_guard). `commands` is removed only with its workspace; job_attempts and interactions have their own guards.
DELETE_GRANTED = [g[0] for g in GATED if g[0] != "commands"]


def _trigger(t: str, store: str, kind: str, cols: list[str]) -> str:
    args = ", ".join(f"'{a}'" for a in (store, kind, *cols))
    return f"""
DROP TRIGGER IF EXISTS purge_guard_delete ON {t};
CREATE TRIGGER purge_guard_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION public.purge_guard({args});
"""


GATE_TRIGGERS_SQL = "".join(_trigger(*g) for g in GATED)
GATE_DROP_SQL = "".join(f"DROP TRIGGER IF EXISTS purge_guard_delete ON {g[0]};\n" for g in GATED)

GUARDS_SQL = """
-- the three tables that already had their own guard: the delete branch now asks the gate instead of refusing outright
CREATE OR REPLACE FUNCTION public.interactions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT public.purge_gate(OLD.workspace_id, 'postgres', 'interactions', OLD.id::text, 'delete') THEN
            RAISE EXCEPTION 'interactions are removed only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
    END IF;
    IF (to_jsonb(NEW) - 'processing_status' - 'status_changed_at') IS DISTINCT FROM (to_jsonb(OLD) - 'processing_status' - 'status_changed_at') THEN
        RAISE EXCEPTION 'the interaction envelope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION public.job_attempts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ended_at IS NOT NULL OR NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'job_attempts is append-only' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND public.purge_gate(OLD.workspace_id, 'job', 'job_attempts', OLD.id::text, 'delete') THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'job_attempts is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE OR REPLACE FUNCTION public.ontology_versions_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'draft' THEN
            RAISE EXCEPTION 'an ontology version is created as a draft' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.state <> 'draft' AND NOT public.purge_request_allows(OLD.workspace_id, 'postgres', 'ontology_versions', OLD.id::text, 'delete') THEN
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

CREATE OR REPLACE FUNCTION public.ontology_activations_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND public.purge_request_allows(OLD.workspace_id, 'postgres', 'ontology_activations', OLD.id::text, 'delete') THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'ontology_activations is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;

-- an identity SPLIT re-points a memory's subject (and its subject scope) to the split-off entity, the one other legal move besides a merge: the old entity is the one the
-- new entity was split from, recorded in entity_lineage in the same transaction
CREATE OR REPLACE FUNCTION public.memory_scope_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope_ref IS DISTINCT FROM OLD.scope_ref AND NOT (OLD.scope_kind = 'subject' AND (EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id::text = OLD.scope_ref AND e.status = 'merged' AND e.merged_into_id::text = NEW.scope_ref)
        OR EXISTS (SELECT 1 FROM public.entity_lineage l WHERE l.workspace_id = OLD.workspace_id AND l.relation = 'split_from'
                   AND l.related_id::text = OLD.scope_ref AND l.entity_id::text = NEW.scope_ref))) THEN
        RAISE EXCEPTION 'a memory scope is fixed at birth; only a merge or a split re-points a subject scope' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.memory_subject_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id AND NOT (EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id = OLD.entity_id AND e.status = 'merged' AND e.merged_into_id = NEW.entity_id)
        OR EXISTS (SELECT 1 FROM public.entity_lineage l WHERE l.workspace_id = OLD.workspace_id AND l.relation = 'split_from'
                   AND l.related_id = OLD.entity_id AND l.entity_id = NEW.entity_id)) THEN
        RAISE EXCEPTION 'a memory subject is fixed at birth; only a merge or a split re-points it' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

-- redaction (UPDATE) of the three columns that hold free text, each only through the gate and only to a redacted value
CREATE FUNCTION public.commands_payload_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload IS DISTINCT FROM OLD.payload THEN
        IF NOT (NEW.payload ? 'redacted') OR NOT public.purge_gate(OLD.workspace_id, 'postgres', 'commands', OLD.id::text, 'redact') THEN
            RAISE EXCEPTION 'a command payload is redacted only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF NEW.source_ref IS DISTINCT FROM OLD.source_ref THEN
        IF NEW.source_ref IS DISTINCT FROM '[redacted]' OR NOT public.purge_gate(OLD.workspace_id, 'postgres', 'commands', OLD.id::text, 'redact') THEN
            RAISE EXCEPTION 'a command source reference is redacted only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER commands_payload_guard BEFORE UPDATE ON commands FOR EACH ROW EXECUTE FUNCTION public.commands_payload_guard();

CREATE FUNCTION public.review_text_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.rationale_text IS NOT NULL AND NEW.rationale_text IS DISTINCT FROM OLD.rationale_text THEN
        IF NOT public.purge_gate(OLD.workspace_id, 'postgres', 'review_items', OLD.id::text, 'redact') THEN
            RAISE EXCEPTION 'a recorded rationale is redacted only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER review_text_guard BEFORE UPDATE ON review_items FOR EACH ROW EXECUTE FUNCTION public.review_text_guard();

CREATE FUNCTION public.context_trace_redact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'request') IS DISTINCT FROM (to_jsonb(OLD) - 'request')
       AND NOT COALESCE((SELECT r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user), false) THEN
        RAISE EXCEPTION 'a context trace is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT public.purge_gate(OLD.workspace_id, 'trace', 'context_traces', OLD.id::text, 'redact') THEN
        RAISE EXCEPTION 'a context trace is redacted only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER context_trace_redact_guard BEFORE UPDATE ON context_traces FOR EACH ROW EXECUTE FUNCTION public.context_trace_redact_guard();

CREATE FUNCTION public.outbox_payload_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload IS DISTINCT FROM OLD.payload AND NOT public.purge_gate(OLD.workspace_id, 'job', 'outbox_jobs', OLD.id::text, 'redact') THEN
        RAISE EXCEPTION 'a job payload is cleared only through an approved deletion plan' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER outbox_payload_guard BEFORE UPDATE ON outbox_jobs FOR EACH ROW EXECUTE FUNCTION public.outbox_payload_guard();

-- a deletion request moves along its lifecycle and nowhere else; its identity never changes
CREATE FUNCTION public.deletion_requests_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ok boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a deletion request is a record and stays' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status NOT IN ('received','frozen','blocked_legal_hold') THEN
            RAISE EXCEPTION 'a deletion request starts received, frozen or blocked' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.target_id IS DISTINCT FROM OLD.target_id
       OR NEW.requested_by IS DISTINCT FROM OLD.requested_by THEN
        RAISE EXCEPTION 'a deletion request keeps its target' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = OLD.status THEN RETURN NEW; END IF;
    ok := (OLD.status, NEW.status) IN (
        ('received','frozen'), ('received','blocked_legal_hold'), ('received','failed'),
        ('frozen','planned'), ('frozen','cancelled'), ('frozen','failed'), ('frozen','blocked_legal_hold'),
        ('blocked_legal_hold','frozen'), ('blocked_legal_hold','cancelled'),
        ('planned','purging'), ('planned','failed'), ('planned','blocked_legal_hold'), ('planned','planned'),
        ('purging','verifying'), ('purging','failed'),
        ('verifying','completed'), ('verifying','completed_with_exceptions'), ('verifying','purging'), ('verifying','failed'),
        ('failed','planned'), ('failed','purging'), ('failed','verifying'),
        ('completed_with_exceptions','verifying'), ('completed_with_exceptions','purging'));
    IF NOT ok THEN
        RAISE EXCEPTION 'a deletion request cannot move from % to %', OLD.status, NEW.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER deletion_requests_guard BEFORE INSERT OR UPDATE OR DELETE ON deletion_requests FOR EACH ROW EXECUTE FUNCTION public.deletion_requests_guard();

-- the plan is fixed before purging begins: no item is added while the request is purging (or after it ended)
CREATE FUNCTION public.deletion_items_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
    SELECT status INTO st FROM public.deletion_requests WHERE workspace_id = NEW.workspace_id AND id = NEW.request_id;
    IF st IS NULL OR st IN ('purging','completed','cancelled') THEN
        RAISE EXCEPTION 'items are added to a deletion plan before it is purged' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER deletion_items_guard BEFORE INSERT ON deletion_items FOR EACH ROW EXECUTE FUNCTION public.deletion_items_guard();

-- a tombstone and a suppression are never rewritten (only a suppression is lifted)
CREATE FUNCTION public.deletion_records_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'deletion_suppressions' AND TG_OP = 'UPDATE'
       AND (to_jsonb(NEW) - 'lifted_by' - 'lifted_at') IS NOT DISTINCT FROM (to_jsonb(OLD) - 'lifted_by' - 'lifted_at') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER deletion_tombstones_guard BEFORE UPDATE OR DELETE ON deletion_tombstones FOR EACH ROW EXECUTE FUNCTION public.deletion_records_guard();
CREATE TRIGGER deletion_suppressions_guard BEFORE UPDATE OR DELETE ON deletion_suppressions FOR EACH ROW EXECUTE FUNCTION public.deletion_records_guard();
"""

# ---- the platform receipt table and the cross-tenant ops functions ---------------------------------------------------------------------------------
OPS_SQL = """
CREATE TABLE platform_deletion_receipts (
    request_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    kind text NOT NULL,
    receipt jsonb NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON platform_deletion_receipts FROM PUBLIC;

CREATE FUNCTION ops.write_platform_receipt(p_request uuid, p_workspace uuid, p_kind text, p_receipt jsonb) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    INSERT INTO public.platform_deletion_receipts (request_id, workspace_id, kind, receipt) VALUES (p_request, p_workspace, p_kind, p_receipt)
    ON CONFLICT (request_id) DO UPDATE SET receipt = EXCLUDED.receipt, completed_at = now()
$$;
CREATE FUNCTION ops.read_platform_receipt(p_request uuid, p_workspace uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT receipt FROM public.platform_deletion_receipts WHERE request_id = p_request AND workspace_id = p_workspace
$$;

-- every Postgres row of a workspace, once its deletion request (kind workspace) is purging. Returns counts per table; the gate re-checks every row.
CREATE FUNCTION ops.purge_workspace_rows(p_request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    r public.deletion_requests%ROWTYPE;
    t text;
    n bigint;
    counts jsonb := '{}'::jsonb;
    -- children before parents
    tables text[] := ARRAY['context_items','context_debug_snapshots','context_traces','memory_embeddings','memory_lineage','memory_evidence','memory_scopes','memory_subjects',
        'review_items','curated_memories','fact_evidence','attribute_claims','entity_mentions','job_attempts','outbox_jobs','graph_episodes','interaction_contents',
        'interaction_stages','interaction_subjects','identity_non_matches','entity_lineage','attribute_corrections','corrections','consolidation_runs','ontology_simulations',
        'facts','entity_external_ids','entities','interactions','idempotency_records','agent_usage_daily','workspace_settings','legal_holds',
        'deletion_manifests','principal_scopes','role_assignments','api_keys'];
BEGIN
    SELECT * INTO r FROM public.deletion_requests WHERE id = p_request;
    IF NOT FOUND OR r.kind <> 'workspace' OR r.status <> 'purging' THEN
        RAISE EXCEPTION 'workspace rows are purged only for a purging workspace deletion request' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM set_config('app.purge_request_id', p_request::text, true);
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('DELETE FROM public.%I WHERE workspace_id = $1', t) USING r.workspace_id;
        GET DIAGNOSTICS n = ROW_COUNT;
        counts := counts || jsonb_build_object(t, n);
    END LOOP;
    -- the ledger and the ontology registry: ledger rows reference ontology versions, so commands go first
    UPDATE public.workspace_ontology SET active_version_id = NULL WHERE workspace_id = r.workspace_id;
    FOREACH t IN ARRAY ARRAY['commands','ontology_activations','workspace_ontology','ontology_versions','model_calls'] LOOP
        EXECUTE format('DELETE FROM public.%I WHERE workspace_id = $1', t) USING r.workspace_id;
        GET DIAGNOSTICS n = ROW_COUNT;
        counts := counts || jsonb_build_object(t, n);
    END LOOP;
    DELETE FROM public.principals WHERE workspace_id = r.workspace_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('principals', n);
    UPDATE public.workspaces SET status = 'deleted', display_name = 'deleted' WHERE id = r.workspace_id;
    RETURN counts;
END $$;

-- cross-workspace discovery for the retention job: workspace ids and counts ONLY (no ids of rows, no content)
CREATE FUNCTION ops.retention_candidates() RETURNS TABLE (workspace_id uuid, retention_class text, n bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    WITH latest AS (
        SELECT DISTINCT ON (s.workspace_id) s.workspace_id, s.settings FROM public.workspace_settings s ORDER BY s.workspace_id, s.version DESC
    ), live AS (
        SELECT l.workspace_id, l.settings FROM latest l JOIN public.workspaces w ON w.id = l.workspace_id AND w.status = 'active'
    )
    SELECT x.workspace_id, x.retention_class, x.n FROM (
        SELECT l.workspace_id, 'raw_content'::text AS retention_class,
               (SELECT count(*) FROM public.interaction_contents c WHERE c.workspace_id = l.workspace_id AND c.form = 'raw'
                  AND c.created_at < now() - ((l.settings #>> '{retention,raw_content_days}')::int * interval '1 day')) AS n
          FROM live l WHERE l.settings #>> '{retention,raw_content_days}' IS NOT NULL
        UNION ALL
        SELECT l.workspace_id, 'normalized_content',
               (SELECT count(*) FROM public.interaction_contents c WHERE c.workspace_id = l.workspace_id AND c.form = 'normalized'
                  AND c.created_at < now() - ((l.settings #>> '{retention,normalized_content_days}')::int * interval '1 day'))
          FROM live l WHERE l.settings #>> '{retention,normalized_content_days}' IS NOT NULL
        UNION ALL
        SELECT l.workspace_id, 'episode_content',
               (SELECT count(*) FROM public.graph_episodes g WHERE g.workspace_id = l.workspace_id
                  AND g.created_at < now() - ((l.settings #>> '{retention,episode_content_days}')::int * interval '1 day'))
          FROM live l WHERE l.settings #>> '{retention,episode_content_days}' IS NOT NULL
        UNION ALL
        SELECT l.workspace_id, 'model_calls',
               (SELECT count(*) FROM public.model_calls m WHERE m.workspace_id = l.workspace_id
                  AND m.created_at < now() - ((l.settings #>> '{retention,model_calls_days}')::int * interval '1 day'))
          FROM live l WHERE l.settings #>> '{retention,model_calls_days}' IS NOT NULL
        UNION ALL
        SELECT l.workspace_id, 'memory_archive',
               (SELECT count(*) FROM public.curated_memories m WHERE m.workspace_id = l.workspace_id AND m.status = 'active' AND m.type <> 'working'
                  AND COALESCE(m.last_confirmed_at, m.recorded_at) < now() - ((l.settings #>> '{retention,memory_archive_after_days}')::int * interval '1 day'))
          FROM live l WHERE l.settings #>> '{retention,memory_archive_after_days}' IS NOT NULL
    ) x WHERE x.n > 0
$$;

CREATE FUNCTION ops.retention_purge_model_calls(p_workspace uuid, p_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n bigint;
BEGIN
    DELETE FROM public.model_calls WHERE workspace_id = p_workspace AND created_at < now() - (p_days * interval '1 day');
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- the router transport asks this (it has no tenant session): the classifications a workspace has narrowed away. Names nothing else.
CREATE FUNCTION ops.workspace_egress_denials(p_workspace uuid) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT COALESCE((SELECT ARRAY(SELECT jsonb_array_elements_text(s.settings #> '{egress,denied_classifications}')) FROM public.workspace_settings s
                      WHERE s.workspace_id = p_workspace ORDER BY s.version DESC LIMIT 1), ARRAY[]::text[])
$$;

-- gauges: aggregates across workspaces (no ids)
CREATE FUNCTION ops.deletion_stats() RETURNS TABLE (kind text, status text, requests bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT r.kind, r.status, count(*)::bigint FROM public.deletion_requests r GROUP BY r.kind, r.status
$$;
-- workspace ids whose deletion work is due (frozen/planned/purging/verifying, or past the cooling-off): the sweeper's discovery
CREATE FUNCTION ops.workspaces_with_deletion_work(p_limit integer) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT DISTINCT r.workspace_id FROM public.deletion_requests r
     WHERE r.status IN ('frozen','planned','purging','verifying') AND (r.cooling_off_until IS NULL OR r.cooling_off_until <= now()) LIMIT GREATEST(p_limit, 1)
$$;
REVOKE ALL ON FUNCTION ops.write_platform_receipt(uuid, uuid, text, jsonb), ops.read_platform_receipt(uuid, uuid), ops.purge_workspace_rows(uuid),
    ops.retention_candidates(), ops.retention_purge_model_calls(uuid, integer), ops.workspace_egress_denials(uuid), ops.deletion_stats(),
    ops.workspaces_with_deletion_work(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.write_platform_receipt(uuid, uuid, text, jsonb), ops.read_platform_receipt(uuid, uuid), ops.purge_workspace_rows(uuid),
    ops.retention_candidates(), ops.retention_purge_model_calls(uuid, integer), ops.workspace_egress_denials(uuid), ops.deletion_stats(),
    ops.workspaces_with_deletion_work(integer) TO kg_app;
"""

GRANTS_SQL = f"""
GRANT SELECT, INSERT ON deletion_requests TO kg_app;
GRANT UPDATE (status, error_code, frozen_at, planned_at, purge_started_at, verified_at, completed_at, cancelled_at, cooling_off_until, receipt, journal_ref, revision,
              include_submissions) ON deletion_requests TO kg_app;
GRANT SELECT, INSERT ON deletion_items TO kg_app;
GRANT UPDATE (status, attempts) ON deletion_items TO kg_app;
GRANT SELECT, INSERT, DELETE ON deletion_manifests TO kg_app;
GRANT SELECT, INSERT ON deletion_tombstones TO kg_app;
GRANT SELECT, INSERT ON deletion_suppressions TO kg_app;
GRANT UPDATE (lifted_by, lifted_at) ON deletion_suppressions TO kg_app;
GRANT SELECT, INSERT ON legal_holds TO kg_app;
GRANT UPDATE (released_by, released_at) ON legal_holds TO kg_app;
GRANT SELECT, INSERT ON corrections, attribute_corrections, entity_lineage, consolidation_runs TO kg_app;
GRANT SELECT, INSERT, UPDATE ON ontology_simulations TO kg_app;
GRANT SELECT, INSERT ON workspace_settings TO kg_app;
GRANT UPDATE (payload, source_ref) ON commands TO kg_app;        -- redaction only: commands_payload_guard
GRANT UPDATE (request) ON context_traces TO kg_app;  -- redaction only: context_trace_redact_guard
GRANT UPDATE (payload) ON outbox_jobs TO kg_app;     -- clearing only: outbox_payload_guard
GRANT UPDATE (deletion_request_id) ON review_items TO kg_app;
GRANT UPDATE (source_interaction_id, source_episode_uuid) ON facts TO kg_app;   -- re-attachment of a retained fact to its remaining evidence
GRANT UPDATE (node_uuid) ON entity_mentions TO kg_app;   -- an identity split moves a mention to the split-off entity's node
GRANT DELETE ON {", ".join(DELETE_GRANTED)} TO kg_app;   -- every one of them behind purge_guard
GRANT DELETE ON job_attempts, interactions TO kg_app;
"""

CORRECTION_COMMANDS = "'correction.fact_reject','correction.fact_supersede','correction.fact_end','correction.fact_restore','correction.attribute','correction.rename','correction.memory','entity.split','memory.consolidate'"
DOWN_GUARD_SQL = f"""
SELECT (SELECT count(*) FROM deletion_requests WHERE status NOT IN ('completed','completed_with_exceptions','cancelled')) AS open_requests,
       (SELECT count(*) FROM commands WHERE command_type IN ({CORRECTION_COMMANDS})) + (SELECT count(*) FROM facts WHERE source_type = 'correction')
       + (SELECT count(*) FROM entities WHERE origin = 'correction') + (SELECT count(*) FROM corrections) AS correction_rows,
       (SELECT count(*) FROM workspaces WHERE status = 'deleted') AS deleted_workspaces
"""  # noqa: S608

DOWN_SQL = f"""
DROP FUNCTION IF EXISTS ops.workspaces_with_deletion_work(integer);
DROP FUNCTION IF EXISTS ops.deletion_stats();
DROP FUNCTION IF EXISTS ops.workspace_egress_denials(uuid);
DROP FUNCTION IF EXISTS ops.retention_purge_model_calls(uuid, integer);
DROP FUNCTION IF EXISTS ops.retention_candidates();
DROP FUNCTION IF EXISTS ops.purge_workspace_rows(uuid);
DROP FUNCTION IF EXISTS ops.read_platform_receipt(uuid, uuid);
DROP FUNCTION IF EXISTS ops.write_platform_receipt(uuid, uuid, text, jsonb);
DROP TABLE IF EXISTS platform_deletion_receipts;

DROP TRIGGER IF EXISTS deletion_items_guard ON deletion_items;
DROP FUNCTION IF EXISTS public.deletion_items_guard();
DROP TRIGGER IF EXISTS deletion_suppressions_guard ON deletion_suppressions;
DROP TRIGGER IF EXISTS deletion_tombstones_guard ON deletion_tombstones;
DROP TRIGGER IF EXISTS deletion_requests_guard ON deletion_requests;
DROP FUNCTION IF EXISTS public.deletion_records_guard();
DROP FUNCTION IF EXISTS public.deletion_requests_guard();
DROP TRIGGER IF EXISTS outbox_payload_guard ON outbox_jobs;
DROP TRIGGER IF EXISTS context_trace_redact_guard ON context_traces;
DROP TRIGGER IF EXISTS review_text_guard ON review_items;
DROP TRIGGER IF EXISTS commands_payload_guard ON commands;
DROP FUNCTION IF EXISTS public.outbox_payload_guard();
DROP FUNCTION IF EXISTS public.context_trace_redact_guard();
DROP FUNCTION IF EXISTS public.review_text_guard();
DROP FUNCTION IF EXISTS public.commands_payload_guard();

REVOKE DELETE ON {", ".join(DELETE_GRANTED)}, job_attempts, interactions FROM kg_app;
REVOKE UPDATE (payload, source_ref) ON commands FROM kg_app;
REVOKE UPDATE (request) ON context_traces FROM kg_app;
REVOKE UPDATE (payload) ON outbox_jobs FROM kg_app;
REVOKE UPDATE (deletion_request_id) ON review_items FROM kg_app;
REVOKE UPDATE (source_interaction_id, source_episode_uuid) ON facts FROM kg_app;
REVOKE UPDATE (node_uuid) ON entity_mentions FROM kg_app;

{"".join(f"DROP TRIGGER IF EXISTS purge_guard_delete ON {g[0]};" + chr(10) for g in GATED)}
DROP FUNCTION IF EXISTS public.purge_guard();

CREATE OR REPLACE FUNCTION public.interactions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'interactions are never deleted by the application' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (to_jsonb(NEW) - 'processing_status' - 'status_changed_at') IS DISTINCT FROM (to_jsonb(OLD) - 'processing_status' - 'status_changed_at') THEN
        RAISE EXCEPTION 'the interaction envelope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.job_attempts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ended_at IS NOT NULL OR NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'job_attempts is append-only' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'job_attempts is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE OR REPLACE FUNCTION public.ontology_versions_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
CREATE OR REPLACE FUNCTION public.ontology_activations_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'ontology_activations is append-only' USING ERRCODE = 'insufficient_privilege'; END $$;
CREATE OR REPLACE FUNCTION public.memory_scope_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope_ref IS DISTINCT FROM OLD.scope_ref AND NOT (OLD.scope_kind = 'subject' AND EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id::text = OLD.scope_ref AND e.status = 'merged' AND e.merged_into_id::text = NEW.scope_ref)) THEN
        RAISE EXCEPTION 'a memory scope is fixed at birth; only a merge re-points a subject scope to the canonical entity' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.memory_subject_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id AND NOT EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id = OLD.entity_id AND e.status = 'merged' AND e.merged_into_id = NEW.entity_id) THEN
        RAISE EXCEPTION 'a memory subject is fixed at birth; only a merge re-points it to the canonical entity' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

DROP FUNCTION IF EXISTS public.purge_request_allows(uuid, text, text, text, text);
DROP FUNCTION IF EXISTS public.purge_gate(uuid, text, text, text, text);

DELETE FROM review_items WHERE item_type IN ('deletion_exception','consolidation_proposal','correction_request','authoritative_override');
ALTER TABLE review_items DROP CONSTRAINT review_items_source_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL)
    OR (item_type IN ('memory_proposal','memory_conflict') AND memory_id IS NOT NULL)
    OR (item_type = 'source_compromised' AND (memory_id IS NOT NULL OR fact_id IS NOT NULL)));
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_OLD});
ALTER TABLE review_items DROP COLUMN deletion_request_id;

DROP TABLE IF EXISTS workspace_settings, ontology_simulations, consolidation_runs, entity_lineage, attribute_corrections, corrections, legal_holds,
    deletion_suppressions, deletion_tombstones, deletion_manifests, deletion_items, deletion_requests CASCADE;

ALTER TABLE commands DROP CONSTRAINT commands_ontology_required_check;
ALTER TABLE commands ADD CONSTRAINT commands_ontology_required_check CHECK (
    ontology_version_id IS NOT NULL OR command_type LIKE 'memory.%' OR command_type LIKE 'principal.%' OR command_type LIKE 'key.%' OR command_type LIKE 'grant.%');
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_OLD});

ALTER TABLE workspaces DROP CONSTRAINT workspaces_status_check;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_status_check CHECK (status IN ('active','suspended'));
ALTER TABLE curated_memories DROP CONSTRAINT curated_memories_status_check;
ALTER TABLE curated_memories ADD CONSTRAINT curated_memories_status_check CHECK (status IN ('proposed','active','superseded','disputed','expired','rejected','deleted'));
ALTER TABLE facts DROP CONSTRAINT facts_command_source_check;
ALTER TABLE facts ADD CONSTRAINT facts_command_source_check CHECK (source_type <> 'command' OR (source_command_id IS NOT NULL AND last_command_id IS NOT NULL));
ALTER TABLE facts DROP CONSTRAINT facts_source_type_check;
ALTER TABLE facts ADD CONSTRAINT facts_source_type_check CHECK (source_type IN ('command','extracted','inferred'));
ALTER TABLE facts DROP CONSTRAINT facts_status_check;
ALTER TABLE facts ADD CONSTRAINT facts_status_check CHECK (status IN ('active','superseded','disputed','deleted','proposed','rejected'));
ALTER TABLE entities DROP CONSTRAINT entities_origin_check;
ALTER TABLE entities ADD CONSTRAINT entities_origin_check CHECK (origin IN ('command','extracted'));
ALTER TABLE entities DROP CONSTRAINT entities_status_check;
ALTER TABLE entities ADD CONSTRAINT entities_status_check CHECK (status IN ('active','deleted','candidate','untyped','merged'));
"""  # noqa: S608


def _force() -> bool:
    return bool(context.config.attributes.get("force_drop_governance"))


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + GATE_SQL + GATE_TRIGGERS_SQL + GUARDS_SQL + OPS_SQL + GRANTS_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.exec_driver_sql(DOWN_GUARD_SQL).one()
    if (held.open_requests or held.correction_rows) and not _force():
        raise RuntimeError(
            f"refusing to downgrade 0008: {held.open_requests} deletion requests are not finished (frozen items must be purged or the request cancelled first) "
            f"and {held.correction_rows} rows were made by corrections, which the previous schema cannot name. "
            "Pass --force-drop-governance to drop the requests (their frozen items become active again) and keep the corrected rows as ordinary ones, re-pointed at the command they replaced."
        )
    # forced: evidence is never dropped, but it becomes the closest thing the old schema knows. Rows that cite a governance command are re-pointed at the command they
    # replaced (a corrected fact at the fact it superseded, a split-off entity at the entity it came from), then the governance commands go.
    op.execute(GATE_DROP_SQL)  # the removals below are the downgrade's own, not a deletion plan's
    bind.exec_driver_sql("UPDATE entities SET status = 'active' WHERE status = 'deletion_pending'")
    bind.exec_driver_sql("UPDATE facts SET status = 'active' WHERE status = 'deletion_pending'")
    bind.exec_driver_sql("UPDATE curated_memories SET status = 'active' WHERE status = 'deletion_pending'")
    bind.exec_driver_sql(
        "UPDATE entities e SET origin = 'command', created_by_command_id = o.created_by_command_id, last_command_id = o.created_by_command_id "
        "FROM entity_lineage l JOIN entities o ON o.workspace_id = l.workspace_id AND o.id = l.related_id "
        "WHERE l.relation = 'split_from' AND e.workspace_id = l.workspace_id AND e.id = l.entity_id AND e.origin = 'correction'"
    )
    bind.exec_driver_sql(
        "UPDATE facts f SET source_type = 'command', source_command_id = o.source_command_id, last_command_id = o.source_command_id "
        "FROM facts o WHERE o.workspace_id = f.workspace_id AND o.succeeded_by_fact_id = f.id AND f.source_type = 'correction'"
    )
    bind.exec_driver_sql("UPDATE entities SET origin = 'command' WHERE origin = 'correction'")
    bind.exec_driver_sql(
        "UPDATE fact_evidence e SET command_id = f.source_command_id FROM facts f WHERE f.workspace_id = e.workspace_id AND f.id = e.fact_id AND f.source_type = 'command' "  # noqa: S608
        "AND e.command_id IN (SELECT id FROM commands WHERE command_type IN (" + CORRECTION_COMMANDS + "))"  # noqa: S608
    )
    bind.exec_driver_sql("DELETE FROM fact_evidence WHERE fact_id IN (SELECT id FROM facts WHERE source_type = 'correction')")
    bind.exec_driver_sql(
        "UPDATE facts SET succeeded_by_fact_id = NULL WHERE succeeded_by_fact_id IN (SELECT id FROM facts WHERE source_type = 'correction')"
    )
    bind.exec_driver_sql("DELETE FROM facts WHERE source_type = 'correction'")
    for table, own in (("facts", "source_command_id"), ("entities", "created_by_command_id")):
        bind.exec_driver_sql(
            f"UPDATE {table} SET last_command_id = {own} WHERE last_command_id IN (SELECT id FROM commands WHERE command_type IN ({CORRECTION_COMMANDS},'settings.update','deletion.request'))"  # noqa: S608
        )
    for table in (
        "attribute_corrections",
        "corrections",
        "entity_lineage",
    ):  # they reference the commands that are about to go (the tables are dropped below)
        bind.exec_driver_sql(f"DELETE FROM {table}")  # noqa: S608
    bind.exec_driver_sql("UPDATE commands SET command_type = 'memory.propose' WHERE command_type = 'memory.consolidate'")
    bind.exec_driver_sql(
        "DELETE FROM commands WHERE command_type IN ("  # noqa: S608
        + CORRECTION_COMMANDS.replace(",'memory.consolidate'", "")
        + ",'settings.update','deletion.request')"  # noqa: S608
    )
    bind.exec_driver_sql("UPDATE workspaces SET status = 'suspended' WHERE status = 'deleted'")
    op.execute(DOWN_SQL)
