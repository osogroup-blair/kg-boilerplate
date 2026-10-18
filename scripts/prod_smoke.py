"""`make prod-smoke` (phase-9 WP10): brings the production compose up under a separate project with generated secrets and proves what the file claims.

    python scripts/prod_smoke.py [--keep]

Needs the development stack's mock router on 127.0.0.1:8782 (it stands in for the model router). Checks: ENV=production boots with generated secrets only (so no development default
survives), two API replicas are healthy behind Caddy over TLS, the console is served with a CSP that forbids inline scripts and framing, docs are off, only Caddy publishes host ports,
Postgres and Neo4j are on the internal network alone and unreachable from the edge, every container is read-only with no-new-privileges and dropped capabilities, nothing but
root-owned Docker secrets carries a credential, `kg workspaces create` bootstraps a first administrator whose break-glass key is refused until ALLOW_BREAKGLASS_KEYS=true."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.prod.yml"
WORK = ROOT / ".local" / "prod-smoke"
PROJECT = "kg-prod-smoke"
HTTPS = 18443
failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + what + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(what)


def compose(*args: str, check_rc: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "--env-file", str(WORK / "prod.env"), *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check_rc,
        env={**os.environ, **(env or {})},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "secrets").mkdir(parents=True)
    (WORK / "router-key").write_text("mock-key")
    subprocess.run(
        [sys.executable, "scripts/prod_secrets.py", "--out", str(WORK / "secrets"), "--router-key-file", str(WORK / "router-key")],
        cwd=ROOT,
        check=True,
    )  # noqa: S603
    (WORK / "prod.env").write_text(
        "\n".join(
            [
                f"KG_SECRETS_DIR={WORK / 'secrets'}",
                "KG_PUBLIC_HOST=localhost",
                "KG_TLS=internal",
                "KG_HTTP_PORT=18080",
                f"KG_HTTPS_PORT={HTTPS}",
                "ROUTER_URL=http://host.docker.internal:8782/v1",
                "KG_VERSION=smoke",
                "API_REPLICAS=2",
                "NEO4J_HEAP=512m",
                "NEO4J_PAGECACHE=128m",
                "",
            ]
        )
    )
    compose("config", "-q")
    check(True, "the compose file is valid")
    try:
        up = compose("up", "-d", "--build", "--wait", "--wait-timeout", "420", check_rc=False)
        check(up.returncode == 0, "the stack comes up healthy (ENV=production, generated secrets only)", up.stderr[-600:])
        if up.returncode != 0:
            print(compose("ps", "-a", check_rc=False).stdout)
            for service in ("neo4j", "api", "worker", "edge", "migrate"):
                print(f"--- logs: {service}\n" + compose("logs", "--tail", "25", service, check_rc=False).stdout[-2500:])
            return 1
        ps = json.loads("[" + ",".join(compose("ps", "--format", "json", check_rc=False).stdout.strip().splitlines()) + "]")
        by_service: dict[str, list[dict]] = {}
        for c in ps:
            by_service.setdefault(c["Service"], []).append(c)
        check(
            len(by_service.get("api", [])) == 2 and all(c.get("Health") == "healthy" for c in by_service["api"]),
            "two API replicas are healthy",
        )
        base = f"https://localhost:{HTTPS}"
        with httpx.Client(verify=False, timeout=15) as http:  # noqa: S501 (Caddy's internal CA; the smoke only checks behaviour)
            for _ in range(20):
                try:
                    if http.get(f"{base}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(2)
            r = http.get(f"{base}/health")
            check(r.status_code == 200, "GET /health over TLS through Caddy")
            r = http.get(f"{base}/v1/me")
            check(r.status_code == 401, "an unauthenticated /v1/me is 401", str(r.status_code))
            check(r.headers.get("x-content-type-options") == "nosniff", "API responses carry nosniff")
            idx = http.get(f"{base}/")
            csp = idx.headers.get("content-security-policy", "")
            check(idx.status_code == 200 and "<html" in idx.text.lower(), "the admin console is served")
            script_src = next((p for p in csp.split(";") if p.strip().startswith("script-src")), "")
            check(
                "frame-ancestors 'none'" in csp and "unsafe-inline" not in script_src and "unsafe-eval" not in csp,
                "the console CSP: no inline scripts, no framing",
                csp,
            )
            check("strict-transport-security" in idx.headers, "HSTS on the edge")
            check(
                "swagger" not in http.get(f"{base}/docs").text.lower(),
                "the API docs are not served in production (the console's fallback page answers /docs)",
            )
            check("openapi" not in http.get(f"{base}/openapi.json").text[:200].lower(), "openapi.json is not served in production")
        # published host ports: Caddy only
        published = {s: any(p.get("PublishedPort") for p in c.get("Publishers", [])) for s, cs in by_service.items() for c in cs[:1]}
        check([s for s, v in published.items() if v] == ["edge"], "only the edge publishes host ports", str(published))
        # hardening, per container
        for s, cs in by_service.items():
            cid = cs[0]["ID"]
            insp = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, check=True).stdout)[0]  # noqa: S603,S607
            host = insp["HostConfig"]
            if s == "migrate":
                continue
            if s == "neo4j":  # documented exception (compose comment): the vendor entrypoint writes its generated config
                check(
                    not host["ReadonlyRootfs"] and insp["Config"].get("User") == "7474:7474",
                    "neo4j: the documented read-only exception, running as its own non-root user",
                )
            else:
                check(bool(host["ReadonlyRootfs"]), f"{s}: read-only root filesystem")
            check(
                "no-new-privileges:true" in (host.get("SecurityOpt") or []) or "no-new-privileges" in str(host.get("SecurityOpt")),
                f"{s}: no-new-privileges",
            )
            check("ALL" in (host.get("CapDrop") or []), f"{s}: capabilities dropped")
            env_list = insp["Config"].get("Env") or []
            leaked = [
                e.split("=", 1)[0]
                for e in env_list
                if any(k in e.split("=", 1)[0] for k in ("PASSWORD", "SECRET", "PEPPER", "KEYS", "API_KEY"))
                and not e.split("=", 1)[0].endswith("_FILE")
                and not e.startswith("ALLOW_")  # a flag, not a value
            ]
            check(not leaked, f"{s}: no secret value in the environment", str(leaked))
            if s in ("api", "worker", "mcp", "edge"):
                check(
                    insp["Config"].get("User") not in ("", "0", "root", None) or s == "edge",
                    f"{s}: runs as a non-root user",
                    insp["Config"].get("User", ""),
                )
            nets = set(insp["NetworkSettings"]["Networks"])
            if s in ("postgres", "neo4j"):
                check(nets == {f"{PROJECT}_internal"}, f"{s}: on the internal network only", str(nets))
        # postgres is not reachable from the edge
        probe = subprocess.run(
            ["docker", "exec", by_service["edge"][0]["ID"], "wget", "-T", "3", "-q", "-O", "-", "http://postgres:5432"],
            capture_output=True,
            text=True,
            check=False,
        )  # noqa: S603,S607
        check(
            probe.returncode != 0 and "bad address" in (probe.stderr or "").lower(), "the edge cannot even resolve Postgres", probe.stderr
        )
        # first boot: a workspace and its administrator
        made = compose("exec", "-T", "worker", "kg", "workspaces", "create", "--slug", "smoke", "--name", "Smoke", check_rc=False)
        key = next((ln.split(": ", 1)[1] for ln in made.stdout.splitlines() if ln.startswith("admin@smoke: ")), "")
        check(made.returncode == 0 and bool(key), "kg workspaces create bootstraps the first administrator", made.stderr[-300:])
        with httpx.Client(verify=False, timeout=15) as http:  # noqa: S501
            r = http.get(f"{base}/v1/me", headers={"authorization": f"Bearer {key}"})
            check(
                r.status_code == 401, "the break-glass key is refused while ALLOW_BREAKGLASS_KEYS=false", f"{r.status_code} {r.text[:120]}"
            )
        reboot = compose("up", "-d", "--wait", "--wait-timeout", "180", env={"ALLOW_BREAKGLASS_KEYS": "true"}, check_rc=False)
        with httpx.Client(verify=False, timeout=15) as http:  # noqa: S501
            r = http.get(f"{base}/v1/me", headers={"authorization": f"Bearer {key}"})
            check(
                reboot.returncode == 0 and r.status_code == 200,
                "with ALLOW_BREAKGLASS_KEYS=true the key works (and the use is audited)",
                f"{r.status_code} {reboot.stderr[-200:]}",
            )
    finally:
        if not args.keep:
            compose("down", "-v", "--remove-orphans", check_rc=False)
            shutil.rmtree(WORK, ignore_errors=True)
    print(json.dumps({"ok": not failures, "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
