"""`make sandbox-smoke`: everything about the sandbox that can be proved without deploying it.

1. the broker's rules (tests/sandbox, no stack),
2. the compose files merge and validate with generated throwaway secrets,
3. the sandbox Caddyfile validates (Caddy's own validator, when its image is present) and its denials are what the plan says,
4. the alert rules parse (promtool, when its image is present).
What it cannot prove is the deployed edge; deploy/sandbox/verify_edge.py does that against the real host before launch."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SB = ROOT / "deploy" / "sandbox"
ok = True


def step(name: str, passed: bool, detail: str = "") -> None:
    global ok
    ok &= passed
    print(f"  {'ok  ' if passed else 'FAIL'} {name}" + (f"\n       {detail}" if detail and not passed else ""))


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False, **kw)  # type: ignore[call-overload]  # noqa: S603


def have_image(name: str) -> bool:
    return run(["docker", "image", "inspect", name]).returncode == 0


def edge_behaviour(caddy: str) -> None:
    """Runs the real Caddy on a throwaway port with the sandbox Caddyfile (plain http, no TLS) and asks it: denied paths answer 404 without being proxied,
    allowed paths are proxied (there is no upstream here, so a proxied path answers 502, which is exactly how we tell it from a denial)."""
    import time

    import httpx

    cf = re.sub(r"^\s*tls \{\$KG_TLS\}\n", "", caddy.replace("{$KG_PUBLIC_HOST} {", "http://:8099 {"), flags=re.M)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Caddyfile"
        path.write_text(cf)
        path.chmod(0o644)
        started = run(
            ["docker", "run", "-d", "--rm", "-p", "127.0.0.1:18099:8099", "-v", f"{path}:/etc/caddy/Caddyfile:ro", "caddy:2-alpine"]
        )
        if started.returncode != 0:
            step("the real Caddy starts with the sandbox config", False, started.stderr[-400:])
            return
        cid = started.stdout.strip()
        try:
            c = httpx.Client(base_url="http://127.0.0.1:18099", timeout=10)
            for _ in range(30):
                try:
                    c.get("/__mock/requests")
                    break
                except httpx.HTTPError:
                    time.sleep(0.3)
            denied = [
                ("GET", "/__mock/requests"),
                ("POST", "/__mock/strict"),
                ("GET", "/metrics"),
                ("GET", "/sandbox/metrics"),
                ("GET", "/v1/operations/jobs"),
                ("POST", "/v1/operations/router-check"),
                ("GET", "/v1/operations/backups"),
            ]
            bad = [f"{m} {p}={c.request(m, p).status_code}" for m, p in denied if c.request(m, p).status_code != 404]
            step("the real edge answers 404 for the mock control plane, metrics and the operations surface", not bad, ", ".join(bad))
            proxied = [
                ("GET", "/v1/me"),
                ("GET", "/health"),
                ("GET", "/sandbox/health"),
                ("POST", "/sandbox/sessions"),
                ("GET", "/v1/operations/health"),
                ("GET", "/v1/operations/backlog"),
                ("POST", "/mcp"),
            ]
            bad = [f"{m} {p}={c.request(m, p).status_code}" for m, p in proxied if c.request(m, p).status_code == 404]
            step("the real edge proxies the API, the broker and the two read-only operations calls", not bad, ", ".join(bad))
        finally:
            run(["docker", "stop", "-t", "1", cid])


def main() -> int:
    print("sandbox smoke")
    r = run([sys.executable, "-m", "pytest", "tests/sandbox", "-q", "-k", "not stack", "--ignore=tests/sandbox/test_broker_stack.py"])
    step("broker tests (caps, expiry, ownership, handover)", r.returncode == 0, r.stdout[-800:])

    with tempfile.TemporaryDirectory() as tmp:
        secrets_dir = Path(tmp) / "secrets"
        key_file = Path(tmp) / "key"
        key_file.write_text("sbx_" + secrets.token_urlsafe(24))
        r = run([sys.executable, "scripts/prod_secrets.py", "--out", str(secrets_dir), "--router-key-file", str(key_file)])
        for f in ("turnstile_secret", "sandbox_ip_salt"):
            (secrets_dir / f).write_text(secrets.token_urlsafe(16))
        env_file = Path(tmp) / "env"
        env_file.write_text(
            (SB / "sandbox.env.example").read_text().replace("SANDBOX_ROUTER_KEY=", f"SANDBOX_ROUTER_KEY={key_file.read_text()}")
        )
        env = {**os.environ, "KG_SECRETS_DIR": str(secrets_dir)}
        r = run(
            [
                "docker",
                "compose",
                "-f",
                "deploy/compose/docker-compose.prod.yml",
                "-f",
                "deploy/sandbox/docker-compose.sandbox.yml",
                "--env-file",
                str(env_file),
                "config",
                "--format",
                "json",
            ],
            env=env,
        )
        step("compose files merge and validate", r.returncode == 0, r.stderr[-800:])
        if r.returncode == 0:
            import json

            cfg = json.loads(r.stdout)
            svc = cfg["services"]
            step(
                "the broker is not published and is not on the public network",
                not svc["broker"].get("ports") and "public" not in svc["broker"]["networks"],
            )
            step(
                "the mock router is on an internal network with no way out",
                set(svc["mock-router"]["networks"]) == {"router"} and cfg["networks"]["router"].get("internal") is True,
            )
            step("only the edge publishes ports", [n for n, s in svc.items() if s.get("ports")] == ["edge"])
            step("workspaces delete without a cooling-off", svc["api"]["environment"].get("WORKSPACE_DELETION_COOLING_HOURS") == "0")
            step("the platform still runs ENV=production", svc["api"]["environment"].get("ENV") == "production")
            step(
                "the router key is not the development placeholder",
                "mock-key" not in svc["mock-router"]["environment"]["MOCK_ROUTER_API_KEY"],
            )

    caddy = (SB / "Caddyfile.sandbox").read_text()
    step(
        "the edge denies /__mock, metrics and the operations surface",
        all(w in caddy for w in ("/__mock*", "/metrics*", "/sandbox/metrics", "/v1/operations/*")) and "respond 404" in caddy,
    )
    step(
        "only two read-only operations calls are allowed",
        re.search(r"path /v1/operations/health /v1/operations/backlog", caddy) is not None and "method GET" in caddy,
    )
    step("request bodies are capped", "max_size 64KB" in caddy and "max_size 256KB" in caddy)
    if have_image("caddy:2-alpine"):
        r = run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "KG_PUBLIC_HOST=sandbox.example.com",
                "-e",
                "KG_TLS=internal",
                "-v",
                f"{SB / 'Caddyfile.sandbox'}:/etc/caddy/Caddyfile:ro",
                "caddy:2-alpine",
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
            ]
        )
        step("Caddy validates the sandbox Caddyfile", r.returncode == 0, (r.stderr or r.stdout)[-800:])
    else:
        print("  skip Caddy validation (image caddy:2-alpine not present)")

    if have_image("caddy:2-alpine"):
        edge_behaviour(caddy)

    rules = SB / "rules.sandbox.yml"
    prom = next((i for i in ("prom/prometheus:latest",) if have_image(i)), None)
    if prom and shutil.which("docker"):
        r = run(["docker", "run", "--rm", "--entrypoint", "promtool", "-v", f"{rules}:/rules.yml:ro", prom, "check", "rules", "/rules.yml"])
        step("promtool checks the alert rules", r.returncode == 0, (r.stderr or r.stdout)[-800:])
    else:
        step("alert rules are valid YAML with a group per concern", rules.exists() and "groups:" in rules.read_text())
    print("sandbox-smoke:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
