"""Ontology registry operations (ONT-01, ONT-02, ONT-05 partial): drafts, validation, diff with impact counts, publish, rollback, retire.

Published versions are immutable and content-hashed (a trigger in Postgres enforces it even against the table owner); rollback appends an activation
and moves the pointer, it never edits a version."""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any, Literal

import yaml
from kg_common.errors import (
    Conflict,
    ImmutableVersion,
    NotFound,
    OntologyInvalid,
    PreconditionFailed,
    PreconditionRequired,
    ValidationFailed,
)
from kg_common.ids import uuid7
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_obs import metrics
from kg_ontology import (
    DiffResult,
    GraphCompilerPort,
    ImpactCount,
    OntologySpec,
    ValidationReport,
    canonical_dict,
    diff_specs,
    parse_document,
    validate_document,
)
from kg_ontology.report import Issue
from kg_ontology.rules.catalog import CATALOG, STAGES
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.models import (
    ActiveOntologyOut,
    OntologyVersionOut,
    OntologyVersionSummary,
    RuleCatalogOut,
    RuleKindOut,
)
from kg_knowledge.specs import SpecCache

_SELECT = """
    SELECT v.id, v.ontology_key, v.version, v.state, v.content_hash, v.revision, v.notes, v.created_at, v.published_at, v.retired_at,
           v.validation_report, v.spec, v.source_text, v.base_version_id, COALESCE(w.active_version_id = v.id, false) AS active
    FROM ontology_versions v LEFT JOIN workspace_ontology w ON w.workspace_id = v.workspace_id
"""
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _json_safe(doc: Any) -> Any:
    return json.loads(json.dumps(doc, default=str))


def _header(doc: Any) -> tuple[str | None, str | None]:
    header = doc.get("ontology") if isinstance(doc, dict) else None
    if not isinstance(header, dict):
        return None, None
    key = header.get("key", header.get("id"))
    version = header.get("version")
    return (key if isinstance(key, str) else None, version if isinstance(version, str) else None)


def _summary(row: Row[Any]) -> OntologyVersionSummary:
    report = row.validation_report or {}
    return OntologyVersionSummary(
        id=row.id,
        ontology_key=row.ontology_key,
        version=row.version,
        state=row.state,
        active=bool(row.active),
        content_hash=row.content_hash,
        revision=row.revision,
        error_count=len(report.get("errors", [])),
        notes=row.notes,
        created_at=row.created_at,
        published_at=row.published_at,
        retired_at=row.retired_at,
    )


def _full(row: Row[Any], *, activated: bool = False) -> OntologyVersionOut:
    return OntologyVersionOut(
        **_summary(row).model_dump(),
        spec=row.spec or {},
        source_text=row.source_text,
        base_version_id=row.base_version_id,
        validation_report=ValidationReport.model_validate(row.validation_report or {}),
        activated=activated,
    )


def _next_minor(versions: list[str]) -> str:
    best = (1, 0, 0)
    for v in versions:
        m = _SEMVER.match(v)
        if m:
            best = max(best, tuple(int(x) for x in m.groups()))  # type: ignore[assignment]
    return f"{best[0]}.{best[1] + 1}.0"


class OntologyService:
    def __init__(self, compiler: GraphCompilerPort, specs: SpecCache):
        self._compiler, self._specs = compiler, specs

    # ---- reads ----------------------------------------------------------------------------------------------------------------------------

    async def _row(self, conn: AsyncConnection, ws: uuid.UUID, version_id: uuid.UUID, *, lock: bool = False) -> Row[Any]:
        row = (
            await conn.execute(
                text(f"{_SELECT} WHERE v.workspace_id = :ws AND v.id = :id" + (" FOR UPDATE OF v" if lock else "")),
                {"ws": ws, "id": version_id},
            )
        ).first()  # noqa: S608
        if row is None:
            raise NotFound()
        return row

    async def list_versions(
        self, conn: AsyncConnection, ctx: WorkspaceContext, *, limit: int | None, cursor: str | None
    ) -> Page[OntologyVersionSummary]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        params: dict[str, Any] = {"ws": ctx.workspace_id, "n": size + 1}
        keyset = ""
        if before:
            keyset, params["c_at"], params["c_id"] = "AND (v.created_at, v.id) < (:c_at, :c_id)", before[0], before[1]
        rows = (
            await conn.execute(
                text(f"{_SELECT} WHERE v.workspace_id = :ws {keyset} ORDER BY v.created_at DESC, v.id DESC LIMIT :n"), params
            )
        ).all()  # noqa: S608
        page, more = rows[:size], len(rows) > size
        return Page[OntologyVersionSummary](
            items=[_summary(r) for r in page], next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None
        )

    async def get_version(self, conn: AsyncConnection, ctx: WorkspaceContext, version_id: uuid.UUID) -> OntologyVersionOut:
        return _full(await self._row(conn, ctx.workspace_id, version_id))

    async def active(self, conn: AsyncConnection, ctx: WorkspaceContext) -> ActiveOntologyOut:
        row = (
            await conn.execute(text(f"{_SELECT} WHERE v.workspace_id = :ws AND w.active_version_id = v.id"), {"ws": ctx.workspace_id})
        ).first()  # noqa: S608
        if row is None:
            return ActiveOntologyOut(version=None)
        spec = OntologySpec.model_validate(row.spec)
        return ActiveOntologyOut(
            version=_summary(row),
            entity_types=sorted(spec.entity_types),
            relationship_types=[{"type": n, "source": t.source, "target": t.target} for n, t in sorted(spec.relationship_types.items())],
        )

    @staticmethod
    def rule_catalog() -> RuleCatalogOut:
        kinds = [
            RuleKindOut(
                kind=k.kind,
                family=k.family,
                stage=k.stage,
                implicit=k.implicit,
                commands=sorted(k.commands),
                applies_to=sorted(k.applies_to),
                default_action=k.default_action,
                allowed_actions=sorted(k.allowed_actions),
                description=k.description,
                conflict_policy=k.conflict_policy,
                params_schema=k.params_model.model_json_schema(),
            )
            for k in CATALOG.values()
        ]
        return RuleCatalogOut(stages=STAGES, kinds=kinds)

    # ---- drafts ---------------------------------------------------------------------------------------------------------------------------

    def _analyse(self, content: str | dict[str, Any], fmt: Literal["yaml", "json"]) -> tuple[Any, ValidationReport, str | None]:
        """Returns (the JSON to store as `spec`, the report, the YAML source text). An invalid document is still stored so it can be fixed (ONT-01)."""
        from kg_ontology.spec import load_text

        if isinstance(content, dict):
            doc, parse_issues = content, list[Issue]()
        else:
            doc, parse_issues = load_text(content, fmt)
        if parse_issues:
            return {}, ValidationReport(errors=parse_issues), content if isinstance(content, str) else None
        spec, report = validate_document(doc, self._compiler)
        stored = canonical_dict(spec) if spec is not None else _json_safe(doc)
        source = (
            content if isinstance(content, str) and fmt == "yaml" else yaml.safe_dump(_json_safe(doc), sort_keys=False, allow_unicode=True)
        )
        return stored, report, source

    async def _assert_unique(
        self, conn: AsyncConnection, ws: uuid.UUID, key: str | None, version: str | None, own: uuid.UUID | None
    ) -> None:
        if key is None or version is None:
            return
        taken = (
            await conn.execute(
                text(
                    "SELECT 1 FROM ontology_versions WHERE workspace_id = :ws AND ontology_key = :k AND version = :v AND id IS DISTINCT FROM :own"
                ),
                {"ws": ws, "k": key, "v": version, "own": own},
            )
        ).first()
        if taken:
            raise Conflict("That ontology version already exists", code="version_exists")

    async def create_draft(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        audit: AuditWriter,
        *,
        content: str | dict[str, Any] | None,
        fmt: Literal["yaml", "json"],
        copy_of: uuid.UUID | None,
    ) -> OntologyVersionOut:
        ws = ctx.workspace_id
        base_id: uuid.UUID | None = None
        if copy_of is not None:
            src = await self._row(conn, ws, copy_of)
            doc = copy.deepcopy(src.spec) if isinstance(src.spec, dict) else {}
            header = doc.get("ontology")
            if not isinstance(header, dict):
                raise ValidationFailed("The source version has no ontology header to copy", details={"fields": ["copy_of"]})
            key = header.get("key", header.get("id"))
            existing = [
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT version FROM ontology_versions WHERE workspace_id = :ws AND ontology_key = :k"), {"ws": ws, "k": key}
                    )
                ).all()
            ]
            header["version"] = _next_minor([v for v in existing if v])
            content, fmt, base_id = doc, "json", copy_of
        assert content is not None
        stored, report, source = self._analyse(content, fmt)
        key, version = _header(stored if report.ok or isinstance(stored, dict) else {})
        await self._assert_unique(conn, ws, key, version, None)
        vid = uuid7()
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO ontology_versions (id, workspace_id, ontology_key, version, spec, source_text, validation_report, base_version_id, created_by) "
                        "VALUES (:id, :ws, :k, :v, CAST(:spec AS jsonb), :src, CAST(:rep AS jsonb), :base, :by)"
                    ),
                    {
                        "id": vid,
                        "ws": ws,
                        "k": key,
                        "v": version,
                        "spec": json.dumps(stored),
                        "src": source,
                        "rep": report.model_dump_json(),
                        "base": base_id,
                        "by": principal.principal_id,
                    },
                )
        except IntegrityError as exc:
            raise Conflict("That ontology version already exists", code="version_exists") from exc
        await audit.record(
            "ontology.draft_created",
            resource_type="ontology_version",
            resource_id=vid,
            metadata=AuditMetadata(version_id=vid, violation_count=len(report.errors)),
        )
        return _full(await self._row(conn, ws, vid))

    async def replace_draft(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        audit: AuditWriter,
        version_id: uuid.UUID,
        *,
        content: str | dict[str, Any],
        fmt: Literal["yaml", "json"],
        expected_revision: int | None,
    ) -> OntologyVersionOut:
        ws = ctx.workspace_id
        row = await self._row(conn, ws, version_id, lock=True)
        if row.state != "draft":
            raise ImmutableVersion()
        if expected_revision is None:
            raise PreconditionRequired()
        if expected_revision != row.revision:
            raise PreconditionFailed()
        stored, report, source = self._analyse(content, fmt)
        key, version = _header(stored)
        await self._assert_unique(conn, ws, key, version, version_id)
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE ontology_versions SET ontology_key = :k, version = :v, spec = CAST(:spec AS jsonb), source_text = :src, "
                        "validation_report = CAST(:rep AS jsonb), revision = revision + 1 WHERE workspace_id = :ws AND id = :id"
                    ),
                    {
                        "k": key,
                        "v": version,
                        "spec": json.dumps(stored),
                        "src": source,
                        "rep": report.model_dump_json(),
                        "ws": ws,
                        "id": version_id,
                    },
                )
        except IntegrityError as exc:
            raise Conflict("That ontology version already exists", code="version_exists") from exc
        await audit.record(
            "ontology.draft_updated",
            resource_type="ontology_version",
            resource_id=version_id,
            metadata=AuditMetadata(version_id=version_id, violation_count=len(report.errors)),
        )
        return _full(await self._row(conn, ws, version_id))

    async def delete_draft(self, conn: AsyncConnection, ctx: WorkspaceContext, audit: AuditWriter, version_id: uuid.UUID) -> None:
        row = await self._row(conn, ctx.workspace_id, version_id, lock=True)
        if row.state != "draft":
            raise ImmutableVersion()
        await conn.execute(
            text("DELETE FROM ontology_versions WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": version_id}
        )
        await audit.record(
            "ontology.draft_deleted",
            resource_type="ontology_version",
            resource_id=version_id,
            metadata=AuditMetadata(version_id=version_id),
        )

    # ---- validate / diff ------------------------------------------------------------------------------------------------------------------

    async def validate(self, conn: AsyncConnection, ctx: WorkspaceContext, version_id: uuid.UUID) -> ValidationReport:
        """Recomputes the report from the stored content. Read-only: the draft's stored report is refreshed by create/replace, not by this call."""
        row = await self._row(conn, ctx.workspace_id, version_id)
        if not isinstance(row.spec, dict) or not row.spec:
            return ValidationReport.model_validate(row.validation_report or {})
        _, report = validate_document(row.spec, self._compiler)
        return report

    async def _spec_of(self, row: Row[Any]) -> OntologySpec:
        spec, issues = parse_document(row.spec)
        if spec is None:
            raise OntologyInvalid(details={"errors": [i.model_dump() for i in issues][:50]})
        return spec

    async def diff(self, conn: AsyncConnection, ctx: WorkspaceContext, version_id: uuid.UUID, against: str) -> DiffResult:
        ws = ctx.workspace_id
        row = await self._row(conn, ws, version_id)
        new = await self._spec_of(row)
        old: OntologySpec | None = None
        if against == "active":
            active = await self._specs.active(conn, ws)
            old = active[1] if active else None
        else:
            try:
                other = uuid.UUID(against)
            except ValueError as exc:
                raise ValidationFailed("against must be 'active' or a version id", details={"fields": ["against"]}) from exc
            old = await self._spec_of(await self._row(conn, ws, other))
        result = diff_specs(old, new)
        result.impact = await self._impact(conn, ws, old, result)
        return result

    @staticmethod
    async def _impact(conn: AsyncConnection, ws: uuid.UUID, old: OntologySpec | None, result: DiffResult) -> list[ImpactCount]:
        """How many active entities/facts the changed types hold today (the rows a breaking change would touch)."""
        if old is None:
            return []
        entity_keys = {c.type_key for c in result.changes if c.type_key in old.entity_types}
        rel_keys = {c.type_key for c in result.changes if c.type_key in old.relationship_types}
        counts: dict[tuple[str, str], int] = {("entity", k): 0 for k in entity_keys} | {("relationship", k): 0 for k in rel_keys}
        if entity_keys:
            for r in (
                await conn.execute(
                    text(
                        "SELECT type_key, count(*) AS n FROM entities WHERE workspace_id = :ws AND status = 'active' AND type_key = ANY(:keys) GROUP BY type_key"
                    ),
                    {"ws": ws, "keys": sorted(entity_keys)},
                )
            ).all():
                counts[("entity", r.type_key)] = int(r.n)
        if rel_keys:
            for r in (
                await conn.execute(
                    text(
                        "SELECT rel_type, count(*) AS n FROM facts WHERE workspace_id = :ws AND status = 'active' AND rel_type = ANY(:keys) GROUP BY rel_type"
                    ),
                    {"ws": ws, "keys": sorted(rel_keys)},
                )
            ).all():
                counts[("relationship", r.rel_type)] = int(r.n)
        out = [ImpactCount(type_key=k, kind=kind, active=n) for (kind, k), n in counts.items()]
        return sorted(out, key=lambda i: (i.kind, i.type_key))

    # ---- publish / activate / retire ------------------------------------------------------------------------------------------------------

    @staticmethod
    async def _lock_workspace(conn: AsyncConnection, ws: uuid.UUID) -> uuid.UUID | None:
        """The workspace_ontology row is the lock that serialises publish and activate."""
        await conn.execute(text("INSERT INTO workspace_ontology (workspace_id) VALUES (:ws) ON CONFLICT DO NOTHING"), {"ws": ws})
        row = (
            await conn.execute(text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws FOR UPDATE"), {"ws": ws})
        ).one()
        return row.active_version_id  # type: ignore[no-any-return]

    async def _activate(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        actor: uuid.UUID,
        previous: uuid.UUID | None,
        version_id: uuid.UUID,
        reason: str,
        audit: AuditWriter,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO ontology_activations (id, workspace_id, version_id, previous_version_id, activated_by, reason_code) VALUES (:id, :ws, :v, :prev, :by, :reason)"
            ),
            {"id": uuid7(), "ws": ws, "v": version_id, "prev": previous, "by": actor, "reason": reason},
        )
        await conn.execute(
            text("UPDATE workspace_ontology SET active_version_id = :v WHERE workspace_id = :ws"), {"v": version_id, "ws": ws}
        )
        await audit.record(
            "ontology.activated", resource_type="ontology_version", resource_id=version_id, metadata=AuditMetadata(version_id=version_id)
        )

    async def publish(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        audit: AuditWriter,
        version_id: uuid.UUID,
        *,
        notes: str,
        activate: bool,
    ) -> OntologyVersionOut:
        ws = ctx.workspace_id
        previous = await self._lock_workspace(conn, ws)
        row = await self._row(conn, ws, version_id, lock=True)
        if row.state != "draft":
            metrics.ontology_publish_total.labels(outcome="not_a_draft").inc()
            raise ImmutableVersion()
        spec, report = validate_document(row.spec, self._compiler)
        if spec is None or not report.ok or report.content_hash is None:
            metrics.ontology_publish_total.labels(outcome="invalid").inc()
            errors: list[Issue] = report.errors
            raise OntologyInvalid(details={"errors": [e.model_dump() for e in errors][:50], "error_count": len(errors)})
        other_keys = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT ontology_key FROM ontology_versions WHERE workspace_id = :ws AND state IN ('published','retired')"
                    ),
                    {"ws": ws},
                )
            ).all()
        }
        if other_keys and other_keys != {spec.key}:
            metrics.ontology_publish_total.labels(outcome="key_mismatch").inc()
            raise Conflict("A workspace has one ontology; versions must share its key", code="ontology_key_mismatch")
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE ontology_versions SET state = 'published', spec = CAST(:spec AS jsonb), content_hash = :h, notes = :notes, published_by = :by, "
                        "published_at = now(), validation_report = CAST(:rep AS jsonb), revision = revision + 1 WHERE workspace_id = :ws AND id = :id"
                    ),
                    {
                        "spec": json.dumps(canonical_dict(spec)),
                        "h": report.content_hash,
                        "notes": notes,
                        "by": principal.principal_id,
                        "rep": report.model_dump_json(),
                        "ws": ws,
                        "id": version_id,
                    },
                )
        except IntegrityError as exc:
            metrics.ontology_publish_total.labels(outcome="conflict").inc()
            raise Conflict("That ontology version already exists", code="version_exists") from exc
        await audit.record(
            "ontology.published",
            resource_type="ontology_version",
            resource_id=version_id,
            metadata=AuditMetadata(version_id=version_id, content_hash=report.content_hash),
        )
        if activate:
            await self._activate(conn, ws, principal.principal_id, previous, version_id, "publish", audit)
        metrics.ontology_publish_total.labels(outcome="published").inc()
        return _full(await self._row(conn, ws, version_id), activated=activate)

    async def activate(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        audit: AuditWriter,
        version_id: uuid.UUID,
        reason_code: str,
    ) -> ActiveOntologyOut:
        """Rollback (ONT-02): an older published version becomes active again. Appends an activation; no version is mutated."""
        ws = ctx.workspace_id
        previous = await self._lock_workspace(conn, ws)
        row = await self._row(conn, ws, version_id, lock=True)
        if row.state != "published":
            raise Conflict("Only a published version can be activated", code="version_not_published")
        if previous == version_id:
            raise Conflict("That version is already active", code="already_active")
        await self._activate(conn, ws, principal.principal_id, previous, version_id, reason_code, audit)
        return await self.active(conn, ctx)

    async def retire(self, conn: AsyncConnection, ctx: WorkspaceContext, audit: AuditWriter, version_id: uuid.UUID) -> OntologyVersionOut:
        ws = ctx.workspace_id
        active = await self._lock_workspace(conn, ws)
        row = await self._row(conn, ws, version_id, lock=True)
        if row.state != "published":
            raise Conflict("Only a published version can be retired", code="version_not_published")
        if active == version_id:
            raise Conflict("The active version cannot be retired", code="version_active")
        await conn.execute(
            text("UPDATE ontology_versions SET state = 'retired', retired_at = now() WHERE workspace_id = :ws AND id = :id"),
            {"ws": ws, "id": version_id},
        )
        await audit.record(
            "ontology.retired", resource_type="ontology_version", resource_id=version_id, metadata=AuditMetadata(version_id=version_id)
        )
        return _full(await self._row(conn, ws, version_id))
