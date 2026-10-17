"""context traces: what a context build did, as metadata only; opt-in encrypted debug snapshots; retention functions

Revision ID: 0006
Revises: 0005

A context package is NOT stored (phase-6 plan §5.12): `GET /v1/context/{id}` re-materialises it from the stored item ids under the caller's CURRENT authorisation, so a
memory superseded, a principal revoked or a record later deleted never resurfaces from a stored copy. What is stored is the explanation: the plan's shape, the versions
(ranker, tokenizer, compression, pipeline), authorised candidate counts per source, every authorised candidate with its score, features, tier and exclusion reason, the
token accounting, the watermarks and the latencies. The task and the query are kept only as keyed hashes (S8); no rendered text is stored unless an admin asks for a
debug snapshot, which is encrypted with the payload cipher (AAD workspace|context|debug) and carries an expiry.

Conventions follow 0001-0005: workspace_id on every tenant table, composite foreign keys, ENABLE + FORCE row-level security, column-level grants, DB-clock timestamps.
`kg_app` can INSERT and SELECT traces and items; there is no UPDATE and no DELETE. Retention is the SECURITY DEFINER `ops.purge_expired_context_traces`, which returns a
count and nothing else. Downgrade drops the tables: traces are disposable.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = ["context_traces", "context_items", "context_debug_snapshots"]

UP_SQL = """
CREATE TABLE context_traces (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    principal_id uuid NOT NULL,
    act_as_by uuid,
    profile text NOT NULL CHECK (profile ~ '^[a-z][a-z0-9_]{0,63}$'),
    profile_source text NOT NULL CHECK (profile_source IN ('builtin','ontology')),
    ontology_version_id uuid,
    status text NOT NULL CHECK (status IN ('complete','partial','insufficient_evidence','insufficient_budget')),
    compression text NOT NULL CHECK (compression IN ('structured','extractive','summarized')),
    task_hmac text NOT NULL CHECK (task_hmac ~ '^[0-9a-f]{64}$'),
    task_tokens integer NOT NULL CHECK (task_tokens >= 0),
    query_hmac text NOT NULL CHECK (query_hmac ~ '^[0-9a-f]{64}$'),
    request jsonb NOT NULL,        -- the non-content parameters: subject ids, as_of, known_at, caps, include, allow_partial, response_mode
    plan jsonb NOT NULL,           -- the retrieval plan's shape (never its text)
    versions jsonb NOT NULL,       -- ranker, tokenizer, compression, pipeline, embedding
    counts jsonb NOT NULL,         -- authorised candidates per source and exclusions per reason
    unavailable jsonb NOT NULL DEFAULT '[]'::jsonb,
    watermarks jsonb NOT NULL DEFAULT '{}'::jsonb,
    latencies jsonb NOT NULL DEFAULT '{}'::jsonb,
    max_tokens integer NOT NULL,
    used_tokens integer NOT NULL,
    required_tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, principal_id) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, act_as_by) REFERENCES principals(workspace_id, id),
    FOREIGN KEY (workspace_id, ontology_version_id) REFERENCES ontology_versions(workspace_id, id)
);
CREATE INDEX context_traces_ws_created_idx ON context_traces (workspace_id, created_at DESC, id DESC);
CREATE INDEX context_traces_principal_idx ON context_traces (workspace_id, principal_id, created_at DESC, id DESC);

CREATE TABLE context_items (
    context_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    handle text,
    item_kind text NOT NULL CHECK (item_kind IN ('fact','memory','unknown','conflict')),
    item_id text CHECK (item_id IS NULL OR length(item_id) <= 128),   -- a fact or memory id, or `<signal>:<subject>` for an unknown
    section text NOT NULL,
    tier text NOT NULL CHECK (tier IN ('required','high','optional')),
    score numeric(12,9) NOT NULL,
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    tokens integer NOT NULL DEFAULT 0,
    included boolean NOT NULL,
    exclusion_reason text CHECK (exclusion_reason IS NULL OR exclusion_reason ~ '^[a-z][a-z_]{0,31}$'),
    duplicate_of text CHECK (duplicate_of IS NULL OR length(duplicate_of) <= 128),
    PRIMARY KEY (context_id, position),
    CHECK (included = (exclusion_reason IS NULL)),
    FOREIGN KEY (workspace_id, context_id) REFERENCES context_traces(workspace_id, id) ON DELETE CASCADE
);
-- "used in contexts" (Memory Explorer, admin): which builds included this fact or memory
CREATE INDEX context_items_ref_idx ON context_items (workspace_id, item_kind, item_id) WHERE item_id IS NOT NULL;

CREATE TABLE context_debug_snapshots (
    context_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    kid text NOT NULL CHECK (kid ~ '^[A-Za-z0-9_.-]{1,32}$'),
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at),
    FOREIGN KEY (workspace_id, context_id) REFERENCES context_traces(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, created_by) REFERENCES principals(workspace_id, id)
);
CREATE INDEX context_debug_expiry_idx ON context_debug_snapshots (expires_at);

-- ---- retention: aggregates only, across workspaces, no ids and no content (like ops.memory_stats) -----------------------------------------------
CREATE FUNCTION ops.purge_expired_context_traces(retention interval) RETURNS bigint
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    WITH gone AS (DELETE FROM public.context_traces WHERE created_at < now() - retention RETURNING 1)
    SELECT count(*)::bigint FROM gone
$$;
CREATE FUNCTION ops.purge_expired_context_debug() RETURNS bigint
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    WITH gone AS (DELETE FROM public.context_debug_snapshots WHERE expires_at <= now() RETURNING 1)
    SELECT count(*)::bigint FROM gone
$$;
REVOKE ALL ON FUNCTION ops.purge_expired_context_traces(interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION ops.purge_expired_context_debug() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.purge_expired_context_traces(interval) TO kg_app;
GRANT EXECUTE ON FUNCTION ops.purge_expired_context_debug() TO kg_app;
"""

POLICY_SQL = "".join(
    f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (workspace_id = public.current_workspace_id()) WITH CHECK (workspace_id = public.current_workspace_id());
"""
    for t in NEW_TENANT_TABLES
)

GRANTS_SQL = """
GRANT SELECT, INSERT ON context_traces, context_items, context_debug_snapshots TO kg_app;    -- append-only; retention goes through ops.purge_* (SECURITY DEFINER)
"""

DOWN_SQL = """
DROP FUNCTION IF EXISTS ops.purge_expired_context_debug();
DROP FUNCTION IF EXISTS ops.purge_expired_context_traces(interval);
DROP TABLE IF EXISTS context_debug_snapshots, context_items, context_traces CASCADE;
"""


def upgrade() -> None:
    op.execute(UP_SQL + POLICY_SQL + GRANTS_SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
