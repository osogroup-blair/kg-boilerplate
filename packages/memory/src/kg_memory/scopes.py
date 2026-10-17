"""Scope grants and the SQL visibility predicate (phase-5 plan §5.2, ADR-029).

A principal sees a memory when ANY of the memory's scopes is visible to it. Scopes do not inherit from one another: a `project:*` grant reveals nothing that
carries only a `subject:` scope. The predicate is built as SQL text with bound parameters and is embedded in the SAME statement that ranks (search) or lists, so
there is no unscoped candidate list anywhere (R4, SEC-02). RLS is the workspace backstop; this is the primary, per-principal control.

| scope                  | who sees it                                                                                                   |
|------------------------|--------------------------------------------------------------------------------------------------------------|
| `workspace:*`          | every principal with `memory.read`                                                                           |
| `subject:<entity_id>`  | principals granted `subject:<id>` or `subject:*` (rows in `principal_scopes`)                                |
| `project:<key>`        | principals granted `project:<key>` or `project:*`                                                            |
| `agent:<principal_id>` | that principal only                                                                                          |
| `session:<pid>/<run>`  | that principal, when the request names the same run (or `all_runs`); never anyone else                      |

A workspace admin may additionally reveal other principals' private (agent / session) memory with `memory.read_private`: audited, explicit, never default.

`ScopeGrants` (with the P7 privileges) lives in `kg_governance.grants`; this module re-exports it and keeps the memory-specific SQL predicate.
"""

from __future__ import annotations

from typing import Any

from kg_governance.grants import CLASSIFICATION_ORDER, DEFAULT_CEILING, FULL_VIEW_ROLES, InvalidScope, ScopeGrants

from kg_memory.models import SHARED_KINDS

__all__ = ["CLASSIFICATION_ORDER", "DEFAULT_CEILING", "FULL_VIEW_ROLES", "InvalidScope", "ScopeGrants", "is_shared", "visibility_sql"]


def is_shared(scopes: list[tuple[str, str]]) -> bool:
    return any(kind in SHARED_KINDS for kind, _ in scopes)


def visibility_sql(
    g: ScopeGrants, *, run_id: str | None = None, all_runs: bool = False, reveal_private: bool = False, memory: str = "m", prefix: str = "v"
) -> tuple[str, dict[str, Any]]:
    """`EXISTS (...)` over `memory_scopes` for the memory aliased `memory`. Parameters are namespaced with `prefix` so it composes into any statement."""

    def p(name: str) -> str:
        return f"{prefix}_{name}"

    params: dict[str, Any] = {
        p("all_subjects"): g.all_subjects,
        p("subjects"): sorted(g.subjects),
        p("all_projects"): g.all_projects,
        p("projects"): sorted(g.projects),
        p("self_ref"): g.self_ref,
        p("session_ref"): f"{g.self_ref}/{run_id}" if run_id else "",
        p("all_runs"): bool(all_runs),
        p("self_prefix"): f"{g.self_ref}/%",
        p("reveal"): bool(reveal_private and g.may_reveal_private),
    }
    sql = (
        f"EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = {memory}.workspace_id AND s.memory_id = {memory}.id AND ("  # noqa: S608 (alias and parameter names are constants; values are bound)
        f"s.scope_kind = 'workspace' "
        f"OR (s.scope_kind = 'subject' AND (:{p('all_subjects')} OR s.scope_ref = ANY(CAST(:{p('subjects')} AS text[])))) "
        f"OR (s.scope_kind = 'project' AND (:{p('all_projects')} OR s.scope_ref = ANY(CAST(:{p('projects')} AS text[])))) "
        f"OR (s.scope_kind = 'agent' AND s.scope_ref = :{p('self_ref')}) "
        f"OR (s.scope_kind = 'session' AND ((:{p('session_ref')} <> '' AND s.scope_ref = :{p('session_ref')}) OR (:{p('all_runs')} AND s.scope_ref LIKE :{p('self_prefix')}))) "
        f"OR (:{p('reveal')} AND s.scope_kind IN ('agent', 'session'))))"
    )
    return sql, params
