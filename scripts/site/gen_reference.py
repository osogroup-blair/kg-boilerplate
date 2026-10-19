"""Generates the reference pages and the OpenAPI copy the API page loads. Nothing here is hand-written; run it again after `make openapi` or any change to
the SDK, the MCP tools, the CLI, the Makefile, the settings, the ontology models or the rule catalog. Output is deterministic."""

from __future__ import annotations

import ast
import io
import json
import re
import shutil
import sys
import tokenize
from pathlib import Path
from typing import Any

from scripts.site.common import BLOB, CONTENT, ROOT, SITE, escape_stray_angles

OUT = CONTENT / "reference"


def page(name: str, title: str, description: str, body: str, order: int, source: str) -> Path:
    path = OUT / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f'---\ntitle: "{title}"\ndescription: "{description}"\nsidebar:\n  order: {order}\n---\n\n'
    note = f"> Generated from [`{source}`]({BLOB}/{source}). Do not edit this page; change the source.\n\n"
    path.write_text(front + note + body.rstrip() + "\n")
    return path


def esc(s: str) -> str:
    return escape_stray_angles(s.replace("|", "\\|").replace("\n", " "))


# ---- OpenAPI ----------------------------------------------------------------------------------------------------------------------------
def openapi() -> list[Path]:
    public = SITE / "public"
    public.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "contracts" / "openapi.json", public / "openapi.json")
    vendor = public / "vendor"
    vendor.mkdir(exist_ok=True)
    scalar = SITE / "node_modules" / "@scalar" / "api-reference" / "dist" / "browser" / "standalone.js"
    if scalar.exists():
        shutil.copyfile(scalar, vendor / "scalar.js")
    spec = json.loads((ROOT / "contracts" / "openapi.json").read_text())
    rows = []
    for p, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            if method in ("get", "post", "put", "patch", "delete"):
                rows.append((p, method.upper(), (op.get("tags") or ["-"])[0], op.get("summary") or ""))
    by_tag: dict[str, list[tuple[str, str, str]]] = {}
    for p, m, t, s in rows:
        by_tag.setdefault(t, []).append((m, p, s))
    body = f"The platform exposes {len(spec['paths'])} paths ({len(rows)} operations). The [interactive reference](/api/) lets you try them; this page is the plain list.\n\n"
    body += "Authentication is `Authorization: Bearer <key>` with an API key (`kgk_…`) or a short-lived JWT. Every route declares an authorization policy and errors use [one envelope](/docs/reference/errors/).\n\n"
    for tag in sorted(by_tag):
        body += f"## {tag}\n\n| Method | Path | Summary |\n| --- | --- | --- |\n"
        body += "".join(f"| `{m}` | `{p}` | {esc(s)} |\n" for m, p, s in sorted(by_tag[tag], key=lambda x: (x[1], x[0])))
        body += "\n"
    return [page("api-operations", "API operations", "Every route, grouped by tag.", body, 2, "contracts/openapi.json")]


# ---- Python SDK -------------------------------------------------------------------------------------------------------------------------
def sdk() -> Path:
    src = (ROOT / "sdk/python/src/kg_platform_sdk/client.py").read_text()
    tree = ast.parse(src)
    body = "Install the workspace (`make sync`) and import from `kg_platform_sdk`. `KGClient` is async; `KGClientSync` has the same methods without `await`. Both only call the HTTP API and forward your key.\n\n"
    body += '```python\nfrom kg_platform_sdk import KGClient\n\nasync with KGClient(base_url="http://localhost:8000", api_key=KEY) as kg:\n    ctx = await kg.build_context("Help Sarah assess a business", subject_ids=[sarah_id], profile="planning", max_tokens=600)\n    print(ctx.as_prompt_block())\n```\n\n'
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "KGClient":
            body += "## `KGClient` methods\n\n"
            for fn in node.body:
                if isinstance(fn, ast.AsyncFunctionDef) and not fn.name.startswith("_"):
                    sig = ast.unparse(fn.args)
                    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
                    doc = ast.get_docstring(fn) or ""
                    body += f"### `{fn.name}`\n\n```python\nasync def {fn.name}({sig}){ret}\n```\n\n{doc}\n\n"
    body += "## Errors\n\nEvery non-2xx answer raises a typed exception (`AuthError`, `NotFound`, `InvalidEvidence`, `QuotaExceeded`, `ContextIncomplete`, ...) with `code`, `status`, `request_id`, `details` and `retry_after`. See [the integration contract](/docs/guides/agent-integration/).\n"
    return page(
        "python-sdk",
        "Python SDK",
        "Every public method of KGClient with its signature.",
        body,
        3,
        "sdk/python/src/kg_platform_sdk/client.py",
    )


# ---- MCP tools --------------------------------------------------------------------------------------------------------------------------
def mcp() -> Path:
    from kg_mcp.tools import TOOLS, input_schema

    body = "`make mcp-up` (or `python -m kg_mcp`) serves these nine tools over stateless Streamable HTTP at `/mcp`. Send `Authorization: Bearer <your key>` with every request; the server holds no credential. An undeclared argument (`workspace_id`, `agent_id`, ...) is a validation error.\n\n"
    for t in TOOLS:
        schema = input_schema(t)
        props = schema.get("properties", {})
        req = set(schema.get("required", []))
        body += f"## `{t.name}`\n\n{t.description}\n\n"
        if props:
            body += "| Argument | Type | Required | Notes |\n| --- | --- | --- | --- |\n"
            for k, v in props.items():
                typ = v.get("type") or (
                    "one of " + ", ".join(x.get("type", "object") for x in v.get("anyOf", [])) if v.get("anyOf") else "object"
                )
                body += f"| `{k}` | {typ} | {'yes' if k in req else 'no'} | {esc(str(v.get('description', '')))} |\n"
            body += "\n"
        else:
            body += "No arguments.\n\n"
        body += f"<details><summary>JSON Schema</summary>\n\n```json\n{json.dumps(schema, indent=2, sort_keys=True)}\n```\n\n</details>\n\n"
    return page("mcp-tools", "MCP tools", "The nine MCP tools with their input schemas.", body, 4, "apps/mcp/src/kg_mcp/tools.py")


# ---- CLI --------------------------------------------------------------------------------------------------------------------------------
def cli() -> Path:
    import typer
    from kg_cli.main import app

    root = typer.main.get_command(app)
    out: list[str] = []

    def walk(cmd: Any, path: list[str], parent: Any) -> None:
        if path[1:]:
            head = f"{'#' * min(len(path), 4)} `{' '.join(path)}`\n\n"
            text = (cmd.help or getattr(cmd, "short_help", "") or "").strip()
            sub = f"Subcommands: {', '.join(f'`{n}`' for n in sorted(cmd.commands))}\n\n" if hasattr(cmd, "commands") else ""
            params = [p for p in cmd.params if getattr(p, "name", "") != "help"]
            table = ""
            if params:
                table = "| Option or argument | Required | Default | What it does |\n| --- | --- | --- | --- |\n"
                for prm in params:
                    names = " / ".join(getattr(prm, "opts", None) or [prm.name.upper()])
                    default = getattr(prm, "default", None)
                    shown = (
                        "" if default in (None, ()) or str(default).startswith("Sentinel") else str(default).replace(str(ROOT), "<repo>")
                    )
                    table += f"| `{names}` | {'yes' if prm.required else 'no'} | {esc(shown)} | {esc(getattr(prm, 'help', None) or '')} |\n"
                table += "\n"
            out.append(head + (text + "\n\n" if text else "") + sub + table)
        if hasattr(cmd, "commands"):
            for name in sorted(cmd.commands):
                walk(cmd.commands[name], [*path, name], cmd)

    walk(root, ["kg"], None)
    head = "The `kg` command runs from the repository (`uv run kg …`). It talks to the database directly for operator tasks (workspaces, keys, reconcile, backups, deletion sweeps) and is not a client of the HTTP API.\n\n"
    return page(
        "cli", "The kg command line", "Every kg command with its help text.", head + "\n".join(out), 5, "apps/cli/src/kg_cli/main.py"
    )


# ---- Make targets -----------------------------------------------------------------------------------------------------------------------
def make_targets() -> Path:
    rows = []
    for line in (ROOT / "Makefile").read_text().splitlines():
        m = re.match(r"^([a-zA-Z0-9_-]+):[^#]*##\s*(.+)$", line)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
    groups = {
        "Start and stop": ("up", "down", "reset", "seed", "stack-up", "tools", "sync", "mcp-up", "help"),
        "Examples and notebooks": (
            "examples",
            "demo-sarah",
            "demo-crm",
            "eval-crm",
            "examples-agents",
            "notebooks",
            "examples-install",
            "test-examples",
        ),
        "Tests and checks": (
            "lint",
            "typecheck",
            "test",
            "test-int",
            "test-security",
            "test-contract",
            "test-resilience",
            "test-release",
            "test-e2e",
            "test-admin",
            "check",
        ),
        "Evaluation and release": tuple(
            n for n, _ in rows if n.startswith(("eval-", "perf-", "release-", "live-", "prod-", "scan", "obs-", "backup", "restore"))
        ),
        "Site": tuple(n for n, _ in rows if n.startswith(("site-", "tapes", "sandbox"))),
    }
    seen: set[str] = set()
    body = "Run `make help` for the same list in your terminal. Targets that need the stack say so; never run `make down` or `make reset` in the middle of a test run.\n\n"
    for g, names in groups.items():
        sel = [(n, d) for n, d in rows if n in names]
        if sel:
            body += f"## {g}\n\n| Target | What it does |\n| --- | --- |\n" + "".join(f"| `make {n}` | {esc(d)} |\n" for n, d in sel) + "\n"
            seen.update(n for n, _ in sel)
    rest = [(n, d) for n, d in rows if n not in seen]
    if rest:
        body += (
            "## Everything else\n\n| Target | What it does |\n| --- | --- |\n"
            + "".join(f"| `make {n}` | {esc(d)} |\n" for n, d in rest)
            + "\n"
        )
    return page("make-targets", "Make targets", "Every make target with its description.", body, 6, "Makefile")


# ---- Configuration ----------------------------------------------------------------------------------------------------------------------
def configuration() -> Path:
    path = ROOT / "packages/common/src/kg_common/settings.py"
    src = path.read_text()
    tree = ast.parse(src)
    comments: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string.lstrip("# ").strip()
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    lines = src.splitlines()
    rows = []
    section = "General"
    sections: dict[str, list[tuple[str, str, str, str]]] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # a standalone comment line directly above the field starts a section
            above = node.lineno - 2
            if above >= 0 and lines[above].strip().startswith("#") and comments.get(above + 1) and len(comments[above + 1]) > 20:
                section = comments[above + 1][:90]
            end = node.end_lineno or node.lineno
            note = " ".join(comments[ln] for ln in range(node.lineno, end + 1) if ln in comments)
            default = ast.get_source_segment(src, node.value) if node.value else "required"
            if default and "SecretStr(" in default:
                default = "(secret; the development default is in .env.example)"
            typ = ast.unparse(node.annotation)
            sections.setdefault(section, []).append((node.target.id.upper(), typ, (default or "").replace("\n", " "), note))
            rows.append(node.target.id)
    body = "Settings are read from environment variables (and `.env` in development). Names are case-insensitive. Defaults and bounds are the code's own; the comments are the author's notes.\n\n"
    for sec, items in sections.items():
        body += f"## {sec}\n\n| Variable | Type | Default | Notes |\n| --- | --- | --- | --- |\n"
        body += "".join(f"| `{n}` | `{esc(t)}` | `{esc(d)[:80]}` | {esc(c)} |\n" for n, t, d, c in items) + "\n"
    return page(
        "configuration",
        "Configuration",
        f"{len(rows)} settings with their types, defaults and notes.",
        body,
        7,
        "packages/common/src/kg_common/settings.py",
    )


# ---- Ontology spec and rule catalog -----------------------------------------------------------------------------------------------------
def ontology() -> list[Path]:
    from kg_ontology.rules.catalog import CATALOG
    from kg_ontology.spec import OntologySpec

    schema = OntologySpec.model_json_schema()
    top = schema.get("properties", {})
    body = "An ontology is one YAML document: the types of things that can be known, how they relate, the rules that govern writes, the memory policy and the context profiles. Publish it as an immutable version; validate and simulate first.\n\n## Top-level keys\n\n| Key | Meaning |\n| --- | --- |\n"
    for k, v in top.items():
        body += f"| `{k}` | {esc(str(v.get('description') or v.get('title') or ''))} |\n"
    example = (ROOT / "examples/ontologies/personal-knowledge.yaml").read_text().splitlines()[:70]
    body += (
        "\n## An annotated start\n\nThe first lines of `examples/ontologies/personal-knowledge.yaml`:\n\n```yaml\n"
        + "\n".join(example)
        + "\n```\n\n"
    )
    body += f"## Full JSON Schema\n\n<details><summary>Show the schema ({len(json.dumps(schema)) // 1024} KB)</summary>\n\n```json\n{json.dumps(schema, indent=2, sort_keys=True)}\n```\n\n</details>\n"
    spec_page = page(
        "ontology-spec",
        "The ontology spec",
        "The ontology YAML structure, generated from the validating models.",
        body,
        8,
        "packages/ontology/src/kg_ontology/spec.py",
    )
    rb = "The rule catalog is a **fixed** set of rule kinds: no expressions and no user code. A new kind needs code, fixtures and a conflict policy (a meta-test enforces all three). Declare rules in an ontology under an entity type, a relationship type or the workspace.\n\n"
    rb += "| Rule | Stage | Applies to | Default action | Allowed actions | What it does |\n| --- | --- | --- | --- | --- | --- |\n"
    for name in sorted(CATALOG):
        k = CATALOG[name]
        rb += f"| `{name}`{' (implicit)' if k.implicit else ''} | {k.stage} | {', '.join(sorted(k.applies_to))} | {k.default_action} | {', '.join(sorted(k.allowed_actions)) or '-'} | {esc(k.description)} |\n"
    rb += "\n## Conflict policy\n\n" + "".join(f"- **`{n}`**: {CATALOG[n].conflict_policy}\n" for n in sorted(CATALOG))
    return [
        spec_page,
        page(
            "rule-catalog",
            "The rule catalog",
            "Every rule kind an ontology can declare.",
            rb,
            9,
            "packages/ontology/src/kg_ontology/rules/catalog.py",
        ),
    ]


# ---- Errors -----------------------------------------------------------------------------------------------------------------------------
def errors() -> Path:
    found: dict[str, tuple[str, str, str]] = {}
    pat = re.compile(r'code, http_status, default_message = "([a-z_]+)", (\d+), "([^"]+)"')
    for root in ("packages", "apps", "sdk"):
        for p in sorted((ROOT / root).rglob("*.py")):
            if "/tests/" in p.as_posix() or "node_modules" in p.as_posix():
                continue
            for m in pat.finditer(p.read_text()):
                found.setdefault(m.group(1), (m.group(2), m.group(3), p.relative_to(ROOT).as_posix()))
    body = 'Every error uses one envelope. Quote `request_id` when asking for help.\n\n```json\n{"error": {"code": "not_found", "message": "Resource not found", "request_id": "01a1527d-…", "retry_after": null, "details": null}}\n```\n\n'
    body += "| Code | HTTP | Meaning | Defined in |\n| --- | --- | --- | --- |\n"
    for code, (status, msg, src) in sorted(found.items(), key=lambda x: (int(x[1][0]), x[0])):
        body += f"| `{code}` | {status} | {esc(msg)} | [`{src}`]({BLOB}/{src}) |\n"
    body += "\nMore codes are raised by specific features (for example `invalid_evidence`, `profile_not_allowed`, `exceeds_profile`, `quota_exceeded`, `rate_limited`, `context_incomplete`, `idempotency_mismatch`). The [integration contract](/docs/guides/agent-integration/) lists what an agent should do for each status.\n"
    return page("errors", "Errors", "The error envelope and the error codes.", body, 10, "packages/common/src/kg_common/errors.py")


def run() -> list[Path]:
    written = [*openapi(), sdk(), mcp(), cli(), make_targets(), configuration(), *ontology(), errors()]
    return written


if __name__ == "__main__":
    files = run()
    print(f"gen_reference: {len(files)} pages")
    sys.exit(0)
