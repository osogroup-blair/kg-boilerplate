from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def env_or_file(name: str, default: str = "") -> str:
    """NAME, or the contents of the file NAME_FILE (a Docker secret)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    api_url: str = "http://127.0.0.1:8000"  # how the broker reaches the API (inside the compose network)
    public_api_url: str = "http://127.0.0.1:8000"  # what visitors are told to use
    console_url: str = "http://127.0.0.1:5273"
    mock_url: str = "http://127.0.0.1:8782"
    db_path: Path = REPO / ".local" / "sandbox.sqlite"
    ttl_hours: float = 2.0
    pool_size: int = 3
    max_active: int = 40
    ip_per_hour: int = 3
    turnstile_secret: str = ""
    ip_salt: str = "dev-only-salt"
    cli: tuple[str, ...] = ("kg",)
    ontology: Path = REPO / "examples" / "ontologies" / "personal-knowledge-governance.yaml"
    allow_origins: tuple[str, ...] = ("http://localhost:4321",)
    manage_mock: bool = True  # load the strict fixture pack into the mock router at startup (and leave it loaded)

    @classmethod
    def from_env(cls) -> Config:
        e = os.environ.get
        d = cls()
        return cls(
            api_url=e("SANDBOX_API_URL", d.api_url).rstrip("/"),
            public_api_url=e("SANDBOX_PUBLIC_API_URL", d.public_api_url).rstrip("/"),
            console_url=e("SANDBOX_CONSOLE_URL", d.console_url).rstrip("/"),
            mock_url=e("SANDBOX_MOCK_URL", d.mock_url).rstrip("/"),
            db_path=Path(e("SANDBOX_DB", str(d.db_path))),
            ttl_hours=float(e("SANDBOX_TTL_HOURS", d.ttl_hours)),
            pool_size=int(e("SANDBOX_POOL_SIZE", d.pool_size)),
            max_active=int(e("SANDBOX_MAX_ACTIVE", d.max_active)),
            ip_per_hour=int(e("SANDBOX_IP_PER_HOUR", d.ip_per_hour)),
            turnstile_secret=env_or_file("SANDBOX_TURNSTILE_SECRET"),
            ip_salt=env_or_file("SANDBOX_IP_SALT", d.ip_salt),
            cli=tuple(shlex.split(e("SANDBOX_CLI", "kg"))),
            allow_origins=tuple(x for x in e("SANDBOX_ALLOW_ORIGINS", ",".join(d.allow_origins)).split(",") if x),
            manage_mock=e("SANDBOX_MANAGE_MOCK", "1") != "0",
        )
