"""Process configuration. Secrets are SecretStr; production refuses development defaults."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from kg_common.secrets import RingError, SecretRing

REPO_ROOT = Path(__file__).resolve().parents[4]

DEV_PEPPER = "dev-only-pepper-change-me-32-bytes-min"  # noqa: S105 (documented development default; production refuses it)
DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-32-bytes"  # noqa: S105
DEV_TELEMETRY_SECRET = "dev-only-telemetry-secret"  # noqa: S105
DEV_CONTENT_HMAC_SECRET = "dev-only-content-hmac-secret-32-bytes"  # noqa: S105
# Development payload keyring: kid "dev1" is base64 of 32 bytes. Production refuses it (and any ring whose active kid is missing).
DEV_PAYLOAD_KEY_B64 = "ZGV2LW9ubHktcGF5bG9hZC1rZXktMzItYnl0ZXMtISE="  # noqa: S105 (documented development default)
DEV_PAYLOAD_KEYS = f"dev1:{DEV_PAYLOAD_KEY_B64}"
MIN_SECRET_BYTES = 32


class _FileSecretsSource(PydanticBaseSettingsSource):
    """`NAME_FILE=/run/secrets/x` means "the value of NAME is the content of that file" (Docker / Kubernetes secrets; trailing newlines are stripped). It wins over `NAME` itself
    and over the .env file: a container that was given a secret file means that file. An unreadable file is a startup error, never a silent fallback to a default."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        path = os.environ.get(f"{field_name.upper()}_FILE", "")
        if not path:
            return None, field_name, False
        try:
            return Path(path).read_text().rstrip("\r\n"), field_name, False
        except OSError as exc:
            raise ValueError(f"{field_name.upper()}_FILE={path} cannot be read: {exc.strerror}") from exc

    def __call__(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            value, _, _ = self.get_field_value(field, name)
            if value is not None:
                out[name] = value
        return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _FileSecretsSource(settings_cls), env_settings, dotenv_settings, file_secret_settings)

    env: Literal["dev", "test", "production"] = "dev"
    service_name: str = "kg-api"

    # Postgres: the app role (RLS applies) and the owner role (migrations only)
    database_url_app: SecretStr = SecretStr("postgresql+psycopg://kg_app:kg_app_dev@127.0.0.1:5544/kg")
    database_url_owner: SecretStr = SecretStr("postgresql+psycopg://kg_owner:kg_owner_dev@127.0.0.1:5544/kg")
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # Neo4j
    neo4j_uri: str = "bolt://127.0.0.1:7688"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("kg-neo4j-dev")

    # Model router
    oso_router_mode: Literal["mock", "live"] = "mock"
    oso_router_mock_url: str = "http://127.0.0.1:8782/v1"
    oso_router_live_url: str = "http://127.0.0.1:8780/v1"
    oso_router_api_key_api: SecretStr = SecretStr("mock-key")
    oso_router_api_key_worker: SecretStr = SecretStr("mock-key")
    model_purposes_path: Path = REPO_ROOT / "compat/model-purposes.yaml"
    pricing_path: Path = REPO_ROOT / "compat/pricing.yaml"

    # Auth
    auth_key_pepper: SecretStr = SecretStr(DEV_PEPPER)
    auth_jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    auth_jwt_ttl_seconds: int = 900
    telemetry_hmac_secret: SecretStr = SecretStr(DEV_TELEMETRY_SECRET)

    # HTTP
    max_body_bytes: int = 1024 * 1024  # the largest request body of any route (an interaction, an ontology)
    max_body_bytes_default: int = Field(default=256 * 1024, ge=1024)  # every other route (P9: a body limit per route class)
    metrics_host: str = "0.0.0.0"  # noqa: S104 (inside the container network; production publishes no metrics port and the proxy exposes none)
    cors_allow_origins: list[str] = Field(default_factory=list)
    metrics_port_api: int = 9100
    metrics_port_worker: int = 9101

    # Observability
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str = ""

    # Worker
    worker_heartbeat_seconds: float = 10.0
    worker_stale_seconds: float = 45.0

    # Outbox dispatcher (P3). Leases and backoff are judged by the database clock; these are durations only.
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_stage_limits: str = "graph_projection=2,graph_ingest=2"  # "stage=n,stage=n": per-stage slot caps inside worker_concurrency
    worker_lease_seconds: int = Field(default=30, ge=1, le=900)
    worker_poll_busy_seconds: float = 0.2
    worker_poll_idle_seconds: float = 2.0
    worker_shutdown_grace_seconds: float = 20.0

    # Interaction ledger (P3)
    interaction_max_content_bytes: int = 256 * 1024
    # "kid:base64(32 bytes),kid:base64(32 bytes)": the keyring for payload encryption at rest (AES-256-GCM). New writes use payload_active_kid.
    payload_keys: SecretStr = SecretStr(DEV_PAYLOAD_KEYS)
    payload_active_kid: str = "dev1"
    # HKDF master for the per-workspace content HMAC. NOT the payload key: rotating payload keys must not change request hashes (idempotency).
    content_hmac_secret: SecretStr = SecretStr(DEV_CONTENT_HMAC_SECRET)

    # Extraction and identity (P4). Thresholds are placeholders until the live evaluation sets them (ADR-013: E5 similarities cluster high).
    identity_trgm_min: float = Field(default=0.45, ge=0.0, le=1.0)  # pg_trgm similarity of names for an identity candidate
    identity_cos_min: float = Field(default=0.92, ge=0.0, le=1.0)  # cosine of the E5 name embeddings for an identity candidate
    graph_store_episode_content: bool = (
        True  # Graphiti keeps the episode text in Neo4j (inside the trusted boundary; EX-06). False blanks it after extraction.
    )
    graph_previous_episodes: int = Field(
        default=0, ge=0, le=10
    )  # earlier episodes sent as extraction context (0: each interaction stands alone)
    graph_ingest_lock_wait_seconds: float = 120.0
    graph_ingest_timeout_seconds: float = (
        600.0  # one extraction attempt, model calls included; longer is a retryable failure (the lock must not be held forever)
    )

    # Curated memory (P5). The cosine floor of the vector half of a search is a placeholder until the live evaluation sets it from data (ADR-013: E5
    # similarities cluster high, so a real floor is much higher than the mock's).
    memory_vec_min_cosine: float = Field(default=0.2, ge=-1.0, le=1.0)
    memory_propose_timeout_seconds: float = Field(default=120.0, gt=0.0)  # one extraction attempt, model call included
    memory_embedding_version: int = Field(
        default=1, ge=1
    )  # the vector version a read pins (a re-embed to N+1 switches this after coverage is 100%)

    # Context engine (P6). The token margin is PROVISIONAL until TK-02 measures the router's prompt_tokens (ADR-020); every trace records the tokenizer id.
    context_api_enabled: bool = True  # False: POST /v1/context/build answers 503 feature_disabled (rollback switch)
    context_token_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    context_source_timeout_ms: int = Field(
        default=1500, ge=50, le=60_000
    )  # per retriever; a source over it is `unavailable` and the package `partial`
    context_trace_retention_days: int = Field(default=30, ge=1, le=3650)
    context_rate_per_minute: int = Field(default=60, ge=1)  # per principal, in-process token bucket (the multi-replica limiter is P9)
    context_rate_burst: int = Field(default=20, ge=1)
    context_workspace_concurrency: int = Field(
        default=6, ge=1
    )  # builds in flight per workspace (a build holds up to three connections; the API pool is 20)
    context_debug_ttl_hours: float = Field(default=24.0, gt=0.0, le=24 * 7)
    context_ablate: str = (
        ""  # evaluation only (`make eval-context --ablate <feature>`): zero one ranking weight. Refused when ENV=production.
    )

    # Agents and quotas (P7). The platform default for a principal whose registration sets no limit; daily token and interaction quotas default to unlimited.
    principal_api_enabled: bool = True  # False: the registration API (POST/PATCH/DELETE /v1/principals...) answers 503 feature_disabled; manage principals with the CLI
    principal_requests_per_minute: int = Field(default=1200, ge=1)  # in-process token bucket per principal (the shared limiter is P9)
    principal_usage_flush_seconds: float = Field(default=10.0, gt=0.0)

    # Governance (P8). Deletion intake can be switched off (frozen requests stay frozen, which keeps reads safe); the journal lives OUTSIDE the database and its backups.
    deletion_api_enabled: bool = True  # False: POST /v1/deletion-requests answers 503 feature_disabled
    workspace_egress_ttl_seconds: float = Field(
        default=30.0, ge=0.0, le=600.0
    )  # how long a process trusts a workspace's egress narrowing before it asks again (a change made through this process is seen at once)
    deletion_journal_dir: str = (
        ""  # empty: <repo>/.local/deletion-journal in dev; production requires it to be set (outside the database volume)
    )
    workspace_deletion_cooling_hours: float = Field(default=24.0, ge=0.0, le=24 * 30)
    backup_retention_days: int = Field(default=30, ge=1, le=3650)  # receipts state when backup copies expire (P9 makes backups honour it)
    consolidation_debounce_minutes: float = Field(default=10.0, ge=0.0)
    retention_sweep_seconds: float = Field(default=300.0, gt=0.0)
    simulation_cleanup_minutes: float = Field(
        default=60.0, gt=0.0
    )  # a delayed job removes a staging namespace this long after a run started

    # Release hardening (P9). The shared limiter and cache invalidation use Postgres (no broker); OIDC is for human users, API keys stay for agents, apps and break-glass.
    limiter_backend: Literal["memory", "postgres"] = "memory"  # postgres: one budget across API replicas (unlogged counters, DB clock)
    limiter_fail_mode: Literal["closed", "open"] = (
        "closed"  # what an EXPENSIVE call (a context build) does when the shared limiter's database is unreachable
    )
    invalidation_listener: bool = (
        True  # LISTEN kg_invalidate: settings, grants, revocations, the active ontology and namespaces take effect across processes at once
    )
    maintenance_mode: Literal["off", "read_only"] = (
        "off"  # read_only: every write answers 503 (a restored environment before it is verified)
    )
    auth_oidc_enabled: bool = False
    oidc_issuer: str = ""  # the IdP's issuer URL (its discovery document and JWKS are derived from it)
    oidc_audience: str = ""  # the `aud` the access/ID token must carry (normally the console's client id)
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")  # empty: a public client (PKCE only), which is what the console uses
    oidc_algorithms: str = "RS256,ES256"  # pinned; "none" and HMAC algorithms are never accepted
    oidc_workspace_claim: str = "kg_workspace"  # optional claim naming the workspace slug when the groups map into several
    oidc_groups_claim: str = "groups"
    oidc_jwks_url: str = ""  # empty: taken from the issuer's discovery document
    oidc_jwks_ttl_seconds: float = Field(default=600.0, gt=0.0)
    oidc_redirect_uris: list[str] = Field(default_factory=list)  # the exact redirect URIs the console may use
    allow_breakglass_keys: bool = (
        False  # production: API keys of human (user) principals work only while this is true, and each use is audited
    )
    reindex_retention_days: int = Field(default=14, ge=0, le=365)  # a retired graph namespace is kept this long for rollback, then cleared
    reconcile_scan_hours: float = Field(default=24.0, ge=0.0)  # nightly check-only scan per workspace; 0 disables
    alertmanager_url: str = ""  # optional: the console's alerts panel also lists what this Alertmanager is firing (GET /api/v2/alerts)
    canary_workspace: str = ""  # slug of the workspace whose context the canary builds every 5 minutes; empty disables
    security_headers: bool = True

    # Secret rotation (P9 WP6). Each secret is a RING of versions; empty rings fall back to the single secret above, as version v1 (JWT: local-1). See docs/runbooks/rotate-secrets.md.
    auth_jwt_keys: SecretStr = SecretStr(
        ""
    )  # "kid:secret,kid:secret": tokens are signed with the active kid and verified with any kid in the ring
    auth_jwt_active_kid: str = ""
    auth_key_pepper_id: str = "p1"  # the label stored with every API key hash; a key hashed under the PREVIOUS pepper is accepted once and re-hashed under this one
    auth_key_pepper_previous: SecretStr = SecretStr("")
    auth_key_pepper_previous_id: str = "p0"
    content_hmac_secrets: SecretStr = SecretStr(
        ""
    )  # "v1:secret,v2:secret": new hashes use the active version; lookups of stored hashes try every version in the ring
    content_hmac_active: str = ""

    # Test-only failure injection (P3 §5.7). Refused when ENV=production.
    kg_faults: str = ""  # "stage:point:count,..." e.g. "normalize:after_claim:3"
    kg_faults_dir: str = ""  # holds the persistent fire counters and an optional `spec` file the dev worker re-reads

    # Graph projection (P2). The command fast path waits this long for its projection; the outbox finishes anything slower.
    projection_request_budget_seconds: float = 5.0  # how long a command request waits for its projection before answering 202

    @property
    def router_base_url(self) -> str:
        return (self.oso_router_live_url if self.oso_router_mode == "live" else self.oso_router_mock_url).rstrip("/")

    @property
    def router_origin(self) -> str:
        base = self.router_base_url
        return base[: -len("/v1")] if base.endswith("/v1") else base

    def router_api_key(self, service: str) -> SecretStr:
        return self.oso_router_api_key_worker if service == "kg-worker" else self.oso_router_api_key_api

    def content_hmac_ring(self) -> SecretRing:
        return SecretRing.parse(
            self.content_hmac_secrets.get_secret_value(),
            self.content_hmac_active,
            legacy=self.content_hmac_secret.get_secret_value(),
            legacy_version="v1",
            name="CONTENT_HMAC_SECRETS",
            min_bytes=1,
        )

    def jwt_ring(self) -> SecretRing:
        return SecretRing.parse(
            self.auth_jwt_keys.get_secret_value(),
            self.auth_jwt_active_kid,
            legacy=self.auth_jwt_secret.get_secret_value(),
            legacy_version="local-1",
            name="AUTH_JWT_KEYS",
            min_bytes=1,
        )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @model_validator(mode="after")
    def _production_secrets(self) -> Settings:
        if self.env != "production":
            return self
        problems: list[str] = []
        for name, value, dev in (
            ("AUTH_KEY_PEPPER", self.auth_key_pepper, DEV_PEPPER),
            ("AUTH_JWT_SECRET", self.auth_jwt_secret, DEV_JWT_SECRET),
            ("TELEMETRY_HMAC_SECRET", self.telemetry_hmac_secret, DEV_TELEMETRY_SECRET),
        ):
            raw = value.get_secret_value()
            if raw == dev:
                problems.append(f"{name} is still the development default")
            elif len(raw.encode()) < MIN_SECRET_BYTES:
                problems.append(f"{name} must be at least {MIN_SECRET_BYTES} bytes")
        raw_hmac = self.content_hmac_secret.get_secret_value()
        if raw_hmac == DEV_CONTENT_HMAC_SECRET:
            problems.append("CONTENT_HMAC_SECRET is still the development default")
        elif len(raw_hmac.encode()) < MIN_SECRET_BYTES:
            problems.append(f"CONTENT_HMAC_SECRET must be at least {MIN_SECRET_BYTES} bytes")
        ring = self.payload_keys.get_secret_value()
        if DEV_PAYLOAD_KEY_B64 in ring:
            problems.append("PAYLOAD_KEYS still contains the development key")
        if not any(part.strip().startswith(f"{self.payload_active_kid}:") for part in ring.split(",")):
            problems.append("PAYLOAD_ACTIVE_KID is not in PAYLOAD_KEYS")
        for name, ring_fn, dev in (
            ("CONTENT_HMAC_SECRETS", self.content_hmac_ring, DEV_CONTENT_HMAC_SECRET),
            ("AUTH_JWT_KEYS", self.jwt_ring, DEV_JWT_SECRET),
        ):
            try:
                versions = ring_fn().secrets
            except RingError as exc:
                problems.append(str(exc))
                continue
            for version, secret in versions.items():
                if secret.decode() == dev:
                    problems.append(f"{name} version '{version}' is the development default")
                elif len(secret) < MIN_SECRET_BYTES:
                    problems.append(f"{name} version '{version}' must be at least {MIN_SECRET_BYTES} bytes")
        prev = self.auth_key_pepper_previous.get_secret_value()
        if prev and (len(prev.encode()) < MIN_SECRET_BYTES or prev == DEV_PEPPER):
            problems.append("AUTH_KEY_PEPPER_PREVIOUS must be a real secret of at least 32 bytes (or empty)")
        if prev and self.auth_key_pepper_previous_id == self.auth_key_pepper_id:
            problems.append("AUTH_KEY_PEPPER_PREVIOUS_ID must differ from AUTH_KEY_PEPPER_ID")
        if self.kg_faults or self.kg_faults_dir:
            problems.append("KG_FAULTS / KG_FAULTS_DIR (failure injection) must not be set in production")
        if self.context_ablate:
            problems.append("CONTEXT_ABLATE (a ranking ablation for evaluation) must not be set in production")
        if not self.deletion_journal_dir:
            problems.append(
                "DELETION_JOURNAL_DIR must be set in production (the deletion journal lives outside the database and its backups)"
            )
        for name, key in (
            ("OSO_ROUTER_API_KEY_API", self.oso_router_api_key_api),
            ("OSO_ROUTER_API_KEY_WORKER", self.oso_router_api_key_worker),
        ):
            if key.get_secret_value().startswith("REPLACE-WITH"):
                problems.append(f"{name} is still the placeholder written by scripts/prod_secrets.py")
        for origin in self.cors_allow_origins:
            if origin.strip() == "*" or "*" in origin:
                problems.append("CORS_ALLOW_ORIGINS must list exact origins in production (no wildcard)")
            elif not origin.startswith("https://"):
                problems.append(f"CORS_ALLOW_ORIGINS entry '{origin}' must be an https:// origin in production")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def stage_limits(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for part in self.worker_stage_limits.split(","):
            if "=" in part:
                stage, _, n = part.partition("=")
                if stage.strip() and n.strip().isdigit():
                    out[stage.strip()] = max(1, int(n))
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
