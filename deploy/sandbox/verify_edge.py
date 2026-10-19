"""Run against the DEPLOYED sandbox before launch (and whenever the edge changes): the denials hold from the internet, and a session's lifecycle works.

    python deploy/sandbox/verify_edge.py https://sandbox.kg.example.com [--site-origin https://kg.example.com] [--expire-wait 130]

Needs httpx (the repository's Python environment has it). It starts ONE sandbox session; with --expire-wait it also ends it early and proves the key stops working."""

from __future__ import annotations

import argparse
import sys
import time

import httpx

fails = 0


def check(name: str, passed: bool, detail: str = "") -> None:
    global fails
    fails += 0 if passed else 1
    print(f"  {'ok  ' if passed else 'FAIL'} {name}" + (f"  ({detail})" if detail and not passed else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--site-origin", default="")
    ap.add_argument("--expire-wait", type=int, default=0, help="seconds to wait after ending a session before checking its key is refused")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    c = httpx.Client(base_url=base, timeout=60, follow_redirects=False)

    print("denials from the internet")
    for path in ("/__mock/requests", "/__mock/strict", "/metrics", "/sandbox/metrics", "/v1/operations/router-check", "/v1/operations/jobs", "/v1/operations/backups"):
        r = c.get(path)
        check(f"GET {path} is not served", r.status_code in (404, 405), str(r.status_code))
    r = c.post("/v1/operations/router-check")
    check("POST /v1/operations/router-check is not served", r.status_code == 404, str(r.status_code))
    check("the API answers its health check", c.get("/health").status_code == 200)
    check("the broker answers its health check", c.get("/sandbox/health").status_code == 200)
    r = c.get("/sandbox/health")
    check("the broker's health names no key", "kgk_" not in r.text and "sbx_" not in r.text)

    print("bodies are capped")
    r = c.post("/v1/entities", content=b"x" * 400_000, headers={"content-type": "application/json", "authorization": "Bearer kgk_invalid_invalid"})
    check("a 400 KB body to the API is refused", r.status_code in (401, 413), str(r.status_code))
    r = c.post("/sandbox/sessions", content=b"x" * 100_000, headers={"content-type": "application/json"})
    check("a 100 KB body to the broker is refused", r.status_code in (413, 422, 400), str(r.status_code))

    if args.site_origin:
        r = c.options("/sandbox/sessions", headers={"origin": args.site_origin, "access-control-request-method": "POST"})
        check("the site's origin is allowed by the broker (CORS)", r.headers.get("access-control-allow-origin") == args.site_origin, r.headers.get("access-control-allow-origin", "none"))
        r = c.options("/sandbox/sessions", headers={"origin": "https://evil.example", "access-control-request-method": "POST"})
        check("another origin is not", r.headers.get("access-control-allow-origin") != "https://evil.example")

    print("a session")
    r = c.post("/sandbox/sessions", json={"scenario": "sarah"})
    if r.status_code != 201:
        check("a session starts (Turnstile may be on: pass a token, or run this from an allowed address)", False, f"{r.status_code} {r.text[:120]}")
        return 1
    s = r.json()
    admin = {"authorization": f"Bearer {s['keys']['admin']}"}
    check("the admin key works against the API", c.get("/v1/me", headers=admin).status_code == 200)
    plan = {"authorization": f"Bearer {s['keys']['planning_agent']}"}
    ctx = c.post("/v1/context/build", headers=plan, json={"task": "Help Sarah assess the implications of starting a business", "subject_ids": [s["ids"]["sarah"]], "profile": "planning", "max_tokens": 600, "project": "newco-plan"})
    check("a context builds for the planning agent", ctx.status_code == 200 and ctx.json().get("status") == "complete", ctx.text[:120])
    out = c.get(f"/v1/entities/{s['ids']['sarah']}", headers={"authorization": f"Bearer {s['keys']['ungranted_agent']}"})
    check("the agent with no grants gets a 404", out.status_code == 404)
    check("a wrong key gets the broker's uniform 404", c.get(f"/sandbox/sessions/{s['session_id']}", headers={"authorization": "Bearer kgk_nope"}).status_code == 404)
    check("the visitor's own key reads its session", c.get(f"/sandbox/sessions/{s['session_id']}", headers=admin).status_code == 200)

    c.delete(f"/sandbox/sessions/{s['session_id']}", headers=admin)
    if args.expire_wait:
        time.sleep(args.expire_wait)
    r = c.get("/v1/me", headers=admin)
    check("after the session ends, its key is refused", r.status_code in (401, 403, 404), str(r.status_code))
    print("verify_edge:", "OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
