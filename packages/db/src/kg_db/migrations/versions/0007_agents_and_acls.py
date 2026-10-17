"""agents and ACLs: registration columns on principals, grant privileges, named keys, frozen private memory, daily usage, source-compromised reviews

Revision ID: 0007
Revises: 0006

Conventions follow 0001-0006: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security, column-level grants, DB-clock timestamps.

* `principals` gains the registration profile (description, trusted_source for apps, model_disclosure, allowed_profiles, quotas) and the revocation record
  (revoke_reason, revoked_by). `conversation_app` is backfilled as a trusted source so the P2-P6 demos keep their authority (phase-7 plan T12).
* `principal_scopes.privileges` is a subset of {read, propose, publish}. Every existing grant becomes {read, propose}: `publish` is never granted implicitly.
* `api_keys.name` names a key (rotation keeps several active keys per principal).
* `curated_memories.frozen_at` freezes an agent-private memory when its owner is revoked (no more writes or publishing; erasure is P8). `verified_by` / `verified_at`
  record the reviewer who verified an agent's finding (the only way a memory's `evidence_status` ever changes).
* `agent_usage_daily` is the per-principal, per-UTC-day usage aggregate. Only the request counter is not recoverable from durable tables; everything else is
  recomputed from context traces, interactions and memories (phase-7 plan T8).
* `review_items.item_type` adds `source_compromised` (a memory or fact derived from a compromised principal's submissions, quarantined for review).
* `commands.command_type` adds the principal-management commands; like memory commands they may exist without an ontology version.

Downgrade is guarded: it refuses while any grant carries `publish` unless force_forget_privileges is set (`kg db downgrade --force-forget-privileges`).
"""

from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = ["agent_usage_daily"]
REVIEW_TYPES_OLD = "('policy_violation','identity_match','contradiction','graphiti_invalidation','memory_proposal','memory_conflict')"
REVIEW_TYPES_NEW = (
    "('policy_violation','identity_match','contradiction','graphiti_invalidation','memory_proposal','memory_conflict','source_compromised')"
)
COMMAND_TYPES_OLD = (
    "('entity.create','entity.update','relationship.assert','relationship.end','review.decide','entity.merge',"
    "'memory.save','memory.propose','memory.extract','memory.supersede','memory.expire','memory.dispute','memory.publish','memory.review')"
)
PRINCIPAL_COMMANDS = "'principal.register','principal.update','principal.revoke','key.create','key.revoke','grant.add','grant.remove'"
COMMAND_TYPES_NEW = COMMAND_TYPES_OLD[:-1] + "," + PRINCIPAL_COMMANDS + ")"

UP_SQL = f"""
-- ---- registration profile and the revocation record ------------------------------------------------------------------------------------------------
ALTER TABLE principals
    ADD COLUMN description text NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    ADD COLUMN trusted_source boolean NOT NULL DEFAULT false,
    ADD COLUMN model_disclosure text NOT NULL DEFAULT 'allowed' CHECK (model_disclosure IN ('allowed','denied')),
    ADD COLUMN allowed_profiles text[] NOT NULL DEFAULT '{{}}',
    ADD COLUMN quota_requests_per_minute integer CHECK (quota_requests_per_minute IS NULL OR quota_requests_per_minute BETWEEN 1 AND 1000000),
    ADD COLUMN quota_context_tokens_per_day bigint CHECK (quota_context_tokens_per_day IS NULL OR quota_context_tokens_per_day >= 1),
    ADD COLUMN quota_interactions_per_day integer CHECK (quota_interactions_per_day IS NULL OR quota_interactions_per_day >= 1),
    ADD COLUMN revoke_reason text CHECK (revoke_reason IS NULL OR revoke_reason IN ('retired','compromised')),
    ADD COLUMN revoked_by uuid,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN revision integer NOT NULL DEFAULT 1,
    ADD CONSTRAINT principals_trusted_only_apps CHECK (NOT trusted_source OR kind = 'app'),
    ADD CONSTRAINT principals_revoked_has_reason CHECK (status <> 'revoked' OR revoke_reason IS NOT NULL OR revoked_at IS NOT NULL);
-- the demo's HR system of record keeps authoritative_system authority (T12)
UPDATE principals SET trusted_source = true WHERE kind = 'app' AND handle = 'conversation_app';

-- ---- grant privileges: read | propose | publish; a pre-existing grant is {{read, propose}} --------------------------------------------------------------
ALTER TABLE principal_scopes
    ADD COLUMN privileges text[] NOT NULL DEFAULT '{{read,propose}}',
    ADD CONSTRAINT principal_scopes_privileges_check CHECK (privileges <@ ARRAY['read','propose','publish']::text[] AND cardinality(privileges) >= 1);
UPDATE principal_scopes SET privileges = '{{read,propose}}';

-- ---- named keys ------------------------------------------------------------------------------------------------------------------------------------
ALTER TABLE api_keys ADD COLUMN name text NOT NULL DEFAULT '' CHECK (length(name) <= 100);
GRANT SELECT (name) ON api_keys TO kg_app;

-- ---- frozen private memory (revoked owner) ------------------------------------------------------------------------------------------------------
ALTER TABLE curated_memories ADD COLUMN frozen_at timestamptz, ADD COLUMN verified_by uuid, ADD COLUMN verified_at timestamptz,
    ADD CONSTRAINT curated_memories_verified_by_fk FOREIGN KEY (workspace_id, verified_by) REFERENCES principals(workspace_id, id);
-- a reviewer's approval of an agent's finding makes it verified (who, when); nothing else ever changes evidence_status. The grant is a column grant, and this trigger
-- narrows it to the one legal move, so a bug in the application cannot launder a memory into "verified".
GRANT UPDATE (frozen_at, evidence_status, verified_by, verified_at) ON curated_memories TO kg_app;
CREATE FUNCTION public.curated_memories_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.evidence_status IS DISTINCT FROM OLD.evidence_status AND NOT (
        OLD.evidence_status = 'unverified' AND NEW.evidence_status = 'verified' AND NEW.verified_by IS NOT NULL AND OLD.authority_class = 'agent_generated') THEN
        RAISE EXCEPTION 'evidence_status moves only from unverified to verified, by a named reviewer, on an agent finding' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (NEW.verified_by IS DISTINCT FROM OLD.verified_by OR NEW.verified_at IS DISTINCT FROM OLD.verified_at) AND NEW.evidence_status IS NOT DISTINCT FROM OLD.evidence_status THEN
        RAISE EXCEPTION 'verified_by and verified_at move only with the verification itself' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER curated_memories_evidence_guard BEFORE UPDATE ON curated_memories FOR EACH ROW EXECUTE FUNCTION public.curated_memories_evidence_guard();

-- ---- daily usage ----------------------------------------------------------------------------------------------------------------------------------
CREATE TABLE agent_usage_daily (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    principal_id uuid NOT NULL,
    day date NOT NULL,
    requests bigint NOT NULL DEFAULT 0 CHECK (requests >= 0),
    context_builds integer NOT NULL DEFAULT 0 CHECK (context_builds >= 0),
    context_tokens bigint NOT NULL DEFAULT 0 CHECK (context_tokens >= 0),
    interactions integer NOT NULL DEFAULT 0 CHECK (interactions >= 0),
    proposals integer NOT NULL DEFAULT 0 CHECK (proposals >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, principal_id, day),
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id)
);

-- ---- gauges: aggregates across workspaces, NO workspace ids and no handles (like ops.memory_stats) ---------------------------------------------------------------
CREATE FUNCTION ops.principal_stats() RETURNS TABLE (kind text, status text, principals bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT p.kind, p.status, count(*)::bigint FROM public.principals p GROUP BY p.kind, p.status
$$;
CREATE FUNCTION ops.grant_stats() RETURNS TABLE (scope_kind text, grants bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT s.scope_kind, count(*)::bigint FROM public.principal_scopes s GROUP BY s.scope_kind
$$;
REVOKE ALL ON FUNCTION ops.principal_stats(), ops.grant_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.principal_stats(), ops.grant_stats() TO kg_app;

-- ---- review items: a derivative of a compromised principal's submissions ------------------------------------------------------------------
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_NEW});
ALTER TABLE review_items DROP CONSTRAINT review_items_source_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL)
    OR (item_type IN ('memory_proposal','memory_conflict') AND memory_id IS NOT NULL)
    OR (item_type = 'source_compromised' AND (memory_id IS NOT NULL OR fact_id IS NOT NULL)));

-- ---- the ledger: principal-management commands ------------------------------------------------------------------------------------------------
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_NEW});
ALTER TABLE commands DROP CONSTRAINT commands_ontology_required_check;
ALTER TABLE commands ADD CONSTRAINT commands_ontology_required_check CHECK (
    ontology_version_id IS NOT NULL OR command_type LIKE 'memory.%' OR command_type LIKE 'principal.%' OR command_type LIKE 'key.%' OR command_type LIKE 'grant.%');
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
GRANT SELECT, INSERT, UPDATE ON agent_usage_daily TO kg_app;
-- an entity merge re-points the references that name the merged entity (grants already have UPDATE/DELETE): memory scopes and memory subjects follow the canonical id.
-- The column grants are narrowed by triggers to that ONE move (the old id is a merged entity whose canonical id is the new one), so scopes stay fixed at birth otherwise.
GRANT UPDATE (scope_ref) ON memory_scopes TO kg_app;
GRANT UPDATE (entity_id) ON memory_subjects TO kg_app;
CREATE FUNCTION public.memory_scope_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope_ref IS DISTINCT FROM OLD.scope_ref AND NOT (OLD.scope_kind = 'subject' AND EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id::text = OLD.scope_ref AND e.status = 'merged' AND e.merged_into_id::text = NEW.scope_ref)) THEN
        RAISE EXCEPTION 'a memory scope is fixed at birth; only a merge re-points a subject scope to the canonical entity' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_scope_repoint_guard BEFORE UPDATE ON memory_scopes FOR EACH ROW EXECUTE FUNCTION public.memory_scope_repoint_guard();
CREATE FUNCTION public.memory_subject_repoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id AND NOT EXISTS (
        SELECT 1 FROM public.entities e WHERE e.workspace_id = OLD.workspace_id AND e.id = OLD.entity_id AND e.status = 'merged' AND e.merged_into_id = NEW.entity_id) THEN
        RAISE EXCEPTION 'a memory subject is fixed at birth; only a merge re-points it to the canonical entity' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_subject_repoint_guard BEFORE UPDATE ON memory_subjects FOR EACH ROW EXECUTE FUNCTION public.memory_subject_repoint_guard();
"""


def _force() -> bool:
    return bool(context.config.attributes.get("force_forget_privileges"))


DOWN_GUARD_SQL = "SELECT (SELECT count(*) FROM principal_scopes WHERE 'publish' = ANY(privileges)) AS n"

DOWN_SQL = f"""
DROP FUNCTION IF EXISTS ops.principal_stats();
DROP FUNCTION IF EXISTS ops.grant_stats();
DROP TRIGGER IF EXISTS memory_scope_repoint_guard ON memory_scopes;
DROP TRIGGER IF EXISTS memory_subject_repoint_guard ON memory_subjects;
DROP FUNCTION IF EXISTS public.memory_scope_repoint_guard();
DROP FUNCTION IF EXISTS public.memory_subject_repoint_guard();
REVOKE UPDATE (scope_ref) ON memory_scopes FROM kg_app;
REVOKE UPDATE (entity_id) ON memory_subjects FROM kg_app;
DELETE FROM review_items WHERE item_type = 'source_compromised';
DELETE FROM commands WHERE command_type LIKE 'principal.%' OR command_type LIKE 'key.%' OR command_type LIKE 'grant.%';
ALTER TABLE commands DROP CONSTRAINT commands_ontology_required_check;
ALTER TABLE commands ADD CONSTRAINT commands_ontology_required_check CHECK (ontology_version_id IS NOT NULL OR command_type LIKE 'memory.%');
ALTER TABLE commands DROP CONSTRAINT commands_command_type_check;
ALTER TABLE commands ADD CONSTRAINT commands_command_type_check CHECK (command_type IN {COMMAND_TYPES_OLD});

ALTER TABLE review_items DROP CONSTRAINT review_items_source_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_source_check CHECK (
    (item_type = 'policy_violation' AND (command_id IS NOT NULL OR fact_id IS NOT NULL))
    OR (item_type = 'identity_match' AND candidate_entity_id IS NOT NULL AND subject_entity_id IS NOT NULL)
    OR (item_type IN ('contradiction','graphiti_invalidation') AND fact_id IS NOT NULL)
    OR (item_type IN ('memory_proposal','memory_conflict') AND memory_id IS NOT NULL));
ALTER TABLE review_items DROP CONSTRAINT review_items_item_type_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_item_type_check CHECK (item_type IN {REVIEW_TYPES_OLD});

DROP TABLE IF EXISTS agent_usage_daily CASCADE;
DROP TRIGGER IF EXISTS curated_memories_evidence_guard ON curated_memories;
DROP FUNCTION IF EXISTS public.curated_memories_evidence_guard();
ALTER TABLE curated_memories DROP CONSTRAINT IF EXISTS curated_memories_verified_by_fk, DROP COLUMN IF EXISTS verified_at, DROP COLUMN IF EXISTS verified_by, DROP COLUMN IF EXISTS frozen_at;
ALTER TABLE api_keys DROP COLUMN name;
ALTER TABLE principal_scopes DROP CONSTRAINT principal_scopes_privileges_check, DROP COLUMN privileges;
ALTER TABLE principals
    DROP CONSTRAINT principals_revoked_has_reason, DROP CONSTRAINT principals_trusted_only_apps,
    DROP COLUMN revision, DROP COLUMN updated_at, DROP COLUMN revoked_by, DROP COLUMN revoke_reason, DROP COLUMN quota_interactions_per_day,
    DROP COLUMN quota_context_tokens_per_day, DROP COLUMN quota_requests_per_minute, DROP COLUMN allowed_profiles, DROP COLUMN model_disclosure,
    DROP COLUMN trusted_source, DROP COLUMN description;
"""  # noqa: S608


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + GRANTS_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.exec_driver_sql(DOWN_GUARD_SQL).scalar_one()
    if held and not _force():
        raise RuntimeError(
            f"refusing to downgrade 0007: {held} grants carry the publish privilege, which the previous schema cannot represent. "
            "Pass --force-forget-privileges to drop them."
        )
    op.execute(DOWN_SQL)
