"""interaction ledger, per-stage state, transactional outbox, job attempts; RLS; ops.* claim/heartbeat/complete/release/cancel/backlog functions

Revision ID: 0003
Revises: 0002

Conventions follow 0001/0002: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security with the tenant_isolation
policy, column-level grants for kg_app, DB-clock timestamps. The interaction envelope is immutable (kg_app may only move its processing status);
raw and normalised content are stored ENCRYPTED (AES-256-GCM, ADR-024). Outbox rows are changed only through SECURITY DEFINER ops.* functions
(ADR-025): kg_app can INSERT and SELECT jobs, never UPDATE them.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TENANT_TABLES = ["interactions", "interaction_contents", "interaction_subjects", "interaction_stages", "outbox_jobs", "job_attempts"]
CLASSIFICATIONS = "('public','internal','confidential','restricted','local_only')"
KINDS = "('message','conversation','observation','document','json')"
PROCESSING = "('accepted','processing','indexed','partial','failed','deletion_pending')"
STAGE_STATUS = "('pending','running','succeeded','skipped_by_policy','skipped_disabled','invalid','failed','dead')"
JOB_STATUS = "('pending','leased','succeeded','skipped','dead','cancelled')"
CATEGORY = "('retryable','policy_terminal','config_terminal','budget')"
PIPELINE_VERSION = "p3.v1"

TABLES_SQL = f"""
CREATE TABLE interactions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_system text NOT NULL CHECK (source_system ~ '^[a-z0-9][a-z0-9_.-]{{0,63}}$'),
    source_ref text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 256),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_hmac text NOT NULL CHECK (request_hmac ~ '^[0-9a-f]{{64}}$'),
    kind text NOT NULL CHECK (kind IN {KINDS}),
    content_type text,
    occurred_at timestamptz,
    occurred_at_unknown boolean NOT NULL DEFAULT false,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    submitted_by uuid NOT NULL,
    classification text NOT NULL CHECK (classification IN {CLASSIFICATIONS}),
    consent_external boolean NOT NULL DEFAULT true,
    pipeline_version text NOT NULL,
    ontology_version_id uuid,
    processing_status text NOT NULL DEFAULT 'accepted' CHECK (processing_status IN {PROCESSING}),
    status_changed_at timestamptz NOT NULL DEFAULT now(),
    byte_size integer NOT NULL CHECK (byte_size >= 0),
    token_estimate integer,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, source_system, idempotency_key),
    CHECK (occurred_at IS NULL OR NOT occurred_at_unknown),
    CHECK (occurred_at IS NOT NULL OR occurred_at_unknown),
    FOREIGN KEY (workspace_id, submitted_by) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX interactions_ws_recorded_idx ON interactions (workspace_id, recorded_at DESC, id DESC);
CREATE INDEX interactions_ws_status_idx ON interactions (workspace_id, processing_status);
CREATE INDEX interactions_ws_submitter_idx ON interactions (workspace_id, submitted_by, recorded_at DESC);

-- encrypted raw and normalised forms. kid / nonce / ciphertext are bound to (workspace, interaction, form) by the AEAD associated data.
CREATE TABLE interaction_contents (
    interaction_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    form text NOT NULL CHECK (form IN ('raw','normalized')),
    kid text NOT NULL CHECK (kid ~ '^[A-Za-z0-9_.-]{{1,32}}$'),
    nonce bytea NOT NULL CHECK (length(nonce) = 12),
    ciphertext bytea NOT NULL,
    content_hmac text NOT NULL CHECK (content_hmac ~ '^[0-9a-f]{{64}}$'),
    byte_size integer NOT NULL CHECK (byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (interaction_id, form),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id)
);
CREATE INDEX interaction_contents_kid_idx ON interaction_contents (workspace_id, kid);

CREATE TABLE interaction_subjects (
    interaction_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    role text NOT NULL CHECK (role IN ('subject','actor')),
    ordinal smallint NOT NULL,
    entity_id uuid,
    ref_type text,
    ref_system text,
    ref_value_hmac text CHECK (ref_value_hmac IS NULL OR ref_value_hmac ~ '^[0-9a-f]{{64}}$'),
    resolved boolean NOT NULL,
    PRIMARY KEY (interaction_id, role, ordinal),
    CHECK (resolved = (entity_id IS NOT NULL)),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities(workspace_id, id)
);
CREATE INDEX interaction_subjects_entity_idx ON interaction_subjects (workspace_id, entity_id) WHERE entity_id IS NOT NULL;

CREATE TABLE interaction_stages (
    interaction_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    stage text NOT NULL CHECK (stage ~ '^[a-z][a-z_]{{0,31}}$'),
    generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
    status text NOT NULL CHECK (status IN {STAGE_STATUS}),
    attempts integer NOT NULL DEFAULT 0,
    last_error_category text CHECK (last_error_category IS NULL OR last_error_category IN {CATEGORY}),
    last_error_code text,
    result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    pipeline_version text NOT NULL,
    ontology_version_id uuid,
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (interaction_id, stage),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX interaction_stages_open_idx ON interaction_stages (workspace_id, status) WHERE status IN ('pending','running','failed');

CREATE TABLE outbox_jobs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    stage text NOT NULL CHECK (stage ~ '^[a-z][a-z_]{{0,31}}$'),
    subject_type text NOT NULL CHECK (subject_type IN ('interaction','command')),
    subject_id uuid NOT NULL,
    dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 255),
    payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN {JOB_STATUS}),
    priority integer NOT NULL DEFAULT 100,
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL CHECK (max_attempts >= 1),
    available_at timestamptz NOT NULL DEFAULT now(),
    concurrency_key text,
    lease_owner text,
    lease_token uuid,
    lease_expires_at timestamptz,
    last_error_category text CHECK (last_error_category IS NULL OR last_error_category IN {CATEGORY}),
    last_error_code text,
    pipeline_version text NOT NULL,
    ontology_version_id uuid,
    correlation_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, dedupe_key),
    CHECK (status <> 'leased' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX outbox_claimable_idx ON outbox_jobs (priority, available_at, id) WHERE status IN ('pending','leased');
CREATE INDEX outbox_concurrency_idx ON outbox_jobs (concurrency_key) WHERE status = 'leased' AND concurrency_key IS NOT NULL;
CREATE INDEX outbox_ws_created_idx ON outbox_jobs (workspace_id, created_at DESC, id DESC);
CREATE INDEX outbox_ws_status_idx ON outbox_jobs (workspace_id, status, stage);
CREATE INDEX outbox_subject_idx ON outbox_jobs (workspace_id, subject_type, subject_id);

CREATE TABLE job_attempts (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    attempt_no integer NOT NULL,
    worker_id text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    outcome text CHECK (outcome IS NULL OR outcome IN ('succeeded','skipped','retry','dead','budget_pause','lease_expired','released')),
    error_category text,
    error_code text,
    duration_ms integer,
    FOREIGN KEY (workspace_id, job_id) REFERENCES outbox_jobs(workspace_id, id)
);
CREATE INDEX job_attempts_job_idx ON job_attempts (job_id, attempt_no);
CREATE INDEX job_attempts_ws_reclaim_idx ON job_attempts (workspace_id, ended_at DESC) WHERE outcome = 'lease_expired';
"""

TRIGGERS_SQL = """
-- job_attempts is append-only: rows are inserted by ops.claim_jobs and CLOSED once by ops.complete_job / release / reclaim; nothing else changes them.
CREATE FUNCTION public.job_attempts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER job_attempts_guard_row BEFORE UPDATE OR DELETE ON job_attempts FOR EACH ROW EXECUTE FUNCTION public.job_attempts_guard();
CREATE TRIGGER job_attempts_guard_truncate BEFORE TRUNCATE ON job_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.job_attempts_guard();

-- the interaction envelope is immutable even for a role that was granted more than it should have been: only the processing status may move
CREATE FUNCTION public.interactions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'interactions are never deleted by the application' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (to_jsonb(NEW) - 'processing_status' - 'status_changed_at') IS DISTINCT FROM (to_jsonb(OLD) - 'processing_status' - 'status_changed_at') THEN
        RAISE EXCEPTION 'the interaction envelope is immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER interactions_guard BEFORE UPDATE OR DELETE ON interactions FOR EACH ROW EXECUTE FUNCTION public.interactions_guard();
"""

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in TENANT_TABLES
)

# ---- ops.* : the only way outbox rows change. SECURITY DEFINER, owned by kg_owner (BYPASSRLS), pinned search_path. ----------------------------
# They return routing metadata only (ids, stage, attempt, lease token, pinned versions). Time is the database clock.
OPS_SQL = """
CREATE FUNCTION ops.claim_jobs(p_worker text, p_stages text[], p_limit integer, p_lease_seconds integer)
RETURNS TABLE (job_id uuid, workspace_id uuid, stage text, subject_type text, subject_id uuid, payload jsonb, attempts integer, max_attempts integer,
               lease_token uuid, pipeline_version text, ontology_version_id uuid, correlation_id text, reclaimed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
#variable_conflict use_column
DECLARE
    r public.outbox_jobs%ROWTYPE;
    n integer := 0;
    token uuid;
BEGIN
    IF p_limit < 1 THEN RETURN; END IF;
    FOR r IN
        SELECT j.* FROM public.outbox_jobs j
        WHERE j.stage = ANY (p_stages)
          AND ((j.status = 'pending' AND j.available_at <= now()) OR (j.status = 'leased' AND j.lease_expires_at < now()))
        ORDER BY j.priority, j.available_at, j.id
        LIMIT GREATEST(p_limit * 4, 20)
        FOR UPDATE OF j SKIP LOCKED
    LOOP
        EXIT WHEN n >= p_limit;
        IF r.concurrency_key IS NOT NULL THEN
            -- serialise jobs that share a key: the advisory lock closes the race between two claimers that both see "no live lease" (released at commit)
            IF NOT pg_try_advisory_xact_lock(hashtextextended('ck:' || r.workspace_id::text || ':' || r.concurrency_key, 0)) THEN CONTINUE; END IF;
            IF EXISTS (SELECT 1 FROM public.outbox_jobs o WHERE o.workspace_id = r.workspace_id AND o.concurrency_key = r.concurrency_key
                       AND o.id <> r.id AND o.status = 'leased' AND o.lease_expires_at >= now()) THEN CONTINUE; END IF;
        END IF;
        IF r.status = 'leased' THEN
            UPDATE public.job_attempts a SET ended_at = clock_timestamp(), outcome = 'lease_expired',
                   duration_ms = (EXTRACT(EPOCH FROM clock_timestamp() - a.started_at) * 1000)::integer
             WHERE a.job_id = r.id AND a.ended_at IS NULL;
        END IF;
        token := gen_random_uuid();
        UPDATE public.outbox_jobs j SET status = 'leased', lease_owner = p_worker, lease_token = token,
               lease_expires_at = now() + (p_lease_seconds * interval '1 second'), attempts = j.attempts + 1
         WHERE j.id = r.id;
        INSERT INTO public.job_attempts (id, job_id, workspace_id, attempt_no, worker_id, started_at)
        VALUES (gen_random_uuid(), r.id, r.workspace_id, r.attempts + 1, p_worker, clock_timestamp());
        n := n + 1;
        job_id := r.id; workspace_id := r.workspace_id; stage := r.stage; subject_type := r.subject_type; subject_id := r.subject_id;
        payload := r.payload; attempts := r.attempts + 1; max_attempts := r.max_attempts; lease_token := token;
        pipeline_version := r.pipeline_version; ontology_version_id := r.ontology_version_id; correlation_id := r.correlation_id;
        reclaimed := (r.status = 'leased');
        RETURN NEXT;
    END LOOP;
END $$;

-- Extends the leases this worker still owns and returns the tokens it LOST (reclaimed by another worker, completed or cancelled meanwhile).
CREATE FUNCTION ops.heartbeat_jobs(p_worker text, p_tokens uuid[], p_lease_seconds integer) RETURNS SETOF uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    UPDATE public.outbox_jobs j SET lease_expires_at = now() + (p_lease_seconds * interval '1 second')
     WHERE j.lease_token = ANY (p_tokens) AND j.status = 'leased' AND j.lease_owner = p_worker;
    RETURN QUERY
        SELECT t FROM unnest(p_tokens) AS t
         WHERE NOT EXISTS (SELECT 1 FROM public.outbox_jobs j WHERE j.lease_token = t AND j.status = 'leased' AND j.lease_owner = p_worker);
END $$;

-- FENCED completion. A mismatched token means another worker reclaimed the job: answer 'stale' and change nothing.
CREATE FUNCTION ops.complete_job(p_job uuid, p_token uuid, p_outcome text, p_category text, p_code text, p_delay_seconds double precision)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    j public.outbox_jobs%ROWTYPE;
BEGIN
    SELECT * INTO j FROM public.outbox_jobs WHERE id = p_job FOR UPDATE;
    IF NOT FOUND OR j.status <> 'leased' OR j.lease_token IS DISTINCT FROM p_token THEN
        RETURN 'stale';
    END IF;
    IF p_outcome IN ('succeeded', 'skipped', 'dead') THEN
        UPDATE public.outbox_jobs SET status = p_outcome, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, finished_at = now(),
               last_error_category = COALESCE(p_category, last_error_category), last_error_code = COALESCE(p_code, last_error_code)
         WHERE id = p_job;
    ELSIF p_outcome = 'retry' THEN
        UPDATE public.outbox_jobs SET status = 'pending', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               available_at = now() + (GREATEST(p_delay_seconds, 0) * interval '1 second'), last_error_category = p_category, last_error_code = p_code
         WHERE id = p_job;
    ELSIF p_outcome = 'budget_pause' THEN   -- waiting for budget is not a failure: the attempt is not consumed
        UPDATE public.outbox_jobs SET status = 'pending', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0),
               available_at = now() + (GREATEST(p_delay_seconds, 0) * interval '1 second'), last_error_category = p_category, last_error_code = p_code
         WHERE id = p_job;
    ELSE
        RAISE EXCEPTION 'unknown outcome' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    UPDATE public.job_attempts a SET ended_at = clock_timestamp(), outcome = p_outcome, error_category = p_category, error_code = p_code,
           duration_ms = (EXTRACT(EPOCH FROM clock_timestamp() - a.started_at) * 1000)::integer
     WHERE a.job_id = p_job AND a.ended_at IS NULL;
    RETURN 'ok';
END $$;

-- Graceful shutdown: the worker's in-flight jobs go back to pending without consuming an attempt (a shutdown is not a failure).
CREATE FUNCTION ops.release_jobs(p_worker text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    n integer;
BEGIN
    UPDATE public.job_attempts a SET ended_at = clock_timestamp(), outcome = 'released',
           duration_ms = (EXTRACT(EPOCH FROM clock_timestamp() - a.started_at) * 1000)::integer
     WHERE a.ended_at IS NULL AND a.job_id IN (SELECT j.id FROM public.outbox_jobs j WHERE j.status = 'leased' AND j.lease_owner = p_worker);
    UPDATE public.outbox_jobs j SET status = 'pending', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, attempts = GREATEST(j.attempts - 1, 0)
     WHERE j.status = 'leased' AND j.lease_owner = p_worker;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- An operator cancels a pending or dead job of THEIR workspace (the tenant setting scopes it; no workspace argument can widen it).
CREATE FUNCTION ops.cancel_job(p_job uuid)
RETURNS TABLE (stage text, subject_type text, subject_id uuid, payload jsonb, previous_status text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
#variable_conflict use_column
DECLARE
    j public.outbox_jobs%ROWTYPE;
BEGIN
    SELECT * INTO j FROM public.outbox_jobs o WHERE o.id = p_job AND o.workspace_id = public.current_workspace_id() FOR UPDATE;
    IF NOT FOUND OR j.status NOT IN ('pending', 'dead') THEN RETURN; END IF;
    UPDATE public.outbox_jobs o SET status = 'cancelled', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, finished_at = now()
     WHERE o.id = p_job;
    stage := j.stage; subject_type := j.subject_type; subject_id := j.subject_id; payload := j.payload; previous_status := j.status;
    RETURN NEXT;
END $$;

-- Aggregates across all workspaces for the global gauges. NO workspace ids, no subjects.
CREATE FUNCTION ops.backlog_stats()
RETURNS TABLE (stage text, status text, jobs bigint, oldest_age_seconds double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT j.stage, j.status, count(*)::bigint,
           EXTRACT(EPOCH FROM now() - min(j.available_at) FILTER (WHERE j.status = 'pending' AND j.available_at <= now()))::double precision
      FROM public.outbox_jobs j WHERE j.status IN ('pending', 'leased', 'dead') GROUP BY j.stage, j.status
$$;

REVOKE ALL ON FUNCTION ops.claim_jobs(text, text[], integer, integer), ops.heartbeat_jobs(text, uuid[], integer),
    ops.complete_job(uuid, uuid, text, text, text, double precision), ops.release_jobs(text), ops.cancel_job(uuid), ops.backlog_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.claim_jobs(text, text[], integer, integer), ops.heartbeat_jobs(text, uuid[], integer),
    ops.complete_job(uuid, uuid, text, text, text, double precision), ops.release_jobs(text), ops.cancel_job(uuid), ops.backlog_stats() TO kg_app;
"""

GRANTS_SQL = """
GRANT SELECT, INSERT ON interactions TO kg_app;
GRANT UPDATE (processing_status, status_changed_at) ON interactions TO kg_app;       -- the envelope itself is immutable
GRANT SELECT, INSERT ON interaction_contents TO kg_app;
GRANT UPDATE (kid, nonce, ciphertext) ON interaction_contents TO kg_app;             -- re-encryption only (kg payloads reencrypt)
GRANT SELECT, INSERT ON interaction_subjects TO kg_app;
GRANT SELECT, INSERT ON interaction_stages TO kg_app;
GRANT UPDATE (generation, status, attempts, last_error_category, last_error_code, result, pipeline_version, ontology_version_id, started_at, finished_at)
    ON interaction_stages TO kg_app;
GRANT SELECT, INSERT ON outbox_jobs TO kg_app;                                       -- UPDATE only through ops.*
GRANT SELECT ON job_attempts TO kg_app;                                              -- written only by ops.*
"""

# Every applied command whose rows are still waiting for the graph gets one job (the sweeper's duty moves to the outbox). Same dedupe key the
# application uses, so a later enqueue for the same command is a no-op.
BACKFILL_SQL = f"""
INSERT INTO outbox_jobs (id, workspace_id, stage, subject_type, subject_id, dedupe_key, payload, priority, max_attempts, pipeline_version, ontology_version_id, correlation_id)
SELECT gen_random_uuid(), c.workspace_id, 'graph_projection', 'command', c.id, 'command:' || c.id::text || ':graph_projection:g0',
       jsonb_build_object('command_id', c.id::text, 'generation', 0), 100, 8, '{PIPELINE_VERSION}', c.ontology_version_id, 'backfill-0003'
  FROM commands c
 WHERE c.status = 'applied'
   AND (c.projection_status IN ('pending', 'failed')
        OR EXISTS (SELECT 1 FROM entities e WHERE e.workspace_id = c.workspace_id AND e.last_command_id = c.id AND e.projection_status IN ('pending', 'failed'))
        OR EXISTS (SELECT 1 FROM facts f WHERE f.workspace_id = c.workspace_id AND f.last_command_id = c.id AND f.projection_status IN ('pending', 'failed')))
ON CONFLICT (workspace_id, dedupe_key) DO NOTHING;
"""  # noqa: S608 (PIPELINE_VERSION is a module constant)


def upgrade() -> None:
    op.execute(TABLES_SQL + TRIGGERS_SQL + POLICY_SQL + OPS_SQL + GRANTS_SQL + BACKFILL_SQL)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS ops.claim_jobs(text, text[], integer, integer);
        DROP FUNCTION IF EXISTS ops.heartbeat_jobs(text, uuid[], integer);
        DROP FUNCTION IF EXISTS ops.complete_job(uuid, uuid, text, text, text, double precision);
        DROP FUNCTION IF EXISTS ops.release_jobs(text);
        DROP FUNCTION IF EXISTS ops.cancel_job(uuid);
        DROP FUNCTION IF EXISTS ops.backlog_stats();
        DROP TABLE IF EXISTS job_attempts, outbox_jobs, interaction_stages, interaction_subjects, interaction_contents, interactions CASCADE;
        DROP FUNCTION IF EXISTS public.job_attempts_guard();
        DROP FUNCTION IF EXISTS public.interactions_guard();
    """)
