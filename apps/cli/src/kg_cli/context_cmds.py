"""`kg context` (phase 6): build a context AS an agent (the Context Lab's act-as, from a terminal), read a trace, purge old traces.

`build` and `trace` talk to the API with the workspace admin's key from `.local/dev-credentials.json`: there is no second path to a context, so what this prints is exactly
what the agent would get, authorised the same way (ADR-035). `purge-traces` runs the retention function, which returns a count and nothing else."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

import httpx
import typer
from kg_common.settings import REPO_ROOT, get_settings
from kg_db.engine import create_app_engine
from kg_db.session import system_session
from sqlalchemy import text

context_app = typer.Typer(no_args_is_help=True, help="Context engine: build as an agent (act-as), read a trace, purge old traces")
CREDENTIALS = REPO_ROOT / ".local" / "dev-credentials.json"
Workspace = Annotated[str, typer.Option(help="workspace slug")]


def _client(workspace: str) -> httpx.Client:
    keys: dict[str, str] = json.loads(CREDENTIALS.read_text()) if CREDENTIALS.exists() else {}
    key = keys.get(f"{workspace}/admin@{workspace}")
    if key is None:
        typer.echo(f"error: no admin key for {workspace} in {CREDENTIALS.name} (run `make seed`)", err=True)
        raise typer.Exit(2)
    return httpx.Client(
        base_url=os.environ.get("KG_API_URL", "http://127.0.0.1:8000"), headers={"authorization": f"Bearer {key}"}, timeout=60
    )


def _check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        err = r.json().get("error", {})
        typer.echo(f"error: {r.status_code} {err.get('code')}: {err.get('message')} {err.get('details') or ''}", err=True)
        raise typer.Exit(1)
    return r.json()


@context_app.command("build")
def build(
    task: Annotated[str, typer.Argument(help="the task the context is for")],
    workspace: Workspace = "demo",
    as_: Annotated[str, typer.Option("--as", help="the agent or app principal to build as (a handle, e.g. agent_planning)")] = "",
    subject: Annotated[list[str] | None, typer.Option(help="entity id of a subject (repeatable)")] = None,
    profile: str = "quick_answer",
    budget: Annotated[int | None, typer.Option(help="max tokens (never above the profile's cap)")] = None,
    project: Annotated[str | None, typer.Option(help="project key")] = None,
    as_of: Annotated[str | None, typer.Option(help="valid time, ISO date or datetime")] = None,
    known_at: Annotated[str | None, typer.Option(help="record time, ISO date or datetime")] = None,
    compression: Annotated[str | None, typer.Option(help="structured | extractive | summarized")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="print the whole package as JSON")] = False,
) -> None:
    with _client(workspace) as c:
        headers: dict[str, str] = {}
        if as_:
            people = _check(c.get("/v1/principals", params={"limit": 100}))["items"]
            match = next((p for p in people if p["handle"] in (as_, f"{as_}@{workspace}")), None)
            if match is None:
                typer.echo(f"error: no principal '{as_}' in {workspace}", err=True)
                raise typer.Exit(1)
            headers["x-act-as-principal"] = match["id"]
        body: dict[str, Any] = {"task": task, "profile": profile, "subject_ids": subject or []}
        for key, value in (
            ("max_tokens", budget),
            ("project", project),
            ("as_of", as_of),
            ("known_at", known_at),
            ("compression", compression),
        ):
            if value is not None:
                body[key] = value
        out = _check(c.post("/v1/context/build", json=body, headers=headers))
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    b = out.get("budgets") or {}
    typer.echo(
        f"status={out['status']} profile={(out.get('profile') or {}).get('name')} tokens={b.get('used_tokens')}/{b.get('max_tokens')} context_id={out.get('context_id')}"
    )
    for u in out.get("unavailable_sources", []):
        typer.echo(f"  unavailable: {u['source']} ({u['reason']})")
    typer.echo(out["context_text"])


@context_app.command("trace")
def trace(context_id: str, workspace: Workspace = "demo", json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    with _client(workspace) as c:
        out = _check(c.get(f"/v1/context/{context_id}/trace"))
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"{out['profile']} ({out['profile_source']}) status={out['status']} tokens={out['used_tokens']}/{out['max_tokens']} compression={out['compression']}"
    )
    typer.echo(f"versions {out['versions']}")
    typer.echo(f"counts {out['counts']}")
    typer.echo(f"latencies {out['latencies']}")
    for u in out["unavailable"]:
        typer.echo(f"unavailable: {u['source']} ({u['reason']})")
    for i in out["items"]:
        verdict = "IN " if i["included"] else f"out ({i['exclusion_reason']})"
        typer.echo(f"  {i['position']:>2} {i['kind']:<8} {i['section']:<16} {i['tier']:<8} {i['score']:>7.3f} {verdict}  {i['id'] or ''}")


@context_app.command("purge-traces")
def purge_traces(
    older_than_days: Annotated[int, typer.Option(min=0, help="delete traces older than this (default: CONTEXT_TRACE_RETENTION_DAYS)")] = -1,
) -> None:
    settings = get_settings()
    days = settings.context_trace_retention_days if older_than_days < 0 else older_than_days

    async def run() -> tuple[int, int]:
        engine = create_app_engine(settings)
        try:
            async with system_session(engine) as conn:
                debug = (await conn.execute(text("SELECT ops.purge_expired_context_debug() AS n"))).one().n
                gone = (
                    (await conn.execute(text("SELECT ops.purge_expired_context_traces(make_interval(days => :d)) AS n"), {"d": days}))
                    .one()
                    .n
                )
            return int(gone), int(debug)
        finally:
            await engine.dispose()

    traces, snapshots = asyncio.run(run())
    typer.echo(f"purged {traces} traces older than {days} days and {snapshots} expired debug snapshots")
