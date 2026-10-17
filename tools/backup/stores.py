"""One backup SET: Postgres (pg_dump -Fc per database), Neo4j (an OFFLINE `neo4j-admin database dump` in a short maintenance window), the configuration, and (separately) the keys.

Every artifact is piped through `age` on its way to disk. The manifest beside them holds sizes, hashes and row counts only."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from tools.backup import record
from tools.backup.config import REPO, Config
from tools.backup.tooling import ToolError, compose, encrypt_bytes, encrypt_stream, log, run, sha256_file

MANIFEST = "manifest.json"
SECRET_NAMES = (
    "PAYLOAD_KEYS",
    "PAYLOAD_ACTIVE_KID",
    "CONTENT_HMAC_SECRET",
    "CONTENT_HMAC_SECRETS",
    "AUTH_KEY_PEPPER",
    "AUTH_KEY_PEPPER_PREVIOUS",
    "AUTH_JWT_SECRET",
    "AUTH_JWT_KEYS",
    "TELEMETRY_HMAC_SECRET",
    "OSO_ROUTER_API_KEY_API",
    "OSO_ROUTER_API_KEY_WORKER",
    "NEO4J_PASSWORD",
    "KG_APP_PASSWORD",
    "KG_OWNER_PASSWORD",
    "POSTGRES_ADMIN_PASSWORD",
)
COUNT_SQL = (
    "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text "
    "FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
)


def stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _psql(cfg: Config, db: str, sql: str) -> list[list[str]]:
    proc = compose(
        cfg,
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={cfg.pg_password}",
        cfg.postgres_service,
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


def table_counts(cfg: Config, db: str) -> dict[str, int]:
    return {name: int(n) for name, n in _psql(cfg, db, COUNT_SQL)}


STRUCTURE_SQL = {
    "triggers": "SELECT c.relname || '.' || t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE NOT t.tgisinternal AND n.nspname = 'public' ORDER BY 1",
    "indexes": "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1",
    "functions": "SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname IN ('public','auth','ops') ORDER BY 1",
    "policies": "SELECT tablename || '.' || policyname FROM pg_policies WHERE schemaname = 'public' ORDER BY 1",
    "rls_tables": "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity ORDER BY 1",
}


def structure(cfg: Config, db: str) -> dict[str, list[str]]:
    """The names of what the schema relies on (triggers, indexes, functions, policies, tables with forced RLS): a restore must bring back exactly these."""
    return {key: sorted({row[0] for row in _psql(cfg, db, sql)}) for key, sql in STRUCTURE_SQL.items()}


def git_state() -> dict[str, Any]:
    try:
        commit = run(["git", "rev-parse", "HEAD"]).stdout.decode().strip()
        dirty = bool(run(["git", "status", "--porcelain"]).stdout.strip())
        tag = run(["git", "describe", "--tags", "--always"], check=False).stdout.decode().strip()
    except (ToolError, FileNotFoundError):
        return {"commit": None, "dirty": None, "describe": None}
    return {"commit": commit, "dirty": dirty, "describe": tag}


# ---- Postgres -----------------------------------------------------------------------------------------------------------------------------------
def backup_postgres(cfg: Config, setdir: Path) -> list[dict[str, Any]]:
    out = []
    for db in cfg.databases:
        started = time.monotonic()
        counts = table_counts(cfg, db)
        revision = (_psql(cfg, db, "SELECT version_num FROM alembic_version") or [[None]])[0][0]
        path = setdir / f"postgres-{db}.dump.age"
        size, digest = encrypt_stream(
            [
                "docker",
                "compose",
                "-p",
                cfg.compose_project,
                "-f",
                str(cfg.compose_file),
                "--env-file",
                str(cfg.compose_env),
                "exec",
                "-T",
                "-e",
                f"PGPASSWORD={cfg.pg_password}",
                cfg.postgres_service,
                "pg_dump",
                "-U",
                cfg.pg_superuser,
                "-Fc",
                db,
            ],
            path,
            cfg.recipients,
        )
        log(f"postgres {db}: {size} bytes encrypted in {time.monotonic() - started:.1f}s")
        out.append(
            {
                "store": "postgres",
                "name": path.name,
                "database": db,
                "size_bytes": size,
                "sha256": digest,
                "schema_revision": revision,
                "row_counts": counts,
                "structure": structure(cfg, db),
                "seconds": round(time.monotonic() - started, 2),
            }
        )
    return out


# ---- Neo4j (offline dump) -------------------------------------------------------------------------------------------------------------------------
def _neo4j_counts(cfg: Config) -> dict[str, int]:
    user, password = os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "kg-neo4j-dev")
    proc = compose(
        cfg,
        "exec",
        "-T",
        cfg.neo4j_service,
        "cypher-shell",
        "-u",
        user,
        "-p",
        password,
        "--format",
        "plain",
        "MATCH (n) WITH count(n) AS nodes OPTIONAL MATCH ()-[r]->() RETURN nodes, count(r) AS relationships",
        check=False,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}
    nodes, rels = (int(x) for x in lines[1].split(","))
    return {"nodes": nodes, "relationships": rels}


def _neo4j_names(cfg: Config, what: str) -> list[str]:
    user, password = os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "kg-neo4j-dev")
    proc = compose(
        cfg,
        "exec",
        "-T",
        cfg.neo4j_service,
        "cypher-shell",
        "-u",
        user,
        "-p",
        password,
        "--format",
        "plain",
        f"SHOW {what} YIELD name RETURN name ORDER BY name",
        check=False,
    )
    return [ln.strip().strip('"') for ln in proc.stdout.decode().splitlines()[1:] if ln.strip()]


def backup_neo4j(cfg: Config, setdir: Path) -> list[dict[str, Any]]:
    """Stops Neo4j, dumps the database offline from its data volume, ALWAYS starts it again. While it is down model stages back off (their graph step is retryable), the API
    stays up and graph search answers `partial`; Postgres current facts are unaffected. The window is a minute or two for a development-sized graph."""
    started = time.monotonic()
    counts = _neo4j_counts(cfg)
    names = {"constraints": _neo4j_names(cfg, "CONSTRAINTS"), "indexes": _neo4j_names(cfg, "INDEXES")}
    work = setdir / ".neo4j-tmp"
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o777)
    log("neo4j: stopping for the offline dump")
    compose(cfg, "stop", cfg.neo4j_service)
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{cfg.neo4j_volume}:/data",
                "-v",
                f"{work.resolve()}:/backups",
                cfg.neo4j_image,
                "neo4j-admin",
                "database",
                "dump",
                "neo4j",
                "--to-path=/backups",
                "--overwrite-destination=true",
            ]
        )
    finally:
        compose(cfg, "start", cfg.neo4j_service, check=False)
        window = round(time.monotonic() - started, 2)
        log(f"neo4j: started again after {window}s")
    dump = work / "neo4j.dump"
    path = setdir / "neo4j.dump.age"
    size, digest = encrypt_stream(["cat", str(dump)], path, cfg.recipients)
    dump.unlink()
    work.rmdir()
    return [
        {
            "store": "neo4j",
            "name": path.name,
            "size_bytes": size,
            "sha256": digest,
            "counts": counts,
            "structure": names,
            "downtime_seconds": window,
        }
    ]


# ---- configuration --------------------------------------------------------------------------------------------------------------------------------
def backup_config(cfg: Config, setdir: Path) -> list[dict[str, Any]]:
    """The files that define behaviour (compat/, the compose files, the env example), a tarball. Ontologies and workspace settings live in Postgres, so the dump has them."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in ("compat", "deploy/compose", ".env.example", "pyproject.toml", "uv.lock"):
            p = REPO / rel
            if p.exists():
                tar.add(p, arcname=rel, filter=lambda ti: None if "/.local" in ti.name or ti.name.endswith(".pyc") else ti)
    path = setdir / "config.tar.gz.age"
    size, digest = encrypt_bytes(buf.getvalue(), path, cfg.recipients)
    return [{"store": "config", "name": path.name, "size_bytes": size, "sha256": digest}]


# ---- keys (separately, to the escrow) ---------------------------------------------------------------------------------------------------------------
def _env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in (".env.example", ".env"):
        path = REPO / name
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    values[k.strip()] = v.strip()
    values.update({k: v for k, v in os.environ.items() if k in SECRET_NAMES})
    return {k: values[k] for k in SECRET_NAMES if values.get(k)}


def backup_keys(cfg: Config, tag: str, journal: Path | None = None) -> list[dict[str, Any]]:
    """The payload keyring, the content HMAC secret(s), the auth pepper and JWT secrets, and a copy of the deletion journal, encrypted to the ESCROW recipients and written to the
    escrow location. Losing these makes every encrypted payload unreadable; losing the data backup makes them useless to anyone else (the two never travel together)."""
    if not cfg.escrow_recipients:
        raise SystemExit("BACKUP_ESCROW_RECIPIENTS is empty: keys are only ever exported encrypted, and to their own recipients")
    payload: dict[str, Any] = {"created_at": dt.datetime.now(dt.UTC).isoformat(), "secrets": _env_values()}
    if journal is not None and journal.exists():
        payload["deletion_journal"] = journal.read_text()
    path = cfg.escrow_dir / f"keys-{tag}.json.age"
    size, digest = encrypt_bytes(json.dumps(payload, sort_keys=True).encode(), path, cfg.escrow_recipients)
    return [
        {
            "store": "keys",
            "name": path.name,
            "size_bytes": size,
            "sha256": digest,
            "location": "escrow",
            "names": sorted(payload["secrets"]),
        }
    ]


# ---- the set ----------------------------------------------------------------------------------------------------------------------------------------
def run_backup(cfg: Config, stores: list[str]) -> dict[str, Any]:
    cfg.validate_for_backup()
    tag = stamp()
    setdir = cfg.backup_dir / tag
    setdir.mkdir(parents=True, exist_ok=False)
    started = dt.datetime.now(dt.UTC)
    manifest: dict[str, Any] = {
        "id": tag,
        "created_at": started.isoformat(),
        "git": git_state(),
        "recipients": len(cfg.recipients),
        "artifacts": [],
    }
    steps = {"postgres": backup_postgres, "neo4j": backup_neo4j, "config": backup_config}
    for store in stores:
        run_id = record.start(cfg, "backup", store)
        try:
            if store == "keys":
                arts = backup_keys(
                    cfg, tag, Path(os.environ.get("DELETION_JOURNAL_DIR", str(REPO / ".local" / "deletion-journal"))) / "deletions.jsonl"
                )
            else:
                arts = steps[store](cfg, setdir)
            manifest["artifacts"].extend(arts)
            record.finish(
                cfg,
                run_id,
                "completed",
                size=sum(a["size_bytes"] for a in arts),
                sha=arts[0]["sha256"] if len(arts) == 1 else None,
                location=f"{tag}/{arts[0]['name']}" if store != "keys" else f"escrow/{arts[0]['name']}",
                retention_days=cfg.retention_days,
                details={"artifacts": len(arts)},
            )
        except Exception as exc:
            record.finish(cfg, run_id, "failed", error_code=type(exc).__name__)
            raise
    (setdir / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    log(f"backup set {tag} written to {setdir}")
    return manifest


def sets(cfg: Config) -> list[Path]:
    if not cfg.backup_dir.exists():
        return []
    return sorted(p for p in cfg.backup_dir.iterdir() if p.is_dir() and (p / MANIFEST).exists())


def load(setdir: Path) -> dict[str, Any]:
    return json.loads((setdir / MANIFEST).read_text())  # type: ignore[no-any-return]


def verify_set(setdir: Path) -> list[str]:
    """Hashes against the manifest. Returns the problems (empty: the set is intact). Decryption needs the identity and is done by the restore."""
    manifest, problems = load(setdir), []
    for art in manifest["artifacts"]:
        if art.get("location") == "escrow":
            continue
        path = setdir / art["name"]
        if not path.exists():
            problems.append(f"{art['name']}: missing")
        elif sha256_file(path) != art["sha256"]:
            problems.append(f"{art['name']}: sha256 differs from the manifest")
    return problems


def prune(cfg: Config, *, now: dt.datetime | None = None) -> list[str]:
    """Removes sets older than the retention (the P8 setting a deletion receipt promises), always keeping the newest `keep_at_least`. Recorded as a `prune` run."""
    now = now or dt.datetime.now(dt.UTC)
    all_sets = sets(cfg)
    removed = []
    for setdir in all_sets[: max(0, len(all_sets) - cfg.keep_at_least)]:
        created = dt.datetime.fromisoformat(load(setdir)["created_at"])
        if now - created > dt.timedelta(days=cfg.retention_days):
            for f in setdir.iterdir():
                f.unlink()
            setdir.rmdir()
            removed.append(setdir.name)
    if removed:
        run_id = record.start(cfg, "prune", "all")
        record.finish(cfg, run_id, "completed", details={"removed": len(removed)})
    return removed


_ = subprocess
