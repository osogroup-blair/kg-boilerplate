"""`kg principals` (phase 7): register, update, rotate keys, grant, revoke and oversee agents and apps from a terminal.

Like `kg context`, these talk to the API with the workspace admin's key from `.local/dev-credentials.json`: there is no second path, so what the CLI does is authorised, ledgered
and audited exactly as the console's Agents & Access is. A minted key is printed ONCE (to stdout, nothing else on that line)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Annotated, Any

import httpx
import typer
from kg_common.settings import REPO_ROOT

principals_app = typer.Typer(no_args_is_help=True, help="Agents & Access: register, update, keys, grants, revoke, activity")
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
    return r.json() if r.content else None


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": f"cli-{uuid.uuid4().hex}"}


def _resolve(c: httpx.Client, handle: str) -> dict[str, Any]:
    cursor: str | None = None
    while True:
        page = _check(c.get("/v1/principals", params={"limit": 100, **({"cursor": cursor} if cursor else {})}))
        for p in page["items"]:
            if p["handle"] == handle:
                return dict(p)
        cursor = page.get("next_cursor")
        if not cursor:
            typer.echo(f"error: no principal '{handle}' in this workspace", err=True)
            raise typer.Exit(1)


def _grants(specs: list[str] | None) -> list[dict[str, Any]]:
    """`kind:ref=priv,priv` e.g. `subject:<id>=read,propose` or `project:newco-plan=read,propose,publish` (privileges default to read,propose)."""
    out = []
    for spec in specs or []:
        scope, _, privs = spec.partition("=")
        kind, _, ref = scope.partition(":")
        out.append({"scope_kind": kind, "scope_ref": ref, **({"privileges": privs.split(",")} if privs else {})})
    return out


@principals_app.command("list")
def principals_list(workspace: Workspace = "demo") -> None:
    with _client(workspace) as c:
        for p in _check(c.get("/v1/principals", params={"limit": 100}))["items"]:
            typer.echo(f"{p['handle']:<28} {p['kind']:<7} {p['status']:<8} {','.join(p['roles'])}")


@principals_app.command("register")
def principals_register(
    handle: str,
    workspace: Workspace = "demo",
    kind: Annotated[str, typer.Option(help="agent | app")] = "agent",
    name: Annotated[str, typer.Option(help="display name (default: the handle)")] = "",
    description: str = "",
    grant: Annotated[
        list[str] | None,
        typer.Option(help="kind:ref=privileges, repeatable (subject:<id>=read,propose; project:newco-plan=read,propose,publish)"),
    ] = None,
    profile: Annotated[list[str] | None, typer.Option(help="an allowed context profile, repeatable (none: every profile)")] = None,
    trusted_source: Annotated[bool, typer.Option(help="apps only: commands carry authoritative_system authority")] = False,
    model_disclosure: Annotated[str, typer.Option(help="allowed | denied: may this principal's content go to external models")] = "allowed",
    requests_per_minute: int | None = None,
    context_tokens_per_day: int | None = None,
    interactions_per_day: int | None = None,
) -> None:
    """Registers an agent or app and prints its first key ONCE."""
    body = {
        "kind": kind,
        "handle": handle,
        "display_name": name or handle,
        "description": description,
        "trusted_source": trusted_source,
        "model_disclosure": model_disclosure,
        "allowed_profiles": profile or [],
        "grants": _grants(grant),
        "quotas": {
            "requests_per_minute": requests_per_minute,
            "context_tokens_per_day": context_tokens_per_day,
            "interactions_per_day": interactions_per_day,
        },
    }
    with _client(workspace) as c:
        out = _check(c.post("/v1/principals", json=body, headers=_idem()))
    typer.echo(f"registered {out['principal']['handle']} ({out['principal']['id']}) with {len(out['grants'])} grants", err=True)
    typer.echo(out["key"]["plaintext"])


@principals_app.command("update")
def principals_update(
    handle: str,
    workspace: Workspace = "demo",
    description: str | None = None,
    model_disclosure: str | None = None,
    requests_per_minute: int | None = None,
    context_tokens_per_day: int | None = None,
    interactions_per_day: int | None = None,
    profile: Annotated[list[str] | None, typer.Option(help="replace the allowed profiles (repeatable)")] = None,
) -> None:
    with _client(workspace) as c:
        p = _resolve(c, handle)
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if model_disclosure is not None:
            body["model_disclosure"] = model_disclosure
        if profile is not None:
            body["allowed_profiles"] = profile
        quotas = {
            k: v
            for k, v in (
                ("requests_per_minute", requests_per_minute),
                ("context_tokens_per_day", context_tokens_per_day),
                ("interactions_per_day", interactions_per_day),
            )
            if v is not None
        }
        if quotas:
            body["quotas"] = quotas
        out = _check(c.patch(f"/v1/principals/{p['id']}", json=body, headers={**_idem(), "If-Match": f'W/"rev-{p["revision"]}"'}))
    typer.echo(f"updated {out['handle']} (revision {out['revision']})")


@principals_app.command("keys")
def principals_keys(
    handle: str,
    workspace: Workspace = "demo",
    mint: Annotated[bool, typer.Option(help="mint another key (printed once)")] = False,
    name: str = "",
    revoke: Annotated[str | None, typer.Option(help="revoke this key id")] = None,
) -> None:
    with _client(workspace) as c:
        p = _resolve(c, handle)
        if mint:
            out = _check(c.post(f"/v1/principals/{p['id']}/keys", json={"name": name}, headers=_idem()))
            typer.echo(out["plaintext"])
        elif revoke:
            _check(c.delete(f"/v1/principals/{p['id']}/keys/{revoke}", headers=_idem()))
            typer.echo(f"revoked key {revoke}")
        else:
            for k in _check(c.get(f"/v1/principals/{p['id']}/keys")):
                typer.echo(f"{k['id']}  {k['prefix']}…  {k['name'] or '-':<14} {'revoked' if k['revoked_at'] else 'active'}")


@principals_app.command("grants")
def principals_grants(
    handle: str,
    workspace: Workspace = "demo",
    add: Annotated[str | None, typer.Option(help="kind:ref=privileges")] = None,
    remove: Annotated[str | None, typer.Option(help="a grant id")] = None,
) -> None:
    with _client(workspace) as c:
        p = _resolve(c, handle)
        if add:
            out = _check(c.post(f"/v1/principals/{p['id']}/grants", json=_grants([add])[0], headers=_idem()))
            typer.echo(f"granted {out['scope_kind']}:{out['scope_ref']} [{','.join(out['privileges'])}]")
        elif remove:
            _check(c.delete(f"/v1/principals/{p['id']}/grants/{remove}", headers=_idem()))
            typer.echo(f"removed {remove}")
        else:
            for g in _check(c.get(f"/v1/principals/{p['id']}/grants")):
                typer.echo(f"{g['id']}  {g['scope_kind']}:{g['scope_ref']}  [{','.join(g['privileges'])}]")


@principals_app.command("revoke")
def principals_revoke(
    handle: str,
    workspace: Workspace = "demo",
    reason: Annotated[str, typer.Option(help="retired | compromised (compromised also quarantines what it produced)")] = "retired",
    note: str | None = None,
    yes: Annotated[bool, typer.Option("--yes", help="do not ask")] = False,
) -> None:
    """Final and immediate: there is no un-revoke (register a replacement)."""
    if not yes and not typer.confirm(f"Revoke '{handle}' as {reason}? This cannot be undone"):
        raise typer.Exit(1)
    with _client(workspace) as c:
        p = _resolve(c, handle)
        out = _check(c.post(f"/v1/principals/{p['id']}/revoke", json={"reason": reason, "note": note}, headers=_idem()))
    effects = {k: v for k, v in out.items() if k != "principal" and v}
    typer.echo(f"revoked {handle} ({reason}); effects: {json.dumps(effects, sort_keys=True)}")


@principals_app.command("activity")
def principals_activity(handle: str, workspace: Workspace = "demo", days: int = 30) -> None:
    with _client(workspace) as c:
        p = _resolve(c, handle)
        typer.echo(json.dumps(_check(c.get(f"/v1/principals/{p['id']}/activity", params={"days": days})), indent=2, sort_keys=True))
