"""release hardening: the shared limiter, cache invalidation, OIDC identities, graph namespaces, reindex / reconcile / backup / evaluation records

Revision ID: 0009
Revises: 0008

* **Shared limiter (P9 WP7, V4).** Two UNLOGGED tables, no Redis: `rate_counters` (one upsert per request, fixed windows, judged by the DATABASE clock) and `rate_slots` (a lease per
  in-flight build, so a crashed process frees its slot when the lease expires). Neither holds anything but a key made of ids and a count.
* **Cache invalidation.** `kg_notify_invalidate()` sends `pg_notify('kg_invalidate', '<workspace>:<kind>')` after a change to settings, principals (quotas, status), grants, API keys
  (revocation), the active ontology and the graph namespaces; every API and worker process listens and drops its entry. The TTL caches stay as the fallback.
* **OIDC.** `oidc_group_mappings` (admin-managed: an IdP group becomes roles in one workspace) and `oidc_identities` (issuer + subject -> a `user` principal). `auth.oidc_resolve` is
  the one SECURITY DEFINER entry point: it maps the verified claims to a workspace and roles, provisions the principal on first sight (audited) and returns the same shape as
  `auth.credential_status`. Roles are recomputed from the mapping on every call, so a principal never holds more than the mapping says today.
* **Graph namespaces (Flow F).** `workspace_graph_namespaces` says which Neo4j group_id is the workspace's ACTIVE one. A workspace with no row uses `ws-<uuid>` (the P2-P8 behaviour);
  the existing workspaces are backfilled. `ops.graph_namespace_overrides()` lists the workspaces whose active namespace is not the default, which is all a process needs to cache.
* **Records:** `reindex_runs`, `reconcile_runs` (tenant tables), `backup_runs`, `eval_runs`, `secret_versions` (platform tables: no row security, no grant to the application role, read
  only through `ops.*`; they hold statuses, sizes, hashes and versions, never content or secrets).

Downgrade refuses while a reindex is building (the building namespace would be orphaned); `force_drop_release` drops the records anyway.
"""

import importlib.util
import re
from pathlib import Path

from alembic import context, op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TENANT_TABLES = ["oidc_group_mappings", "oidc_identities", "workspace_graph_namespaces", "reindex_runs", "reconcile_runs"]
NOTIFY_ON = [
    # table, events, kind
    ("workspace_settings", "INSERT", "settings"),
    ("principals", "INSERT OR UPDATE OR DELETE", "principals"),
    ("principal_scopes", "INSERT OR UPDATE OR DELETE", "grants"),
    ("role_assignments", "INSERT OR UPDATE OR DELETE", "grants"),
    ("api_keys", "INSERT OR UPDATE OF revoked_at, expires_at", "revocation"),
    ("workspace_ontology", "INSERT OR UPDATE", "ontology"),
    ("workspace_graph_namespaces", "INSERT OR UPDATE OR DELETE", "namespace"),
    ("oidc_group_mappings", "INSERT OR UPDATE OR DELETE", "oidc"),
]

UP_SQL = """
-- ---- the shared limiter (SECURITY DEFINER functions only: the tables are not granted to the application role) --------------------------------------------
CREATE UNLOGGED TABLE rate_counters (
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    window_start timestamptz NOT NULL,
    count integer NOT NULL CHECK (count >= 0),
    PRIMARY KEY (key, window_start)
);
CREATE INDEX rate_counters_window_idx ON rate_counters (window_start);
CREATE UNLOGGED TABLE rate_slots (
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    holder uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (key, holder)
);
CREATE INDEX rate_slots_expiry_idx ON rate_slots (expires_at);

-- One fixed-window increment. The window and the retry-after are the DATABASE clock's (FI-22: a skewed API host changes nothing). Returns the count INCLUDING this request
-- and the seconds until the window ends.
CREATE FUNCTION ops.rate_hit(p_key text, p_window_seconds integer, p_cost integer DEFAULT 1)
RETURNS TABLE (hits integer, seconds_left double precision)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    w timestamptz := to_timestamp(floor(extract(epoch FROM now()) / p_window_seconds) * p_window_seconds);
BEGIN
    RETURN QUERY
    WITH up AS (
        INSERT INTO rate_counters AS c (key, window_start, count) VALUES (p_key, w, p_cost)
        ON CONFLICT (key, window_start) DO UPDATE SET count = c.count + p_cost
        RETURNING c.count
    )
    SELECT up.count, extract(epoch FROM (w + make_interval(secs => p_window_seconds) - now()))::double precision FROM up;
END $$;

-- A concurrency slot with a lease. The advisory lock (per key, transaction-scoped) serialises admissions to one key, so the cap cannot be exceeded by a race.
CREATE FUNCTION ops.slot_acquire(p_key text, p_holder uuid, p_cap integer, p_lease_seconds integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    taken integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_key, 0));
    DELETE FROM rate_slots WHERE key = p_key AND expires_at <= now();
    SELECT count(*) INTO taken FROM rate_slots WHERE key = p_key;
    IF taken >= p_cap THEN
        RETURN false;
    END IF;
    INSERT INTO rate_slots (key, holder, expires_at) VALUES (p_key, p_holder, now() + make_interval(secs => p_lease_seconds));
    RETURN true;
END $$;

CREATE FUNCTION ops.slot_release(p_key text, p_holder uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$ DELETE FROM rate_slots WHERE key = p_key AND holder = p_holder $$;

CREATE FUNCTION ops.rate_sweep(p_keep_seconds integer DEFAULT 3600) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    a integer; b integer;
BEGIN
    DELETE FROM rate_counters WHERE window_start < now() - make_interval(secs => p_keep_seconds);
    GET DIAGNOSTICS a = ROW_COUNT;
    DELETE FROM rate_slots WHERE expires_at <= now();
    GET DIAGNOSTICS b = ROW_COUNT;
    RETURN a + b;
END $$;

-- ---- tenant tables --------------------------------------------------------------------------------------------------------------------------------
CREATE TABLE oidc_group_mappings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    issuer text NOT NULL CHECK (length(issuer) BETWEEN 1 AND 300),
    group_name text NOT NULL CHECK (length(group_name) BETWEEN 1 AND 200),
    roles text[] NOT NULL CHECK (cardinality(roles) BETWEEN 1 AND 6 AND roles <@ ARRAY['workspace_admin','operator','reviewer','app_writer','agent','reader']),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, issuer, group_name),
    UNIQUE (workspace_id, id)
);
CREATE INDEX oidc_group_mappings_lookup_idx ON oidc_group_mappings (issuer, group_name);

CREATE TABLE oidc_identities (
    workspace_id uuid NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 300),
    principal_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, issuer, subject),
    UNIQUE (principal_id),
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id)
);

CREATE TABLE workspace_graph_namespaces (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    namespace text NOT NULL CHECK (namespace ~ '^ws-[A-Za-z0-9_-]+$'),
    status text NOT NULL CHECK (status IN ('building','active','retired')),
    reindex_run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    retired_at timestamptz,
    cleared_at timestamptz,
    PRIMARY KEY (workspace_id, namespace)
);
CREATE UNIQUE INDEX workspace_graph_namespaces_one_active_idx ON workspace_graph_namespaces (workspace_id) WHERE status = 'active';
INSERT INTO workspace_graph_namespaces (workspace_id, namespace, status, activated_at) SELECT id, graph_group_id, 'active', created_at FROM workspaces;

CREATE TABLE reindex_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    ontology_version_id uuid,
    namespace text NOT NULL,
    previous_namespace text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('project','reextract')),
    status text NOT NULL CHECK (status IN ('preview','building','catching_up','ready','activated','failed','cancelled','rolled_back')),
    estimate jsonb NOT NULL DEFAULT '{}',
    progress jsonb NOT NULL DEFAULT '{}',
    report jsonb,
    error_code text,
    started_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    rolled_back_at timestamptz,
    UNIQUE (workspace_id, id)
);
CREATE INDEX reindex_runs_ws_idx ON reindex_runs (workspace_id, created_at DESC);
ALTER TABLE workspace_graph_namespaces ADD FOREIGN KEY (workspace_id, reindex_run_id) REFERENCES reindex_runs(workspace_id, id);

CREATE TABLE reconcile_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    scope text NOT NULL CHECK (scope IN ('all','graph','memory','ledger','deletions')),
    mode text NOT NULL CHECK (mode IN ('check','repair')),
    trigger text NOT NULL CHECK (trigger IN ('manual','scheduled','restore')),
    status text NOT NULL CHECK (status IN ('running','completed','failed')),
    drift jsonb NOT NULL DEFAULT '{}',
    repaired jsonb NOT NULL DEFAULT '{}',
    error_code text,
    started_by uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX reconcile_runs_ws_idx ON reconcile_runs (workspace_id, started_at DESC);

-- ---- platform tables (statuses, sizes, hashes, versions: no content, no secrets) -----------------------------------------------------------------
CREATE TABLE backup_runs (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('backup','prune','restore_rehearsal')),
    store text NOT NULL CHECK (store IN ('postgres','neo4j','config','keys','all')),
    status text NOT NULL CHECK (status IN ('running','completed','failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    location text CHECK (location IS NULL OR length(location) <= 300),
    expires_at timestamptz,
    recipients_fingerprint text,
    error_code text,
    details jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX backup_runs_started_idx ON backup_runs (started_at DESC);

CREATE TABLE eval_runs (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('live_session','eval_gate','eval_gate_live','release_sweep','resilience','baseline','scan')),
    label text,
    status text NOT NULL CHECK (status IN ('passed','failed','partial')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    git_commit text,
    summary jsonb NOT NULL DEFAULT '{}',
    report_ref text CHECK (report_ref IS NULL OR length(report_ref) <= 300)
);
CREATE INDEX eval_runs_started_idx ON eval_runs (kind, started_at DESC);

CREATE TABLE secret_versions (
    kind text NOT NULL CHECK (kind IN ('content_hmac','auth_pepper','jwt','payload_key','router_key','neo4j_password','postgres_password')),
    version text NOT NULL CHECK (version ~ '^[A-Za-z0-9_.-]{1,40}$'),
    status text NOT NULL CHECK (status IN ('active','retiring','retired')),
    fingerprint text CHECK (fingerprint IS NULL OR fingerprint ~ '^[0-9a-f]{16}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    retire_after timestamptz,
    retired_at timestamptz,
    PRIMARY KEY (kind, version)
);

-- ---- cache invalidation ---------------------------------------------------------------------------------------------------------------------------
CREATE FUNCTION public.kg_notify_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ws uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN ws := OLD.workspace_id; ELSE ws := NEW.workspace_id; END IF;
    PERFORM pg_notify('kg_invalidate', ws::text || ':' || TG_ARGV[0]);
    RETURN NULL;
END $$;
"""

NOTIFY_SQL = "".join(
    f"CREATE TRIGGER kg_invalidate_{table} AFTER {events} ON {table} FOR EACH ROW EXECUTE FUNCTION public.kg_notify_invalidate('{kind}');\n"
    for table, events, kind in NOTIFY_ON
)

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in TENANT_TABLES
)

OPS_SQL = """
-- the processes ask this once (and again on a `namespace` notification): the workspaces whose ACTIVE graph namespace is not the default. Names workspace ids and namespaces only.
CREATE FUNCTION ops.graph_namespace_overrides() RETURNS TABLE (workspace_id uuid, namespace text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT n.workspace_id, n.namespace FROM public.workspace_graph_namespaces n JOIN public.workspaces w ON w.id = n.workspace_id
     WHERE n.status = 'active' AND n.namespace <> w.graph_group_id
$$;

-- every namespace a deletion must reach (active, building and retired alike), so a deletion journal replay or a workspace deletion also clears the namespaces kept for rollback
CREATE FUNCTION ops.graph_namespaces_of(p_workspace uuid) RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT n.namespace FROM public.workspace_graph_namespaces n WHERE n.workspace_id = p_workspace AND n.cleared_at IS NULL
$$;

-- gauges and the console: aggregates, no content
CREATE FUNCTION ops.reconcile_drift() RETURNS TABLE (drift_class text, n bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT d.key, COALESCE(sum((d.value)::bigint), 0)::bigint
      FROM (SELECT DISTINCT ON (r.workspace_id) r.drift FROM public.reconcile_runs r WHERE r.status = 'completed' ORDER BY r.workspace_id, r.started_at DESC) latest,
           LATERAL jsonb_each_text(latest.drift) d
     GROUP BY d.key
$$;

CREATE FUNCTION ops.backup_status(p_limit integer DEFAULT 50) RETURNS SETOF public.backup_runs
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT * FROM public.backup_runs ORDER BY started_at DESC LIMIT GREATEST(1, LEAST(p_limit, 500))
$$;

CREATE FUNCTION ops.eval_status(p_limit integer DEFAULT 50) RETURNS SETOF public.eval_runs
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT * FROM public.eval_runs ORDER BY started_at DESC LIMIT GREATEST(1, LEAST(p_limit, 500))
$$;

CREATE FUNCTION ops.secret_status() RETURNS SETOF public.secret_versions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT * FROM public.secret_versions ORDER BY kind, created_at DESC
$$;

-- age in seconds of the newest completed backup per store (null: never), for the alert rule
CREATE FUNCTION ops.backup_ages() RETURNS TABLE (store text, age_seconds double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT s.store, extract(epoch FROM (now() - max(b.completed_at)))::double precision
      FROM (VALUES ('postgres'),('neo4j'),('config'),('keys')) s(store)
      LEFT JOIN public.backup_runs b ON b.store = s.store AND b.kind = 'backup' AND b.status = 'completed'
     GROUP BY s.store
$$;

-- the nightly scan: claims ONE workspace that has had no scheduled scan inside the window (a crashed 'running' scan blocks for an hour, not for ever) and opens its run, atomically,
-- so several workers never scan the same workspace twice. Names a workspace id and a run id, nothing else.
CREATE FUNCTION ops.claim_reconcile_scan(p_hours double precision) RETURNS TABLE (ws_id uuid, run uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    picked uuid;
    rid uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('reconcile_scan', 0));
    SELECT w.id INTO picked FROM public.workspaces w
     WHERE w.status = 'active' AND NOT EXISTS (
         SELECT 1 FROM public.reconcile_runs r WHERE r.workspace_id = w.id AND r.trigger = 'scheduled'
            AND r.started_at > now() - make_interval(secs => p_hours * 3600)
            AND NOT (r.status = 'running' AND r.started_at < now() - interval '1 hour'))
     ORDER BY w.created_at LIMIT 1;
    IF picked IS NULL THEN RETURN; END IF;
    rid := gen_random_uuid();
    INSERT INTO public.reconcile_runs (id, workspace_id, scope, mode, trigger, status) VALUES (rid, picked, 'all', 'check', 'scheduled', 'running');
    ws_id := picked; run := rid;
    RETURN NEXT;
END $$;

CREATE FUNCTION ops.reindex_stats() RETURNS TABLE (status text, runs bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT r.status, count(*)::bigint FROM public.reindex_runs r GROUP BY r.status
$$;

REVOKE ALL ON FUNCTION ops.rate_hit(text, integer, integer), ops.slot_acquire(text, uuid, integer, integer), ops.slot_release(text, uuid), ops.rate_sweep(integer),
    ops.graph_namespace_overrides(), ops.graph_namespaces_of(uuid), ops.reconcile_drift(), ops.backup_status(integer), ops.eval_status(integer), ops.secret_status(),
    ops.backup_ages(), ops.claim_reconcile_scan(double precision), ops.reindex_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.rate_hit(text, integer, integer), ops.slot_acquire(text, uuid, integer, integer), ops.slot_release(text, uuid), ops.rate_sweep(integer),
    ops.graph_namespace_overrides(), ops.graph_namespaces_of(uuid), ops.reconcile_drift(), ops.backup_status(integer), ops.eval_status(integer), ops.secret_status(),
    ops.backup_ages(), ops.claim_reconcile_scan(double precision), ops.reindex_stats() TO kg_app;

-- ---- OIDC: the one entry point from verified claims to a principal ------------------------------------------------------------------------------------
CREATE FUNCTION auth.oidc_resolve(p_issuer text, p_subject text, p_groups text[], p_workspace_slug text, p_display_name text)
RETURNS TABLE (principal_id uuid, workspace_id uuid, kind text, handle text, principal_status text, workspace_status text, roles text[], provisioned boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    ws uuid;
    n_ws integer;
    want text[];
    pid uuid;
    have text[];
    created boolean := false;
    h text;
BEGIN
    IF p_issuer IS NULL OR p_subject IS NULL OR length(p_subject) = 0 THEN RETURN; END IF;
    -- the workspaces this token's groups map into (restricted to the named workspace when the token names one)
    SELECT count(DISTINCT m.workspace_id), min(m.workspace_id::text)::uuid INTO n_ws, ws
      FROM public.oidc_group_mappings m JOIN public.workspaces w ON w.id = m.workspace_id
     WHERE m.issuer = p_issuer AND m.group_name = ANY(COALESCE(p_groups, ARRAY[]::text[])) AND w.status = 'active'
       AND (p_workspace_slug IS NULL OR w.slug = p_workspace_slug);
    IF n_ws IS NULL OR n_ws <> 1 THEN RETURN; END IF;   -- unmapped, or ambiguous without a workspace claim: no principal
    SELECT COALESCE(array_agg(DISTINCT r ORDER BY r), ARRAY[]::text[]) INTO want
      FROM public.oidc_group_mappings m, LATERAL unnest(m.roles) r
     WHERE m.workspace_id = ws AND m.issuer = p_issuer AND m.group_name = ANY(COALESCE(p_groups, ARRAY[]::text[]));
    SELECT i.principal_id INTO pid FROM public.oidc_identities i WHERE i.workspace_id = ws AND i.issuer = p_issuer AND i.subject = p_subject;
    IF pid IS NULL THEN
        pid := gen_random_uuid();
        h := 'oidc-' || substr(md5(p_issuer || '|' || p_subject), 1, 16);
        INSERT INTO public.principals (id, workspace_id, kind, handle, display_name) VALUES (pid, ws, 'user', h, left(COALESCE(NULLIF(p_display_name, ''), h), 100));
        INSERT INTO public.oidc_identities (workspace_id, issuer, subject, principal_id) VALUES (ws, p_issuer, p_subject, pid);
        INSERT INTO public.audit_log (id, workspace_id, actor_principal_id, action, resource_type, resource_id, outcome, metadata)
        VALUES (gen_random_uuid(), ws, pid, 'oidc.user_provisioned', 'principal', pid::text, 'allowed', jsonb_build_object('roles', want));
        created := true;
    ELSE
        UPDATE public.oidc_identities i SET last_login_at = now() WHERE i.workspace_id = ws AND i.issuer = p_issuer AND i.subject = p_subject
           AND i.last_login_at < now() - interval '1 minute';
    END IF;
    SELECT COALESCE(array_agg(a.role ORDER BY a.role), ARRAY[]::text[]) INTO have FROM public.role_assignments a WHERE a.principal_id = pid;
    IF have IS DISTINCT FROM want THEN
        DELETE FROM public.role_assignments a WHERE a.principal_id = pid AND a.role <> ALL (want);
        INSERT INTO public.role_assignments (principal_id, workspace_id, role) SELECT pid, ws, r FROM unnest(want) r ON CONFLICT DO NOTHING;
        IF NOT created THEN
            INSERT INTO public.audit_log (id, workspace_id, actor_principal_id, action, resource_type, resource_id, outcome, metadata)
            VALUES (gen_random_uuid(), ws, pid, 'oidc.roles_changed', 'principal', pid::text, 'allowed', jsonb_build_object('roles', want));
        END IF;
    END IF;
    RETURN QUERY
    SELECT p.id, p.workspace_id, p.kind, p.handle, p.status, w.status, want, created
      FROM public.principals p JOIN public.workspaces w ON w.id = p.workspace_id WHERE p.id = pid AND p.workspace_id = ws;
END $$;
REVOKE ALL ON FUNCTION auth.oidc_resolve(text, text, text[], text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.oidc_resolve(text, text, text[], text, text) TO kg_app;
"""

GRANTS_SQL = """
-- rate_counters and rate_slots: no grant. The application role reaches them only through the SECURITY DEFINER ops.rate_* / ops.slot_* functions.
GRANT SELECT, INSERT, DELETE ON oidc_group_mappings TO kg_app;
GRANT SELECT ON oidc_identities TO kg_app;
GRANT SELECT, INSERT, UPDATE ON workspace_graph_namespaces, reindex_runs, reconcile_runs TO kg_app;
-- backup_runs, eval_runs and secret_versions: no grant. The tools write them as the owner; the application reads them through ops.*.
"""

PURGE_FN = re.compile(r"CREATE FUNCTION ops\.purge_workspace_rows.*?END \$\$;", re.S)


def _0008_purge_function() -> str:
    """The 0008 definition of ops.purge_workspace_rows, read from that migration so the two never drift."""
    path = Path(__file__).with_name("0008_governance.py")
    spec = importlib.util.spec_from_file_location("kg_migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    match = PURGE_FN.search(module.OPS_SQL)
    assert match is not None
    return match.group(0)


def _purge_with_release_tables() -> str:
    fn = _0008_purge_function().replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    # the new tenant tables reference principals / workspaces, so they go first
    return fn.replace(
        "tables text[] := ARRAY['context_items'",
        "tables text[] := ARRAY['reconcile_runs','oidc_identities','oidc_group_mappings','workspace_graph_namespaces','reindex_runs','context_items'",
        1,
    )


def _force() -> bool:
    return bool(context.config.attributes.get("force_drop_release"))


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + NOTIFY_SQL + OPS_SQL + GRANTS_SQL)
    op.execute(_purge_with_release_tables())


def downgrade() -> None:
    bind = op.get_bind()
    building = bind.exec_driver_sql("SELECT count(*) FROM reindex_runs WHERE status IN ('building','catching_up','ready')").scalar_one()
    if building and not _force():
        raise RuntimeError(
            f"refusing to downgrade 0009: {building} reindex runs are building or ready to activate (their namespaces would be orphaned in Neo4j). "
            "Cancel them first, or pass --force-drop-release to drop the records anyway."
        )
    op.execute(_0008_purge_function().replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    for table, _events, _kind in NOTIFY_ON:
        op.execute(f"DROP TRIGGER IF EXISTS kg_invalidate_{table} ON {table}")
    op.execute(
        """
DROP FUNCTION IF EXISTS public.kg_notify_invalidate();
DROP FUNCTION IF EXISTS auth.oidc_resolve(text, text, text[], text, text);
DROP FUNCTION IF EXISTS ops.reindex_stats();
DROP FUNCTION IF EXISTS ops.claim_reconcile_scan(double precision);
DROP FUNCTION IF EXISTS ops.backup_ages();
DROP FUNCTION IF EXISTS ops.secret_status();
DROP FUNCTION IF EXISTS ops.eval_status(integer);
DROP FUNCTION IF EXISTS ops.backup_status(integer);
DROP FUNCTION IF EXISTS ops.reconcile_drift();
DROP FUNCTION IF EXISTS ops.graph_namespaces_of(uuid);
DROP FUNCTION IF EXISTS ops.graph_namespace_overrides();
DROP FUNCTION IF EXISTS ops.rate_sweep(integer);
DROP FUNCTION IF EXISTS ops.slot_release(text, uuid);
DROP FUNCTION IF EXISTS ops.slot_acquire(text, uuid, integer, integer);
DROP FUNCTION IF EXISTS ops.rate_hit(text, integer, integer);
DROP TABLE IF EXISTS secret_versions, eval_runs, backup_runs, reconcile_runs, oidc_identities, oidc_group_mappings, rate_slots, rate_counters CASCADE;
ALTER TABLE IF EXISTS workspace_graph_namespaces DROP CONSTRAINT IF EXISTS workspace_graph_namespaces_workspace_id_reindex_run_id_fkey;
DROP TABLE IF EXISTS reindex_runs, workspace_graph_namespaces CASCADE;
"""
    )
