"""Mock-router control: strict mode and a fixture pack for the length of one example, always put back (lessons X1-X3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from examples._kit.env import Env


def _call(env: Env, method: str, path: str, **kw: Any) -> httpx.Response:
    if env.live:
        raise RuntimeError("mock helpers are not available with --live")
    r = httpx.request(method, f"{env.mock_url}{path}", timeout=10, **kw)
    r.raise_for_status()
    return r


@contextmanager
def mock_pack(env: Env, rules: list[dict[str, Any]], *, strict: bool = True) -> Iterator[None]:
    """Strict mode on, `rules` loaded; on exit the fixtures are removed and the previous strict mode restored."""
    previous = bool(_call(env, "GET", "/__mock/strict").json().get("enabled", False))
    try:
        _call(env, "POST", "/__mock/strict", json={"enabled": strict})
        _call(env, "POST", "/__mock/reload")
        if rules:
            _call(env, "POST", "/__mock/fixtures", json={"rules": rules})
        yield
    finally:
        _call(env, "DELETE", "/__mock/fixtures")
        _call(env, "POST", "/__mock/strict", json={"enabled": previous})


def inject_fault(env: Env, code: str = "provider_unavailable", count: int = 50, endpoint: str = "chat") -> None:
    """Queues `count` failures with the router error `code` for `endpoint`; each router call consumes one."""
    _call(env, "POST", "/__mock/faults", json={"faults": [{"code": code, "count": count, "endpoint": endpoint}]})


def clear_faults(env: Env) -> None:
    _call(env, "POST", "/__mock/faults", json={"faults": []})


def coverage_check(rules: list[dict[str, Any]], schemas: set[str]) -> set[str]:
    """Returns the structured-output schema names that no rule answers (lesson X3): an empty set means the pack covers them."""
    covered = {str(r.get("when", {}).get("schema_name")) for r in rules}
    return schemas - covered


def add_rules(env: Env, rules: list[dict[str, Any]]) -> None:
    """Adds rules to the pack already loaded (for answers that depend on ids created during the run). `mock_pack` removes them on exit."""
    _call(env, "POST", "/__mock/fixtures", json={"rules": rules})
