"""Restoring a backup set into a SEPARATE compose project (`kg-restore`): never over the environment the backup came from."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.backup.config import REPO, Config
from tools.backup.tooling import ToolError, decrypt_to, log, run

PORTS = {
    "KG_PG_PORT": "5644",
    "KG_NEO4J_HTTP_PORT": "7575",
    "KG_NEO4J_BOLT_PORT": "7788",
    "KG_MOCK_PORT": "8882",
    "KG_API_PORT": "8100",
    "KG_API_METRICS_PORT": "9200",
    "KG_WORKER_METRICS_PORT": "9201",
}


@dataclass
class Target:
    """One restore (or perf) environment: a compose project, its own ports, volumes and scratch directory."""

    project: str
    work: Path
    ports: dict[str, str]
    maintenance: str = "read_only"
    pg_password: str = "kg_admin_dev"  # noqa: S105 (the development default; the environment file below carries whatever the source used)

    @property
    def env_file(self) -> Path:
        return self.work / "restore.env"

    @property
    def journal_dir(self) -> Path:
        return self.work / "journal"

    def write_env(self, source_env: Path) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        base = source_env.read_text() if source_env.exists() else ""
        lines = [
            ln
            for ln in base.splitlines()
            if not any(ln.startswith(k + "=") for k in (*self.ports, "MAINTENANCE_MODE", "KG_JOURNAL_HOST_DIR", "KG_FAULTS_HOST_DIR"))
        ]
        lines += [f"{k}={v}" for k, v in self.ports.items()]
        lines += [
            f"MAINTENANCE_MODE={self.maintenance}",
            f"KG_JOURNAL_HOST_DIR={self.journal_dir.resolve()}",
            f"KG_FAULTS_HOST_DIR={(self.work / 'faults').resolve()}",
        ]
        self.env_file.write_text("\n".join(lines) + "\n")
        (self.work / "faults").mkdir(exist_ok=True)
        (self.work / "faults").chmod(0o777)
        self.journal_dir.mkdir(exist_ok=True)
        self.journal_dir.chmod(0o777)

    def compose(self, cfg: Config, *args: str, check: bool = True, capture: bool = True, input: bytes | None = None) -> Any:
        return run(
            ["docker", "compose", "-p", self.project, "-f", str(cfg.compose_file), "--env-file", str(self.env_file), *args],
            check=check,
            capture=capture,
            input=input,
        )

    def exec_psql(self, cfg: Config, db: str, sql: str) -> list[list[str]]:
        proc = self.compose(
            cfg,
            "exec",
            "-T",
            "-e",
            f"PGPASSWORD={self.pg_password}",
            "postgres",
            "psql",
            "-U",
            cfg.pg_superuser,
            "-d",
            db,
            "-At",
            "-F",
            "|",
            "-c",
            sql,
        )
        return [line.split("|") for line in proc.stdout.decode().splitlines() if line]


def fresh_target(cfg: Config, name: str = "kg-restore", ports: dict[str, str] | None = None) -> Target:
    work = REPO / ".local" / name
    if work.exists():
        shutil.rmtree(work)
    t = Target(project=name, work=work, ports=ports or dict(PORTS), pg_password=cfg.pg_password)
    t.write_env(cfg.compose_env)
    tear_down(cfg, t)  # a previous rehearsal left with --keep must not leak its volumes into this one
    return t


def tear_down(cfg: Config, t: Target) -> None:
    t.compose(cfg, "--profile", "replicas", "down", "-v", "--remove-orphans", check=False)


def restore_postgres(cfg: Config, t: Target, dump: Path, db: str) -> None:
    """Drops and recreates the database from the same recipe `postgres-init` uses, copies the (decrypted) dump into the container, pg_restore as the superuser."""
    t.compose(cfg, "up", "-d", "--wait", "postgres")
    t.exec_psql(cfg, "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    t.exec_psql(cfg, "postgres", f"CREATE DATABASE {db} OWNER kg_owner")
    t.compose(cfg, "cp", str(dump), "postgres:/tmp/restore.dump")
    try:
        t.compose(
            cfg,
            "exec",
            "-T",
            "-e",
            f"PGPASSWORD={t.pg_password}",
            "postgres",
            "pg_restore",
            "-U",
            cfg.pg_superuser,
            "-d",
            db,
            "--exit-on-error",
            "/tmp/restore.dump",
        )  # noqa: S108
    finally:
        t.compose(cfg, "exec", "-T", "postgres", "rm", "-f", "/tmp/restore.dump", check=False)  # noqa: S108
    t.exec_psql(cfg, db, f"REVOKE ALL ON DATABASE {db} FROM PUBLIC; GRANT CONNECT ON DATABASE {db} TO kg_app, kg_owner")


def restore_neo4j(cfg: Config, t: Target, dump: Path) -> None:
    """Loads the offline dump into the restore project's data volume BEFORE Neo4j starts."""
    volume = f"{t.project}_neo4jdata"
    run(["docker", "volume", "create", volume])
    staging = t.work / "neo4j-in"
    staging.mkdir(exist_ok=True)
    staging.chmod(0o777)
    shutil.copy(dump, staging / "neo4j.dump")
    (staging / "neo4j.dump").chmod(0o644)
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            "-v",
            f"{staging.resolve()}:/in:ro",
            cfg.neo4j_image,
            "neo4j-admin",
            "database",
            "load",
            "neo4j",
            "--from-path=/in",
            "--overwrite-destination=true",
        ]
    )
    shutil.rmtree(staging)


def decrypt_set(cfg: Config, setdir: Path, identity: Path, workdir: Path) -> dict[str, Path]:
    """Every data artifact of the set decrypted into `workdir` (inside the rehearsal's own scratch directory, removed afterwards)."""
    manifest = json.loads((setdir / "manifest.json").read_text())
    out: dict[str, Path] = {}
    for art in manifest["artifacts"]:
        if art.get("location") == "escrow":
            continue
        dest = workdir / art["name"].removesuffix(".age")
        decrypt_to(setdir / art["name"], dest, identity)
        out[art["name"].removesuffix(".age")] = dest
    return out


def bring_up(cfg: Config, t: Target) -> None:
    """Migrations (a no-op when the backup is current), then the API and the worker in read-only mode."""
    t.compose(cfg, "up", "-d", "--wait", "neo4j", "mock-router")
    t.compose(cfg, "run", "--rm", "migrate")
    t.compose(cfg, "up", "-d", "--wait", "api", "worker")
    log(f"{t.project}: api on :{t.ports['KG_API_PORT']} in {t.maintenance} mode")


def cli(cfg: Config, t: Target, *args: str, check: bool = True) -> tuple[int, str]:
    """`kg ...` in the restore project (the image the API runs, pointed at the restored services)."""
    proc = t.compose(cfg, "run", "--rm", "--no-deps", "-T", "-e", "OSO_ROUTER_MODE=mock", "api", "kg", *args, check=False)
    text = proc.stdout.decode() + proc.stderr.decode()
    if check and proc.returncode != 0:
        raise ToolError(f"kg {' '.join(args)} exited {proc.returncode}: {text[-500:]}")
    return int(proc.returncode), text


_ = os
