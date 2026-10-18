"""Admin-only calls the SDK deliberately does not have. Every write carries its own Idempotency-Key (lesson X6)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from examples._kit.workspace import Workspace


def register_principal(ws: Workspace, handle: str, kind: str = "agent", **body: Any) -> str:
    """Registers an agent or app and returns its key (shown once). Extra fields: scopes, allowed_profiles, quotas, ..."""
    with ws.client() as c:
        r = c.post(
            "/v1/principals",
            headers={"idempotency-key": str(uuid.uuid4())},
            json={"kind": kind, "handle": handle, "display_name": handle, **body},
        )
    _ok(r, 201)
    key: str = r.json()["key"]["plaintext"]
    ws.keys[handle] = key
    return key


def post(ws: Workspace, path: str, json: Any, *, key: str | None = None, expect: tuple[int, ...] = (200, 201, 202)) -> dict[str, Any]:
    with ws.client(key) as c:
        r = c.post(path, headers={"idempotency-key": str(uuid.uuid4())}, json=json)
    _ok(r, *expect)
    return dict(r.json())


def get(ws: Workspace, path: str, *, key: str | None = None, **params: Any) -> httpx.Response:
    with ws.client(key) as c:
        return c.get(path, params=params or None)


def _ok(r: httpx.Response, *codes: int) -> None:
    if r.status_code not in codes:
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:900]}")
