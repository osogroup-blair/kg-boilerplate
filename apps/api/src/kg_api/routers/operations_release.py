"""Operations v2 (phase-9 plan WP9): reconciliation here; cost, latency, freshness, backups and live-evaluation views are added below. Everything is a server-side aggregate from
Postgres (the console needs no Prometheus access) and carries ids, counts and enums only."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Query
from kg_common.errors import ValidationFailed
from kg_db.session import system_session
from kg_domain.principal import WorkspaceContext
from kg_governance.authz.policy import Action, enforce, requires
from kg_reconcile import CLASSES
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key

router = APIRouter(prefix="/v1/operations", tags=["operations"])


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all", "graph", "memory", "ledger", "deletions"] = "all"
    repair: bool = False
    allow_reingest: bool = False


class ReconcileClassOut(BaseModel):
    name: str
    scope: str
    description: str
    repair: str
    spends_money: bool


class ReconcileRunOut(BaseModel):
    id: uuid.UUID
    scope: str
    mode: str
    trigger: str
    status: str
    drift: dict[str, int]
    repaired: dict[str, int]
    error_code: str | None = None
    started_at: Any
    finished_at: Any = None


class ReconcileOverview(BaseModel):
    classes: list[ReconcileClassOut]
    runs: list[ReconcileRunOut]


@router.get("/reconcile", response_model=ReconcileOverview)
@requires(Action.OPERATIONS_READ)
async def reconcile_overview(ws: WorkspaceDep, db: DbDep, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> ReconcileOverview:
    rows = (
        await db.execute(
            text(
                "SELECT id, scope, mode, trigger, status, drift, repaired, error_code, started_at, finished_at FROM reconcile_runs "
                "WHERE workspace_id = :ws ORDER BY started_at DESC LIMIT :n"
            ),
            {"ws": ws.workspace_id, "n": limit},
        )
    ).all()
    return ReconcileOverview(
        classes=[
            ReconcileClassOut(name=c.name, scope=c.scope, description=c.description, repair=c.repair, spends_money=c.spends_money)
            for c in CLASSES.values()
        ],
        runs=[ReconcileRunOut(**dict(r._mapping)) for r in rows],
    )


@router.post("/reconcile")
@requires(Action.OPERATIONS_READ)
async def reconcile_run(
    body: ReconcileIn, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKeyHeader = None
) -> dict[str, object]:
    """A scan (any operator) or, with `repair`, a scan plus the SAFE repairs (operations.repair: admins and operators). A repair that would call a model needs `allow_reingest`
    and answers with the cost preview first (`needs_confirmation`)."""
    if body.allow_reingest and not body.repair:
        raise ValidationFailed("allow_reingest only applies with repair", details={"fields": ["allow_reingest"]})
    if body.repair:
        enforce(principal, Action.OPERATIONS_REPAIR)
        require_key(idempotency_key)
    ctx = WorkspaceContext.of(principal)
    result = await state.reconcile.run(
        ctx, scope=body.scope, repair=body.repair, allow_reingest=body.allow_reingest, trigger="manual", started_by=principal.principal_id
    )
    return result.as_dict()


# ---- cost, latency, freshness, backups, live evaluation, alerts (read-only aggregates) ----------------------------------------------------------------------------------------
Days = Annotated[int, Query(ge=1, le=90, description="window in days")]


def _f(v: Any, digits: int = 3) -> float:
    return round(float(v), digits) if v is not None else 0.0


class CostRow(BaseModel):
    day: str
    purpose: str
    alias: str
    calls: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    usd: float


class Consumer(BaseModel):
    principal_id: uuid.UUID
    handle: str
    kind: str
    context_builds: int
    tokens_used: int


class CostOut(BaseModel):
    days: int
    total_usd: float
    total_calls: int
    by_purpose: dict[str, float]
    by_alias: dict[str, float]
    rows: list[CostRow]
    top_consumers: list[Consumer]
    note: str


@router.get("/cost", response_model=CostOut)
@requires(Action.OPERATIONS_READ)
async def operations_cost(ws: WorkspaceDep, db: DbDep, days: Days = 14) -> CostOut:
    """Model spend from the call log's ESTIMATES (the router's invoice is the truth), by day, purpose and alias. Top consumers are the principals that built the most context in the
    window: background model calls (extraction, consolidation) belong to no principal and show only in the totals."""
    p = {"ws": ws.workspace_id, "d": days}
    rows = (
        await db.execute(
            text(
                "SELECT created_at::date AS day, purpose, alias, count(*) AS calls, count(*) FILTER (WHERE outcome <> 'ok') AS errors, "
                "COALESCE(sum(prompt_tokens), 0) AS pt, COALESCE(sum(completion_tokens), 0) AS ct, COALESCE(sum(est_cost_usd), 0) AS usd "
                "FROM model_calls WHERE workspace_id = :ws AND created_at >= now() - make_interval(days => :d) GROUP BY 1, 2, 3 ORDER BY 1 DESC, 8 DESC"
            ),
            p,
        )
    ).all()
    top = (
        await db.execute(
            text(
                "SELECT t.principal_id, COALESCE(pr.handle, '(deleted)') AS handle, COALESCE(pr.kind, 'unknown') AS kind, count(*) AS builds, COALESCE(sum(t.used_tokens), 0) AS tokens "
                "FROM context_traces t LEFT JOIN principals pr ON pr.id = t.principal_id AND pr.workspace_id = t.workspace_id "
                "WHERE t.workspace_id = :ws AND t.created_at >= now() - make_interval(days => :d) GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 10"
            ),
            p,
        )
    ).all()
    by_purpose: dict[str, float] = {}
    by_alias: dict[str, float] = {}
    for r in rows:
        by_purpose[r.purpose] = round(by_purpose.get(r.purpose, 0.0) + float(r.usd), 6)
        by_alias[r.alias] = round(by_alias.get(r.alias, 0.0) + float(r.usd), 6)
    return CostOut(
        days=days,
        total_usd=round(sum(by_purpose.values()), 6),
        total_calls=sum(int(r.calls) for r in rows),
        by_purpose=by_purpose,
        by_alias=by_alias,
        rows=[
            CostRow(
                day=str(r.day),
                purpose=r.purpose,
                alias=r.alias,
                calls=int(r.calls),
                errors=int(r.errors),
                prompt_tokens=int(r.pt),
                completion_tokens=int(r.ct),
                usd=round(float(r.usd), 6),
            )
            for r in rows
        ],
        top_consumers=[
            Consumer(principal_id=r.principal_id, handle=r.handle, kind=r.kind, context_builds=int(r.builds), tokens_used=int(r.tokens))
            for r in top
        ],
        note="estimates from the platform's call log; the router's invoice is authoritative",
    )


class Quantiles(BaseModel):
    n: int
    p50: float
    p95: float


class LatencyOut(BaseModel):
    days: int
    context_by_profile: dict[str, Quantiles]
    context_by_source: dict[str, Quantiles]
    router_by_alias: dict[str, Quantiles]
    unit: str = "seconds"


@router.get("/latency", response_model=LatencyOut)
@requires(Action.OPERATIONS_READ)
async def operations_latency(ws: WorkspaceDep, db: DbDep, days: Days = 1) -> LatencyOut:
    """Context build time by profile and by source (from the traces), and the router's response time by alias (from the call log). Seconds."""
    p = {"ws": ws.workspace_id, "d": days}
    profiles = (
        await db.execute(
            text(
                "SELECT profile, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY (latencies->>'total')::float8) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY (latencies->>'total')::float8) AS p95 "
                "FROM context_traces WHERE workspace_id = :ws AND created_at >= now() - make_interval(days => :d) AND latencies ? 'total' GROUP BY 1"
            ),
            p,
        )
    ).all()
    sources = (
        await db.execute(
            text(
                "SELECT s.key AS source, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY s.value::float8) AS p50, percentile_cont(0.95) WITHIN GROUP (ORDER BY s.value::float8) AS p95 "
                "FROM context_traces t, jsonb_each_text(t.latencies->'per_source') s WHERE t.workspace_id = :ws AND t.created_at >= now() - make_interval(days => :d) "
                "AND jsonb_typeof(t.latencies->'per_source') = 'object' GROUP BY 1"
            ),
            p,
        )
    ).all()
    aliases = (
        await db.execute(
            text(
                "SELECT alias, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) / 1000.0 AS p50, percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) / 1000.0 AS p95 "
                "FROM model_calls WHERE workspace_id = :ws AND created_at >= now() - make_interval(days => :d) AND latency_ms IS NOT NULL GROUP BY 1"
            ),
            p,
        )
    ).all()

    def q(rows: Any) -> dict[str, Quantiles]:
        return {r[0]: Quantiles(n=int(r.n), p50=_f(r.p50), p95=_f(r.p95)) for r in rows}

    return LatencyOut(days=days, context_by_profile=q(profiles), context_by_source=q(sources), router_by_alias=q(aliases))


class StageLag(BaseModel):
    stage: str
    n: int
    p50: float
    p95: float


class FreshnessOut(BaseModel):
    hours: int
    accepted_to_indexed: Quantiles
    unsettled: dict[str, int]
    oldest_unsettled_age_seconds: float
    by_stage: list[StageLag]
    watermarks: dict[str, Any]
    unit: str = "seconds"


@router.get("/freshness", response_model=FreshnessOut)
@requires(Action.OPERATIONS_READ)
async def operations_freshness(ws: WorkspaceDep, db: DbDep, hours: Annotated[int, Query(ge=1, le=720)] = 24) -> FreshnessOut:
    """How far behind the pipeline is: accepted -> indexed lag, the lag per stage (queue wait + run), what has not settled yet, and the watermarks."""
    p = {"ws": ws.workspace_id, "h": hours}
    lag = (
        await db.execute(
            text(
                "SELECT count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM status_changed_at - recorded_at)) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM status_changed_at - recorded_at)) AS p95 FROM interactions "
                "WHERE workspace_id = :ws AND processing_status IN ('indexed', 'partial') AND recorded_at >= now() - make_interval(hours => :h)"
            ),
            p,
        )
    ).one()
    unsettled = (
        await db.execute(
            text(
                "SELECT processing_status AS s, count(*) AS n, extract(epoch FROM now() - min(recorded_at)) AS oldest FROM interactions "
                "WHERE workspace_id = :ws AND processing_status IN ('accepted', 'processing') GROUP BY 1"
            ),
            {"ws": ws.workspace_id},
        )
    ).all()
    stages = (
        await db.execute(
            text(
                "SELECT stage, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - created_at)) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - created_at)) AS p95 FROM outbox_jobs "
                "WHERE workspace_id = :ws AND status = 'succeeded' AND finished_at >= now() - make_interval(hours => :h) GROUP BY 1 ORDER BY 1"
            ),
            p,
        )
    ).all()
    marks = (
        await db.execute(
            text(
                "SELECT (SELECT max(recorded_at) FROM interactions WHERE workspace_id = :ws) AS newest_event, "
                "(SELECT max(status_changed_at) FROM interactions WHERE workspace_id = :ws AND processing_status IN ('indexed', 'partial')) AS newest_indexed, "
                "(SELECT max(finished_at) FROM outbox_jobs WHERE workspace_id = :ws AND status = 'succeeded') AS last_job"
            ),
            {"ws": ws.workspace_id},
        )
    ).one()
    return FreshnessOut(
        hours=hours,
        accepted_to_indexed=Quantiles(n=int(lag.n), p50=_f(lag.p50, 2), p95=_f(lag.p95, 2)),
        unsettled={r.s: int(r.n) for r in unsettled},
        oldest_unsettled_age_seconds=_f(max((float(r.oldest) for r in unsettled), default=0.0), 1),
        by_stage=[StageLag(stage=r.stage, n=int(r.n), p50=_f(r.p50, 2), p95=_f(r.p95, 2)) for r in stages],
        watermarks={k: (v.isoformat() if v is not None else None) for k, v in marks._mapping.items()},
    )


class BackupRow(BaseModel):
    id: uuid.UUID
    kind: str
    store: str
    status: str
    started_at: Any
    completed_at: Any = None
    size_bytes: int | None = None
    error_code: str | None = None
    expires_at: Any = None


class StoreAge(BaseModel):
    store: str
    age_seconds: float | None
    stale: bool


class RehearsalOut(BaseModel):
    at: Any = None
    status: str | None = None
    rto_seconds: float | None = None
    rpo_seconds: float | None = None
    age_days: float | None = None


class BackupsOut(BaseModel):
    stores: list[StoreAge]
    last_rehearsal: RehearsalOut
    recent: list[BackupRow]
    stale_after_hours: int = 26


@router.get("/backups", response_model=BackupsOut)
@requires(Action.OPERATIONS_READ)
async def operations_backups(state: StateDep, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> BackupsOut:
    """Platform-wide (backups are of the whole database): the age of the newest completed backup per store, recent runs, and the last restore rehearsal with its measured RTO/RPO.
    Sizes, statuses and error codes only: no locations, no keys, no content."""
    async with system_session(state.engine) as conn:
        ages = (await conn.execute(text("SELECT * FROM ops.backup_ages()"))).all()
        recent = (await conn.execute(text("SELECT * FROM ops.backup_status(:n)"), {"n": limit})).all()
    rehearsal = next((r for r in recent if r.kind == "restore_rehearsal"), None)
    if rehearsal is None:
        async with system_session(state.engine) as conn:
            rehearsal = next(
                (r for r in (await conn.execute(text("SELECT * FROM ops.backup_status(500)"))).all() if r.kind == "restore_rehearsal"), None
            )
    d = (rehearsal.details or {}) if rehearsal is not None else {}
    when = rehearsal.completed_at or rehearsal.started_at if rehearsal is not None else None
    return BackupsOut(
        stores=[
            StoreAge(
                store=r.store,
                age_seconds=None if r.age_seconds is None else round(float(r.age_seconds)),
                stale=r.age_seconds is None or float(r.age_seconds) > 26 * 3600,
            )
            for r in ages
        ],
        last_rehearsal=RehearsalOut(
            at=when,
            status=rehearsal.status if rehearsal is not None else None,
            rto_seconds=d.get("rto_seconds"),
            rpo_seconds=d.get("rpo_seconds"),
            age_days=round((dt.datetime.now(dt.UTC) - when).total_seconds() / 86400, 1) if when is not None else None,
        ),
        recent=[
            BackupRow(
                id=r.id,
                kind=r.kind,
                store=r.store,
                status=r.status,
                started_at=r.started_at,
                completed_at=r.completed_at,
                size_bytes=r.size_bytes,
                error_code=r.error_code,
                expires_at=r.expires_at,
            )
            for r in recent
            if r.kind != "restore_rehearsal"
        ],
    )


class EvalRow(BaseModel):
    id: uuid.UUID
    kind: str
    label: str | None = None
    status: str
    started_at: Any
    finished_at: Any = None
    git_commit: str | None = None
    summary: dict[str, Any]


class LiveEvalOut(BaseModel):
    last_live_session: EvalRow | None
    latest_by_kind: list[EvalRow]
    recent: list[EvalRow]


@router.get("/live-eval", response_model=LiveEvalOut)
@requires(Action.OPERATIONS_READ)
async def operations_live_eval(state: StateDep, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> LiveEvalOut:
    """When the live session last ran, the newest record of every kind of evaluation (eval gate, live gate, release sweep, resilience, baseline, scan) and per-suite status/drift
    from each record's summary. Summaries hold counts, thresholds and verdicts."""
    async with system_session(state.engine) as conn:
        rows = (await conn.execute(text("SELECT * FROM ops.eval_status(500)"))).all()

    def row(r: Any) -> EvalRow:
        return EvalRow(
            id=r.id,
            kind=r.kind,
            label=r.label,
            status=r.status,
            started_at=r.started_at,
            finished_at=r.finished_at,
            git_commit=r.git_commit,
            summary=r.summary or {},
        )

    latest: dict[str, Any] = {}
    for r in rows:  # newest first
        latest.setdefault(r.kind, r)
    return LiveEvalOut(
        last_live_session=row(latest["live_session"]) if "live_session" in latest else None,
        latest_by_kind=[row(r) for r in latest.values()],
        recent=[row(r) for r in rows[:limit]],
    )


class AlertOut(BaseModel):
    name: str
    severity: Literal["critical", "warning", "info"]
    source: Literal["platform", "alertmanager"]
    summary: str
    value: float | None = None


class AlertsOut(BaseModel):
    firing: list[AlertOut]
    checked: list[str]
    alertmanager: Literal["not_configured", "ok", "unreachable"]


@router.get("/alerts", response_model=AlertsOut)
@requires(Action.OPERATIONS_READ)
async def operations_alerts(state: StateDep, ws: WorkspaceDep, db: DbDep) -> AlertsOut:
    """What is wrong right now, from the platform's own checks on Postgres state (dead letters, backup age, drift, oldest pending job, a stale rehearsal) plus, when
    ALERTMANAGER_URL is set, whatever that Alertmanager is firing. The checks mirror deploy/observability/rules.yml so the console needs no Prometheus access."""
    firing: list[AlertOut] = []
    checked = ["dead_letters", "oldest_pending_job", "reconcile_drift", "backup_age", "restore_rehearsal_age"]
    dead = (
        await db.execute(text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND status = 'dead'"), {"ws": ws.workspace_id})
    ).scalar_one()
    if dead:
        firing.append(
            AlertOut(
                name="DeadLetters",
                severity="warning",
                source="platform",
                summary=f"{dead} job(s) are dead-lettered in this workspace",
                value=float(dead),
            )
        )
    oldest = (
        await db.execute(
            text(
                "SELECT COALESCE(extract(epoch FROM now() - min(available_at)), 0) FROM outbox_jobs WHERE workspace_id = :ws AND status = 'pending' AND available_at < now()"
            ),
            {"ws": ws.workspace_id},
        )
    ).scalar_one()
    if float(oldest) > 900:
        firing.append(
            AlertOut(
                name="OldestPendingJob",
                severity="warning",
                source="platform",
                summary="a claimable job has waited more than 15 minutes",
                value=float(oldest),
            )
        )
    last = (
        await db.execute(
            text("SELECT drift FROM reconcile_runs WHERE workspace_id = :ws AND status = 'completed' ORDER BY started_at DESC LIMIT 1"),
            {"ws": ws.workspace_id},
        )
    ).first()
    drift = sum(int(v) for v in ((last.drift or {}).values() if last is not None else []))
    if int(drift):
        firing.append(
            AlertOut(
                name="ReconcileDrift",
                severity="warning",
                source="platform",
                summary="the latest reconciliation scan found drift",
                value=float(drift),
            )
        )
    async with system_session(state.engine) as conn:
        ages = (await conn.execute(text("SELECT * FROM ops.backup_ages()"))).all()
    for r in ages:
        if r.age_seconds is None or float(r.age_seconds) > 26 * 3600:
            firing.append(
                AlertOut(
                    name="BackupStale",
                    severity="critical",
                    source="platform",
                    summary=f"no completed {r.store} backup in the last 26 hours"
                    if r.age_seconds is not None
                    else f"no completed {r.store} backup has ever been recorded",
                    value=None if r.age_seconds is None else float(r.age_seconds),
                )
            )
    async with system_session(state.engine) as conn:
        rehearsals = [r for r in (await conn.execute(text("SELECT * FROM ops.backup_status(500)"))).all() if r.kind == "restore_rehearsal"]
    last_ok = next((r for r in rehearsals if r.status == "completed"), None)
    if last_ok is None or (dt.datetime.now(dt.UTC) - (last_ok.completed_at or last_ok.started_at)).days > 8:
        firing.append(
            AlertOut(
                name="RestoreRehearsalStale",
                severity="warning",
                source="platform",
                summary="no passing restore rehearsal in the last 8 days"
                if last_ok is not None
                else "no passing restore rehearsal has been recorded",
            )
        )
    amgr: Literal["not_configured", "ok", "unreachable"] = "not_configured"
    url = state.settings.alertmanager_url
    if url:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{url.rstrip('/')}/api/v2/alerts", params={"active": "true", "silenced": "false", "inhibited": "false"}
                )
                resp.raise_for_status()
                for a in resp.json()[:50]:
                    labels, ann = a.get("labels", {}), a.get("annotations", {})
                    sev = labels.get("severity", "warning")
                    firing.append(
                        AlertOut(
                            name=str(labels.get("alertname", "alert"))[:80],
                            severity=sev if sev in ("critical", "warning", "info") else "warning",
                            source="alertmanager",
                            summary=str(ann.get("summary", ""))[:300],
                        )
                    )
            amgr = "ok"
        except (httpx.HTTPError, ValueError):
            amgr = "unreachable"
    return AlertsOut(firing=firing, checked=checked, alertmanager=amgr)
