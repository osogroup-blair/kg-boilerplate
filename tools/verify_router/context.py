"""Shared plumbing for the live verification kit: config from the environment, key redaction, result recording."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ALIASES = {"oso/fast", "oso/general", "oso/reasoning", "oso/code", "oso/code-high", "oso/vision", "oso/general-vision", "oso/frontier", "oso/embedding"}


class RouterUnreachable(Exception):
    pass


@dataclass
class Ctx:
    base_url: str                              # ends with /v1
    api_key: str = field(repr=False)           # never printed, never written
    results_dir: Path
    client: httpx.Client = field(init=False, repr=False)
    fixtures_dir: Path = ROOT / "tools/mock_router/fixtures"
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = httpx.Client(timeout=120)

    # ---- safety ----
    def redact(self, obj: Any) -> Any:
        s = json.dumps(obj, default=str)
        if self.api_key and self.api_key in s:
            s = s.replace(self.api_key, "[REDACTED]")
        return json.loads(s)

    def say(self, msg: str) -> None:
        line = msg.replace(self.api_key, "[REDACTED]") if self.api_key else msg
        self.log.append(line)
        print(line, flush=True)

    # ---- http ----
    @property
    def root_url(self) -> str:
        return self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url

    def headers(self, key: str | None = None, rid: str | None = None, **extra) -> dict:
        return {"Authorization": f"Bearer {self.api_key if key is None else key}", "x-request-id": rid or str(uuid.uuid4()), **extra}

    def chat(self, model: str, content: str = "Reply with the single word: ok", *, key: str | None = None, rid: str | None = None, **body) -> httpx.Response:
        payload = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": body.pop("max_tokens", 16), "temperature": 0, **body}
        return self.client.post(f"{self.base_url}/chat/completions", headers=self.headers(key, rid), json=payload)

    def embed(self, inputs, model: str = "oso/embedding") -> httpx.Response:
        return self.client.post(f"{self.base_url}/embeddings", headers=self.headers(), json={"model": model, "input": inputs})

    # ---- results ----
    def record(self, cid: str, status: str, evidence: dict, notes: str = "") -> dict:
        assert status in ("pass", "fail", "skipped")
        rec = {"id": cid, "tag": "LIVE", "status": status, "evidence": self.redact(evidence), "notes": notes, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f"live-{cid}.json").write_text(json.dumps(rec, indent=2, default=str) + "\n")
        self.say(f"  {cid}: {status.upper()}" + (f" - {notes[:200]}" if notes and status != "pass" else ""))
        return rec


def from_env(results_dir: Path) -> Ctx:
    if os.environ.get("OSO_ROUTER_MODE") != "live":
        raise SystemExit("OSO_ROUTER_MODE is not 'live'. Set OSO_ROUTER_MODE=live, OSO_ROUTER_BASE_URL and OSO_ROUTER_API_KEY, with the router running.")
    base = os.environ.get("OSO_ROUTER_BASE_URL", "https://models.oso.group/v1").rstrip("/")
    key = os.environ.get("OSO_ROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OSO_ROUTER_API_KEY is empty. Export a non-admin router key (scope 'chat'); it is never printed or written.")
    return Ctx(base, key, results_dir)


def check_reachable(ctx: Ctx) -> None:
    try:
        r = ctx.client.get(f"{ctx.root_url}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        raise RouterUnreachable(f"router not reachable at {ctx.root_url}/health ({type(e).__name__}). Check the address (OSO_ROUTER_BASE_URL, default https://models.oso.group/v1) and your network, then retry.")
