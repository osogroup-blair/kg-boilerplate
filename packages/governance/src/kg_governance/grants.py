"""Scope grants and the subject filter (phase-5 plan §5.2, phase-7 plan §5.1-5.2, ADR-029/037).

A grant is a row of `principal_scopes`: (kind, ref, privileges). Privileges are a subset of {read, propose, publish}:

| privilege | what it lets the principal do with the scope                                                              |
|-----------|-----------------------------------------------------------------------------------------------------------|
| `read`    | see memory and (for `subject:` grants on personal entity types) entities and facts of it                   |
| `propose` | write a memory or proposal into the scope (it still goes through the ontology policy)                      |
| `publish` | let an AGENT's shared proposal activate without review, when the ontology policy also permits (ADR-038)    |

Scopes do not inherit from one another: a `project:*` grant reveals nothing that carries only a `subject:` scope. Visibility is a predicate built here as SQL text with bound
parameters and embedded in the SAME statement that reads or ranks, so there is no unscoped candidate list anywhere (R4, SEC-02); nothing is filtered after the query runs.
RLS is the workspace backstop; this is the primary, per-principal control.

`SubjectFilter` is the entity/fact half (P7): the active ontology declares which entity types are `subject_scoped`; an entity of such a type, and every fact with such an
entity at either end, is visible only to a principal that may see that subject. Every `kg_knowledge` read takes one as a REQUIRED argument, so a caller that forgets it does
not type-check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kg_common.errors import Conflict
from kg_domain.enums import Role
from kg_domain.principal import Principal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

DEFAULT_CEILING = "confidential"  # agents and apps see up to this unless granted `classification:<higher>`
CLASSIFICATION_ORDER: tuple[str, ...] = ("public", "internal", "confidential", "restricted", "local_only")
FULL_VIEW_ROLES = frozenset({Role.WORKSPACE_ADMIN, Role.REVIEWER})  # see every classification and every subject (they curate)
PRIVILEGES: tuple[str, ...] = ("read", "propose", "publish")
DEFAULT_PRIVILEGES: tuple[str, ...] = ("read", "propose")  # what a grant carries unless it says otherwise; `publish` is never implicit
GRANT_KINDS: tuple[str, ...] = (
    "subject",
    "project",
    "workspace",
    "classification",
)  # the kinds a grant row can carry (agent and session visibility is implicit)


class InvalidScope(Conflict):
    """The same answer whether the scope does not exist or the caller may not use it (no existence oracle)."""

    code, http_status, default_message = "invalid_scope", 422, "A requested scope is not available to you"


class ScopeLike(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def ref(self) -> str | None: ...


GrantRow = tuple[str, str] | tuple[str, str, Iterable[str]]


@dataclass(frozen=True)
class ScopeGrants:
    principal_id: uuid.UUID
    all_subjects: bool = False
    subjects: frozenset[str] = frozenset()
    all_projects: bool = False
    projects: frozenset[str] = frozenset()
    ceiling: str = DEFAULT_CEILING  # the highest classification this principal may read
    may_reveal_private: bool = False
    full_view: bool = False  # admin / reviewer: every subject by role, whatever the grants say
    rows: tuple[tuple[str, str], ...] = field(default=(), compare=False)  # the explicit grants, for display
    proposable: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)  # (kind, ref) grants carrying `propose`
    publishable: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)  # (kind, ref) grants carrying `publish`

    @classmethod
    def from_rows(cls, principal: Principal, rows: list[GrantRow], *, may_reveal_private: bool = False) -> ScopeGrants:
        norm: list[tuple[str, str, frozenset[str]]] = [
            (r[0], r[1], frozenset(r[2]) if len(r) == 3 else frozenset(DEFAULT_PRIVILEGES)) for r in rows
        ]
        subjects = {ref for kind, ref, privs in norm if kind == "subject" and "read" in privs}
        projects = {ref for kind, ref, privs in norm if kind == "project" and "read" in privs}
        ceiling = DEFAULT_CEILING
        classes = [ref for kind, ref, _ in norm if kind == "classification" and ref in CLASSIFICATION_ORDER]
        if classes:
            ceiling = max([ceiling, *classes], key=CLASSIFICATION_ORDER.index)
        full = bool(principal.roles & FULL_VIEW_ROLES)
        if full:
            ceiling = CLASSIFICATION_ORDER[-1]
        return cls(
            principal_id=principal.principal_id,
            all_subjects="*" in subjects,
            subjects=frozenset(subjects - {"*"}),
            all_projects="*" in projects,
            projects=frozenset(projects - {"*"}),
            ceiling=ceiling,
            may_reveal_private=may_reveal_private,
            full_view=full,
            rows=tuple(sorted((k, r) for k, r, _ in norm)),
            proposable=frozenset((k, r) for k, r, privs in norm if "propose" in privs),
            publishable=frozenset((k, r) for k, r, privs in norm if "publish" in privs),
        )

    @classmethod
    async def for_principal(cls, conn: AsyncConnection, principal: Principal, *, may_reveal_private: bool = False) -> ScopeGrants:
        rows = (
            await conn.execute(
                text("SELECT scope_kind, scope_ref, privileges FROM principal_scopes WHERE workspace_id = :ws AND principal_id = :p"),
                {"ws": principal.workspace_id, "p": principal.principal_id},
            )
        ).all()
        return cls.from_rows(
            principal, [(r.scope_kind, r.scope_ref, tuple(r.privileges)) for r in rows], may_reveal_private=may_reveal_private
        )

    # ---- write rules ----------------------------------------------------------------------------------------------------------------------

    @property
    def self_ref(self) -> str:
        return str(self.principal_id)

    def normalise(self, scope: ScopeLike) -> tuple[str, str]:
        """`self` and the caller's own id are the same agent scope; a session ref is `self/<run_id>`."""
        if scope.kind == "agent":
            if scope.ref not in ("self", self.self_ref):
                raise InvalidScope()
            return "agent", self.self_ref
        if scope.kind == "session":
            owner, _, run = (scope.ref or "").partition("/")
            if owner not in ("self", self.self_ref) or not run:
                raise InvalidScope()
            return "session", f"{self.self_ref}/{run}"
        return scope.kind, scope.ref or "*"

    def can_use(self, kind: str, ref: str) -> bool:
        """May this principal READ through this scope? Wildcards are grants, never values: a memory is never scoped to `subject:*`."""
        if kind == "workspace":
            return True
        if kind == "subject":
            return ref != "*" and (self.all_subjects or ref in self.subjects)
        if kind == "project":
            return ref != "*" and (self.all_projects or ref in self.projects)
        if kind == "agent":
            return ref == self.self_ref
        if kind == "session":
            return ref.startswith(f"{self.self_ref}/")
        return False

    def _holds(self, granted: frozenset[tuple[str, str]], kind: str, ref: str) -> bool:
        return ref != "*" and ((kind, "*") in granted or (kind, ref) in granted)

    def can_propose(self, kind: str, ref: str) -> bool:
        """May this principal WRITE into this scope? A subject or project scope needs a grant that carries `propose`; the private scopes are the caller's own."""
        if kind == "workspace":
            return True
        if kind in ("subject", "project"):
            return self.can_use(kind, ref) and self._holds(self.proposable, kind, ref)
        return self.can_use(kind, ref)

    def can_publish(self, kind: str, ref: str) -> bool:
        """May a shared memory written into this scope by this principal become active without review (when the ontology policy also allows it)?"""
        if kind == "workspace":
            return ("workspace", "*") in self.publishable
        if kind in ("subject", "project"):
            return self.can_use(kind, ref) and self._holds(self.publishable, kind, ref)
        return False

    def may_publish_all(self, scopes: list[tuple[str, str]]) -> bool:
        shared = [(k, r) for k, r in scopes if k in ("workspace", "subject", "project")]
        return bool(shared) and all(self.can_publish(k, r) for k, r in shared)

    def writable(self, scopes: Sequence[ScopeLike]) -> list[tuple[str, str]]:
        """The normalised, de-duplicated scope rows for a new memory, or InvalidScope. A principal may only write scopes it can see AND holds `propose` on."""
        out: dict[tuple[str, str], None] = {}
        for scope in scopes:
            kind, ref = self.normalise(scope)
            if not self.can_propose(kind, ref):
                raise InvalidScope()
            out[(kind, ref)] = None
        return list(out)

    def can_see_subject(self, entity_id: uuid.UUID | str) -> bool:
        return self.full_view or self.all_subjects or str(entity_id) in self.subjects

    def classifications(self) -> list[str]:
        """Every classification up to and including this principal's ceiling."""
        return list(CLASSIFICATION_ORDER[: CLASSIFICATION_ORDER.index(self.ceiling) + 1])

    def subject_filter(self, scoped_types: Iterable[str]) -> SubjectFilter:
        """The entity/fact filter for the entity types the ACTIVE ontology declares `subject_scoped` (empty: nothing is enforced, the P2-P6 behaviour)."""
        return SubjectFilter(
            scoped_types=frozenset(scoped_types), full=self.full_view or self.all_subjects, subject_ids=frozenset(self.subjects)
        )


@dataclass(frozen=True)
class SubjectFilter:
    """Which entities (and so which facts) a principal may see. `scoped_types` are the entity types that need a `subject:` grant; `full` means no restriction applies
    to this principal (admin / reviewer by role, or a `subject:*` grant)."""

    scoped_types: frozenset[str] = frozenset()
    full: bool = False
    subject_ids: frozenset[str] = frozenset()

    @classmethod
    def unrestricted(cls) -> SubjectFilter:
        """For code that is not acting for a restricted principal: a reviewer's full view, the worker, a migration. It is an explicit choice at the call site."""
        return cls(full=True)

    @property
    def enforced(self) -> bool:
        return bool(self.scoped_types) and not self.full

    def allows(self, entity_type: str, entity_id: uuid.UUID | str) -> bool:
        """The pure form of `entity_visible_sql`, for code that already holds the row (tests, the Lab's explanations). Reads use the SQL form."""
        return not self.enforced or entity_type not in self.scoped_types or str(entity_id) in self.subject_ids


def entity_visible_sql(f: SubjectFilter, *, alias: str = "e", prefix: str = "sf", type_col: str = "type_key") -> tuple[str, dict[str, Any]]:
    """A predicate over an entity row aliased `alias`. Two rules, both in the SQL so no read can forget them:

    * an entity frozen by a deletion request (`deletion_pending`, P8 SEC-05) is invisible to EVERYONE, whatever their grants: a deleted subject answers like a missing one the
      moment the request is accepted;
    * when the active ontology scopes the entity's type, the principal needs a grant on it (P7).

    Bound parameters are namespaced with `prefix` so several can compose into one statement."""
    not_frozen = f"{alias}.status <> 'deletion_pending'"
    if not f.enforced:
        return not_frozen, {}
    sql = f"({not_frozen} AND ({alias}.{type_col} <> ALL(CAST(:{prefix}_types AS text[])) OR {alias}.id = ANY(CAST(:{prefix}_ids AS uuid[]))))"
    return sql, {f"{prefix}_types": sorted(f.scoped_types), f"{prefix}_ids": sorted(f.subject_ids)}


def fact_visible_sql(f: SubjectFilter, *, source: str = "s", target: str = "t", prefix: str = "sf") -> tuple[str, dict[str, Any]]:
    """A fact is visible only when BOTH of its endpoints are (a hidden person stays fully hidden, including in degree counts; a frozen one too). `source` / `target` alias the
    joined entity rows. A fact frozen on its own (it rested only on a deleted interaction) is hidden by its own status in every fact read."""
    a, pa = entity_visible_sql(f, alias=source, prefix=f"{prefix}s")
    b, pb = entity_visible_sql(f, alias=target, prefix=f"{prefix}t")
    return f"({a} AND {b})", {**pa, **pb}
