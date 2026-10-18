"""Creates what the synthetic canary needs in a workspace (phase-9 WP9), through the public API, as that workspace's administrator.

    python scripts/canary_setup.py --api https://kg.example.com --admin-key-file admin.key [--insecure]

* `canary-reader`: a registered agent with a read grant on the project `canary`. Its API key is what the canary uses. Written to `deploy/compose/secrets/canary_key` (0600) unless --key-out says otherwise.
* one workspace-scoped memory ("The canary project status is green") that the canary task asks about, so a healthy build has evidence;
* `canary-private`: a second agent that saves ONE private memory containing a unique marker. The canary reader must never be able to see it, so the marker is also the leakage probe's search text.

Prints the marker for `CANARY_LEAK_MARKER` in .env.prod. Idempotent per run only if the handles are free: re-running with the same handles fails with 409 (revoke the old agents first)."""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--admin-key-file", type=Path, required=True)
    ap.add_argument("--key-out", type=Path, default=ROOT / "deploy" / "compose" / "secrets" / "canary_key")
    ap.add_argument("--insecure", action="store_true", help="do not verify TLS (a private CA); never for an internet-facing host")
    args = ap.parse_args()
    admin = args.admin_key_file.read_text().strip()
    with httpx.Client(base_url=args.api, verify=not args.insecure, timeout=30) as http:

        def call(method: str, path: str, key: str, body: dict | None = None) -> httpx.Response:
            headers = {"authorization": f"Bearer {key}"}
            if method == "POST":
                headers["idempotency-key"] = str(uuid.uuid4())
            return http.request(method, path, headers=headers, json=body)

        def register(handle: str, grants: list[dict]) -> dict:
            r = call("POST", "/v1/principals", admin, {"kind": "agent", "handle": handle, "display_name": handle, "grants": grants})
            if r.status_code != 201:
                sys.exit(f"registering {handle} failed: {r.status_code} {r.text[:200]}")
            return r.json()

        reader = register("canary-reader", [{"scope_kind": "project", "scope_ref": "canary", "privileges": ["read"]}])
        private = register("canary-private", [{"scope_kind": "project", "scope_ref": "canary", "privileges": ["read", "propose"]}])
        # something every canary build can find: one workspace-scoped fact the canary task asks about (without evidence a build answers insufficient_evidence, which the canary counts as a failure)
        seeded = call(
            "POST",
            "/v1/memories",
            admin,
            {
                "type": "semantic",
                "content": "The canary project status is green; its checks run every five minutes.",
                "scopes": [{"kind": "workspace", "ref": "*"}],
                "source_ref": "canary-setup",
                "purpose": "synthetic monitoring",
            },
        )
        if seeded.status_code not in (200, 201):
            sys.exit(f"saving the canary status memory failed: {seeded.status_code} {seeded.text[:200]}")
        marker = f"canary-marker-{secrets.token_hex(6)}"
        r = call(
            "POST",
            "/v1/memories",
            private["key"]["plaintext"],
            {"type": "semantic", "content": f"Private canary note {marker}.", "scopes": [{"kind": "agent", "ref": "self"}]},
        )
        if r.status_code not in (200, 201):
            sys.exit(f"saving the private marker memory failed: {r.status_code} {r.text[:200]}")
    args.key_out.parent.mkdir(parents=True, exist_ok=True)
    args.key_out.write_text(reader["key"]["plaintext"])
    args.key_out.chmod(0o600)
    print(f"canary key written to {args.key_out}")
    print(f"CANARY_LEAK_MARKER={marker}   # add to .env.prod, then: docker compose --profile canary up -d canary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
