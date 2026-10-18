"""Configuration from the environment (a `.env` file is read when present). Nothing secret is stored here: recipients are PUBLIC age keys, identities are paths."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE = "kg-backup-tools:p9"


def _env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


@dataclass
class Config:
    backup_dir: Path
    escrow_dir: Path
    recipients: list[str]  # age public keys (age1...) the data backup is encrypted to
    escrow_recipients: list[str]  # age public keys for the key escrow (never the same set as the data backup's)
    retention_days: int = 30
    keep_at_least: int = 3
    compose_file: Path = REPO / "deploy" / "compose" / "docker-compose.yml"
    compose_env: Path = REPO / ".env.example"
    compose_project: str = "kg"
    postgres_service: str = "postgres"
    neo4j_service: str = "neo4j"
    neo4j_image: str = "neo4j:5.26-community@sha256:c7d25c0eeebfe125718d58b72ed5d663c85d0479047733845eb2237c67ce8069"
    neo4j_volume: str = "kg_neo4jdata"
    pg_superuser: str = "postgres"
    pg_password: str = "kg_admin_dev"  # noqa: S105 (the development default; production passes BACKUP_PG_PASSWORD from a secret file)
    databases: list[str] = field(default_factory=lambda: ["kg"])
    owner_url: str = "postgresql://kg_owner:kg_owner_dev@127.0.0.1:5544/kg"  # where backup_runs is recorded (kg_owner only)

    @classmethod
    def from_env(cls) -> Config:
        env = {**_env_file(), **os.environ}
        recipients = [r for r in env.get("BACKUP_RECIPIENTS", "").split(",") if r.strip()]
        escrow = [r for r in env.get("BACKUP_ESCROW_RECIPIENTS", "").split(",") if r.strip()]
        owner = env.get("DATABASE_URL_OWNER", cls.owner_url).replace("postgresql+psycopg://", "postgresql://")
        pw_file = env.get("BACKUP_PG_PASSWORD_FILE")
        return cls(
            backup_dir=Path(env.get("BACKUP_DIR", str(REPO / ".local" / "backups"))),
            escrow_dir=Path(env.get("BACKUP_ESCROW_DIR", str(REPO / ".local" / "key-escrow"))),
            recipients=[r.strip() for r in recipients],
            escrow_recipients=[r.strip() for r in escrow],
            retention_days=int(env.get("BACKUP_RETENTION_DAYS", "30")),
            keep_at_least=int(env.get("BACKUP_KEEP_AT_LEAST", "3")),
            compose_project=env.get("BACKUP_COMPOSE_PROJECT", "kg"),
            compose_file=Path(env["BACKUP_COMPOSE_FILE"])
            if env.get("BACKUP_COMPOSE_FILE")
            else cls.compose_file,  # production: deploy/compose/docker-compose.prod.yml
            compose_env=Path(env["BACKUP_COMPOSE_ENV"]) if env.get("BACKUP_COMPOSE_ENV") else cls.compose_env,  # production: .env.prod
            neo4j_volume=env.get("BACKUP_NEO4J_VOLUME", "kg_neo4jdata"),
            databases=[d for d in env.get("BACKUP_DATABASES", "kg").split(",") if d],
            pg_password=Path(pw_file).read_text().strip() if pw_file else env.get("POSTGRES_ADMIN_PASSWORD", "kg_admin_dev"),
            owner_url=owner,
        )

    def validate_for_backup(self) -> None:
        if not self.recipients:
            raise SystemExit(
                "BACKUP_RECIPIENTS is empty: backups are always encrypted (generate a key pair with `python -m tools.backup keygen`)"
            )
        if set(self.recipients) & set(self.escrow_recipients):
            raise SystemExit("the key escrow must be encrypted to different recipients than the data backup")
        if self.backup_dir.resolve() == self.escrow_dir.resolve():
            raise SystemExit("BACKUP_DIR and BACKUP_ESCROW_DIR must be different locations")
