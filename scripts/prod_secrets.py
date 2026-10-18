"""Creates the production secret files for deploy/compose/docker-compose.prod.yml (phase-9 WP10).

    python scripts/prod_secrets.py [--out deploy/compose/secrets] [--router-key-file PATH]

One file per secret (mode 0600, directory 0700), each the value only, no trailing newline. Existing files are NEVER overwritten (rotate with `kg secrets` and the runbook, not by
re-running this). The router keys are credentials this repository cannot invent: pass `--router-key-file` (the same key for the API and the worker) or put the files in place
yourself; until then they hold a marker that the production settings refuse at boot. Nothing is printed except file names."""

from __future__ import annotations

import argparse
import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def token(n: int = 48) -> str:
    return secrets.token_urlsafe(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "deploy" / "compose" / "secrets")
    ap.add_argument("--router-key-file", type=Path)
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)

    owner_pw, app_pw, pg_pw = token(32), token(32), token(32)
    router = args.router_key_file.read_text().strip() if args.router_key_file else "REPLACE-WITH-THE-ROUTER-KEY"
    values = {
        "postgres_password": pg_pw,
        "kg_owner_password": owner_pw,
        "kg_app_password": app_pw,
        "database_url_app": f"postgresql+psycopg://kg_app:{app_pw}@postgres:5432/kg",
        "database_url_owner": f"postgresql+psycopg://kg_owner:{owner_pw}@postgres:5432/kg",
        "neo4j_password": token(32),
        "neo4j_auth": None,  # derived below
        "auth_key_pepper": token(48),
        "auth_jwt_secret": token(48),
        "telemetry_hmac_secret": token(48),
        "content_hmac_secret": token(48),
        "payload_keys": "prod1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "oso_router_api_key_api": router,
        "oso_router_api_key_worker": router,
        "oidc_client_secret": "",
        "canary_key": "",  # the canary agent's API key, once the canary workspace exists (docs/runbooks/deploy-production.md)
    }
    existing_neo = out / "neo4j_password"
    neo_pw = existing_neo.read_text() if existing_neo.exists() else str(values["neo4j_password"])
    values["neo4j_password"] = neo_pw
    values["neo4j_auth"] = f"neo4j/{neo_pw}"
    created, kept = [], []
    for name, value in values.items():
        path = out / name
        if path.exists():
            kept.append(name)
            continue
        path.write_text(str(value))
        path.chmod(0o600)
        created.append(name)
    print(f"created: {', '.join(created) or '-'}")
    print(f"kept (already present): {', '.join(kept) or '-'}")
    if not args.router_key_file and "oso_router_api_key_api" in created:
        print("NOTE: oso_router_api_key_api / _worker hold a placeholder: put the real router key in them before `docker compose up`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
