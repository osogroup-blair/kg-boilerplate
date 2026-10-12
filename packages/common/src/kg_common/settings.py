"""Process configuration. Secrets are SecretStr; production refuses development defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]

DEV_PEPPER = "dev-only-pepper-change-me-32-bytes-min"  # noqa: S105 (documented development default; production refuses it)
DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-32-bytes"  # noqa: S105
DEV_TELEMETRY_SECRET = "dev-only-telemetry-secret"  # noqa: S105
DEV_CONTENT_HMAC_SECRET = "dev-only-content-hmac-secret-32-bytes"  # noqa: S105
# Development payload keyring: kid "dev1" is base64 of 32 bytes. Production refuses it (and any ring whose active kid is missing).
DEV_PAYLOAD_KEY_B64 = "ZGV2LW9ubHktcGF5bG9hZC1rZXktMzItYnl0ZXMtISE="  # noqa: S105 (documented development default)
DEV_PAYLOAD_KEYS = f"dev1:{DEV_PAYLOAD_KEY_B64}"
MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

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
    max_body_bytes: int = 1024 * 1024
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
        if self.kg_faults or self.kg_faults_dir:
            problems.append("KG_FAULTS / KG_FAULTS_DIR (failure injection) must not be set in production")
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
