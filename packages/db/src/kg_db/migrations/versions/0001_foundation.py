"""foundation: workspaces, principals, roles, scopes, api keys, audit, model call log, worker heartbeats; RLS; auth functions

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = ["principals", "role_assignments", "principal_scopes", "api_keys", "audit_log"]
ROLES = "('workspace_admin','operator','reviewer','app_writer','agent','reader')"

UPGRADE_SQL = f"""
CREATE SCHEMA IF NOT EXISTS auth;

-- Fail closed: a missing OR empty setting (transaction-local settings revert to '' on a reused connection) yields NULL, never a row.
CREATE FUNCTION public.current_workspace_id() RETURNS uuid LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid $$;

CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    graph_group_id text GENERATED ALWAYS AS ('ws-' || id::text) STORED UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principals (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kind text NOT NULL CHECK (kind IN ('user','app','agent','service')),
    handle text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    owner_principal_id uuid REFERENCES principals(id),
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, handle),
    UNIQUE (workspace_id, id)
);

CREATE TABLE role_assignments (
    principal_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN {ROLES}),
    granted_by uuid,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, role),
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id)
);

CREATE TABLE principal_scopes (
    id uuid PRIMARY KEY,
    principal_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    scope_kind text NOT NULL CHECK (scope_kind IN ('workspace','subject','project','agent','session')),
    scope_ref text NOT NULL,
    actions text[] NOT NULL DEFAULT '{{}}',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id)
);

CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    prefix text NOT NULL UNIQUE,
    key_hash text NOT NULL,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz,
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id)
);
CREATE INDEX api_keys_principal_idx ON api_keys (principal_id);

CREATE TABLE audit_log (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    actor_principal_id uuid,
    action text NOT NULL,
    resource_type text,
    resource_id text,
    outcome text NOT NULL CHECK (outcome IN ('allowed','denied','error')),
    request_id text,
    correlation_id text,
    metadata jsonb NOT NULL DEFAULT '{{}}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_ws_created_idx ON audit_log (workspace_id, created_at DESC, id DESC);

CREATE FUNCTION public.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'insufficient_privilege'; END $$;
CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION public.audit_log_immutable();

CREATE TABLE model_calls (
    id uuid PRIMARY KEY,
    workspace_id uuid REFERENCES workspaces(id),
    purpose text NOT NULL,
    alias text NOT NULL,
    operation text NOT NULL,
    classification text NOT NULL,
    correlation_id text,
    router_request_id text,
    prompt_tokens integer,
    completion_tokens integer,
    usage_estimated boolean NOT NULL DEFAULT false,
    est_cost_usd numeric(14,8),
    latency_ms double precision,
    attempts integer NOT NULL DEFAULT 1,
    outcome text NOT NULL,
    fallback_used boolean NOT NULL DEFAULT false,
    tokenizer text,
    service text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_calls_ws_created_idx ON model_calls (workspace_id, created_at DESC);

CREATE TABLE worker_heartbeats (
    worker_id text PRIMARY KEY,
    hostname text NOT NULL,
    version text NOT NULL,
    started_at timestamptz NOT NULL,
    last_beat_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('running','stopped'))
);

-- ---- row-level security: ENABLE + FORCE on every tenant table; no setting => no rows ----------------------------------------------
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON workspaces USING (id = public.current_workspace_id()) WITH CHECK (id = public.current_workspace_id());
"""

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in TENANT_TABLES
)

MODEL_CALLS_POLICY = """
ALTER TABLE model_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_or_system ON model_calls
    USING (workspace_id IS NULL OR workspace_id = public.current_workspace_id())
    WITH CHECK (workspace_id IS NULL OR workspace_id = public.current_workspace_id());
"""

# ---- SECURITY DEFINER auth functions (L3). Owned by kg_owner (BYPASSRLS), pinned search_path, minimal columns. -----------------------
FUNCTIONS_SQL = """
CREATE FUNCTION auth.lookup_credential(p_prefix text)
RETURNS TABLE (key_id uuid, key_hash text, principal_id uuid, workspace_id uuid, kind text, handle text, principal_status text,
               key_expires_at timestamptz, key_revoked_at timestamptz, key_expired boolean, workspace_status text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    -- expiry is judged by the database clock: comparing a DB timestamp with the API host's clock is wrong when the two drift
    SELECT k.id, k.key_hash, p.id, p.workspace_id, p.kind, p.handle, p.status, k.expires_at, k.revoked_at,
           (k.expires_at IS NOT NULL AND k.expires_at <= now()), w.status,
           COALESCE((SELECT array_agg(r.role ORDER BY r.role) FROM public.role_assignments r WHERE r.principal_id = p.id), ARRAY[]::text[])
    FROM public.api_keys k
    JOIN public.principals p ON p.id = k.principal_id AND p.workspace_id = k.workspace_id
    JOIN public.workspaces w ON w.id = p.workspace_id
    WHERE k.prefix = p_prefix
$$;

CREATE FUNCTION auth.credential_status(p_key_id uuid, p_principal_id uuid)
RETURNS TABLE (key_id uuid, principal_id uuid, workspace_id uuid, kind text, handle text, principal_status text,
               key_expires_at timestamptz, key_revoked_at timestamptz, key_expired boolean, workspace_status text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT k.id, p.id, p.workspace_id, p.kind, p.handle, p.status, k.expires_at, k.revoked_at,
           (k.expires_at IS NOT NULL AND k.expires_at <= now()), w.status,
           COALESCE((SELECT array_agg(r.role ORDER BY r.role) FROM public.role_assignments r WHERE r.principal_id = p.id), ARRAY[]::text[])
    FROM public.api_keys k
    JOIN public.principals p ON p.id = k.principal_id AND p.workspace_id = k.workspace_id
    JOIN public.workspaces w ON w.id = p.workspace_id
    WHERE k.id = p_key_id AND p.id = p_principal_id
$$;

CREATE FUNCTION auth.touch_key(p_key_id uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    UPDATE public.api_keys SET last_used_at = now() WHERE id = p_key_id
$$;

REVOKE ALL ON FUNCTION auth.lookup_credential(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.credential_status(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.touch_key(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA auth TO kg_app;
GRANT EXECUTE ON FUNCTION auth.lookup_credential(text), auth.credential_status(uuid, uuid), auth.touch_key(uuid) TO kg_app;
"""

GRANTS_SQL = """
GRANT USAGE ON SCHEMA public TO kg_app;
GRANT SELECT, INSERT, UPDATE ON workspaces, principals TO kg_app;
GRANT SELECT, INSERT, DELETE ON role_assignments TO kg_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON principal_scopes TO kg_app;
-- key_hash is never readable by the application role: authentication goes through auth.lookup_credential()
GRANT SELECT (id, workspace_id, principal_id, prefix, created_by, created_at, expires_at, revoked_at, last_used_at) ON api_keys TO kg_app;
GRANT INSERT ON api_keys TO kg_app;
GRANT UPDATE (revoked_at, expires_at) ON api_keys TO kg_app;
GRANT SELECT, INSERT ON audit_log, model_calls TO kg_app;
GRANT SELECT, INSERT, UPDATE ON worker_heartbeats TO kg_app;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL + POLICY_SQL + MODEL_CALLS_POLICY + FUNCTIONS_SQL + GRANTS_SQL)


def downgrade() -> None:
    op.execute("""
        DROP SCHEMA IF EXISTS auth CASCADE;
        DROP TABLE IF EXISTS worker_heartbeats, model_calls, audit_log, api_keys, principal_scopes, role_assignments, principals, workspaces CASCADE;
        DROP FUNCTION IF EXISTS public.audit_log_immutable();
        DROP FUNCTION IF EXISTS public.current_workspace_id();
    """)
