from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from examples._kit import admin as kit_admin
from examples._kit.workspace import Workspace
from kg_sandbox import seed as seeding
from kg_sandbox.config import Config
from kg_sandbox.store import Store, sha

log = logging.getLogger("kg.sandbox")
SCENARIOS = ("sarah",)


class StartIn(BaseModel):
    scenario: str = Field(default="sarah", pattern="^[a-z]{2,20}$")
    turnstile_token: str | None = None


def error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message, **extra}})


class Broker:
    """The state and the operations; `create_app` wires them to HTTP. The seeder and the clock are parameters so tests need no stack."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        seeder: Callable[[Config], seeding.Seeded] = seeding.seed_sarah,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cfg, self.store, self.seeder, self.now = cfg, store, seeder, now
        self.seeding = 0
        self.reaped_at = 0.0

    # ---- pool ---------------------------------------------------------------------------------------------------------------------
    def seed_one(self, scenario: str = "sarah") -> str:
        s = self.seeder(self.cfg)
        sid = uuid.uuid4().hex
        self.store.add_pool(
            sid=sid, slug=s.slug, scenario=scenario, admin_key=s.admin_key, conv_key=s.conv_key, keys=s.keys, ids=s.ids, baseline=s.baseline
        )
        return sid

    async def refill(self) -> None:
        for sc in SCENARIOS:
            while self.store.count("pool", sc) + self.seeding < self.cfg.pool_size:
                self.seeding += 1
                try:
                    await asyncio.to_thread(self.seed_one, sc)
                except Exception:
                    log.exception("seeding failed")
                    await asyncio.sleep(10)
                    break
                finally:
                    self.seeding -= 1

    # ---- sessions -----------------------------------------------------------------------------------------------------------------
    def check_limits(self, ip_hash: str) -> None:
        if self.store.count("active") >= self.cfg.max_active:
            raise error(503, "sandbox_full", "The sandbox is full right now. Please try again in a few minutes.", retry_after=300)
        if self.store.ip_recent(ip_hash) >= self.cfg.ip_per_hour:
            raise error(429, "rate_limited", f"At most {self.cfg.ip_per_hour} sandboxes per hour from one address.", retry_after=900)

    def start(self, scenario: str, ip_hash: str) -> dict[str, Any]:
        if scenario not in SCENARIOS:
            raise error(422, "unknown_scenario", f"Scenarios: {', '.join(SCENARIOS)}")
        self.check_limits(ip_hash)
        row = self.store.claim(scenario, ip_hash=ip_hash, ttl_seconds=self.cfg.ttl_hours * 3600)
        if row is None:
            sid = self.seed_one(scenario)  # the pool was empty: seed on demand (slower)
            row = self.store.claim(scenario, ip_hash=ip_hash, ttl_seconds=self.cfg.ttl_hours * 3600)
            assert row is not None and row["id"] == sid or row is not None
        keys = json.loads(row["keys_json"])
        return {
            "session_id": row["id"],
            "scenario": row["scenario"],
            "expires_at": _iso(self.now() + self.cfg.ttl_hours * 3600),
            "api_url": self.cfg.public_api_url,
            "console_url": self.cfg.console_url,
            "keys": keys,
            "ids": json.loads(row["ids_json"]),
        }

    def owned(self, sid: str, authorization: str | None) -> Any:
        row = self.store.get(sid)
        token = (authorization or "").removeprefix("Bearer ").strip()
        if row is None or row["state"] != "active" or not token or not secrets.compare_digest(sha(token), row["admin_key_hash"]):
            raise error(404, "not_found", "Resource not found")  # the same answer for a wrong id, a wrong key and an expired session
        return row

    def tour(self, row: Any) -> dict[str, bool]:
        """What the broker can see the visitor has done, by comparing the workspace with its counts right after seeding. Reads, searches and denials leave no record to count, so those tasks are ticked by the visitor."""
        base = json.loads(row["baseline_json"])
        ws = Workspace(slug=row["slug"], admin_key=row["admin_key"], base_url=self.cfg.api_url, keys={})
        now = seeding.counts(ws)
        fresh = max(0, now["contexts"] - base["contexts"])
        listed = kit_admin.get(ws, "/v1/context", limit=100)
        listed.raise_for_status()
        contexts = listed.json()["items"][:fresh]  # newest first
        return {
            "context": fresh > 0,
            "budget": any((c.get("max_tokens") or 10**9) <= 300 for c in contexts),
            "command": now["entities"] > base["entities"],
            "correct": now["corrections"] > base["corrections"],
            "deletion": now["deletions"] > base["deletions"],
        }

    def end(self, row: Any) -> None:
        self.store.set_state(row["id"], "deleting")
        try:
            status = seeding.delete_workspace(self.cfg, row)
            log.info("deleted workspace %s: %s", row["slug"], status)
        finally:
            self.store.set_state(row["id"], "gone")

    def reap(self) -> int:
        self.reaped_at = self.now()
        n = 0
        for row in self.store.due(self.now()):
            self.end(row)
            n += 1
        return n

    # ---- turnstile ------------------------------------------------------------------------------------------------------------------
    async def verify_turnstile(self, token: str | None, ip: str) -> None:
        if not self.cfg.turnstile_secret:
            return
        if not token:
            raise error(403, "challenge_required", "Complete the verification and try again.")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": self.cfg.turnstile_secret, "response": token, "remoteip": ip},
            )
        if not r.json().get("success"):
            raise error(403, "challenge_failed", "The verification failed. Please try again.")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def create_app(cfg: Config | None = None, *, broker: Broker | None = None, background: bool = True) -> FastAPI:
    cfg = cfg or Config.from_env()
    b = broker or Broker(cfg, Store(cfg.db_path))

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = []
        if background:
            if cfg.manage_mock:
                await asyncio.to_thread(seeding.load_fixture_pack, cfg)

            async def pool_loop() -> None:
                while True:
                    await b.refill()
                    await asyncio.sleep(15)

            async def reaper_loop() -> None:
                while True:
                    try:
                        await asyncio.to_thread(b.reap)
                    except Exception:
                        log.exception("reaper failed")
                    await asyncio.sleep(60)

            tasks = [asyncio.create_task(pool_loop()), asyncio.create_task(reaper_loop())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Sandbox broker", version="1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.allow_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["authorization", "content-type"],
        max_age=600,
    )

    @app.exception_handler(HTTPException)
    async def _err(_r: Request, exc: HTTPException) -> JSONResponse:
        body = (
            exc.detail
            if isinstance(exc.detail, dict) and "error" in exc.detail
            else {"error": {"code": "error", "message": str(exc.detail)}}
        )
        return JSONResponse(body, status_code=exc.status_code)

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "")) or "unknown"

    @app.get("/sandbox/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sessions": b.store.stats(), "pool_target": cfg.pool_size}

    @app.get("/sandbox/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus text format; the edge never publishes this path (deploy/sandbox/Caddyfile.sandbox)."""
        stats = b.store.stats()
        lines = ["# TYPE kg_sandbox_sessions gauge"]
        lines += [f'kg_sandbox_sessions{{state="{st}"}} {stats.get(st, 0)}' for st in ("pool", "active", "deleting", "gone")]
        lines += [
            "# TYPE kg_sandbox_pool_target gauge",
            f"kg_sandbox_pool_target {cfg.pool_size}",
            "# TYPE kg_sandbox_max_active gauge",
            f"kg_sandbox_max_active {cfg.max_active}",
            "# TYPE kg_sandbox_reaper_last_run_timestamp_seconds gauge",
            f"kg_sandbox_reaper_last_run_timestamp_seconds {b.reaped_at}",
            "# TYPE kg_sandbox_overdue gauge",
            f"kg_sandbox_overdue {len(b.store.due(b.now()))}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/sandbox/sessions", status_code=201)
    async def start(body: StartIn, request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        await b.verify_turnstile(body.turnstile_token, ip)
        ip_hash = hashlib.sha256(f"{cfg.ip_salt}:{ip}".encode()).hexdigest()[:24]
        return await asyncio.to_thread(b.start, body.scenario, ip_hash)

    @app.get("/sandbox/sessions/{sid}")
    async def status(sid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        row = b.owned(sid, authorization)
        tour = await asyncio.to_thread(b.tour, row)
        return {"session_id": sid, "expires_at": _iso(row["expires_at"]), "state": row["state"], "tour": tour}

    @app.post("/sandbox/sessions/{sid}/events/{script}", status_code=202)
    async def event(sid: str, script: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        row = b.owned(sid, authorization)
        if script not in seeding.SCRIPTS:
            raise error(404, "not_found", "Resource not found")
        out = await asyncio.to_thread(seeding.send_script, cfg, row, script)
        return {"event_id": out["event_id"], "status_url": f"{cfg.public_api_url}/v1/interactions/{out['event_id']}", "script": script}

    @app.get("/sandbox/scripts")
    async def scripts() -> dict[str, Any]:
        return {k: {"title": v["title"], "why": v["why"]} for k, v in seeding.SCRIPTS.items()}

    @app.delete("/sandbox/sessions/{sid}", status_code=204)
    async def end(sid: str, authorization: str | None = Header(default=None)) -> None:
        row = b.owned(sid, authorization)
        await asyncio.to_thread(b.end, row)

    app.state.broker = b
    return app
