"""The `normalize` stage: decrypt the raw content, canonicalise it per kind, store the normalised form ENCRYPTED, record counts, advance the pipeline.

Content is DATA. Nothing in it can choose a stage, a classification, a policy or a role (AT-15, partial): this module never interprets text, it only
reshapes it. No model is called (P3 makes no model calls of its own).

Handler contract (ADR-025): tx 1 marks the stage running; the work is pure; tx 2 writes the result AND advances the pipeline; the dispatcher then acks.
A crash anywhere in between re-runs the job, and tx 2 finds the stage already terminal at this generation, so the result is written once."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.tokens import count_tokens
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_jobs import faults
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.policy import Decision
from kg_obs import metrics
from kg_obs.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_ingestion.crypto import ContentHasher, PayloadCipher, canonical_json
from kg_ingestion.envelope import SUPPORTED_DOCUMENT_TYPES
from kg_ingestion.pipeline import (
    DEAD,
    FAILED,
    INVALID,
    SKIPPED_DISABLED,
    SKIPPED_POLICY,
    SUCCEEDED,
    Orchestrator,
)
from kg_ingestion.store import insert_content, read_content

log = get_logger("kg.ingestion.normalize")
STAGE = "normalize"
TERMINAL_OK = frozenset({SUCCEEDED, INVALID, SKIPPED_POLICY, SKIPPED_DISABLED})
DERIVATIVE_NAMESPACE = uuid.UUID("0c6f1f6e-6b0b-4a52-8d1e-2f6a5b1c7d41")
_SPACES = re.compile(r"[ \t]+\n")
_BLANKS = re.compile(r"\n{3,}")


class NormalizeResult(BaseModel):
    """The closed shape of `interaction_stages.result` for this stage: counts only, never text."""

    model_config = ConfigDict(extra="forbid")
    form: str = "normalized"
    byte_size: int
    token_estimate: int
    turn_count: int | None = None


@dataclass(frozen=True)
class Canonical:
    text: str
    turn_count: int | None = None
    invalid_code: str | None = None


def normalized_content_id(interaction_id: uuid.UUID) -> uuid.UUID:
    """The stable derivative id of an interaction's normalised content (the provenance path, EVD-03)."""
    return uuid.uuid5(DERIVATIVE_NAMESPACE, f"{interaction_id}:normalized")


def _nl(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _instant(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonicalise(doc: dict[str, Any]) -> Canonical:
    """Per kind (phase-3 plan WP4): message/observation -> the text; conversation -> one `[at] speaker: text` line per turn; json -> canonical JSON;
    document -> text with normalised whitespace (an unsupported content type is `invalid`)."""
    kind, content = doc["kind"], doc["content"]
    if kind in ("message", "observation"):
        return Canonical(_nl(str(content)))
    if kind == "document":
        if doc.get("content_type") not in SUPPORTED_DOCUMENT_TYPES:
            return Canonical("", invalid_code="unsupported_content_type")
        body = _BLANKS.sub("\n\n", _SPACES.sub("\n", _nl(str(content)))).strip("\n")
        return Canonical(body.rstrip() + "\n" if body else "")
    if kind == "json":
        return Canonical(canonical_json(content).decode())
    if kind == "conversation":
        anonymous: dict[str, int] = {}
        lines: list[str] = []
        for turn in content:
            who = turn.get("speaker_label")
            if not who:
                ref = turn.get("speaker_ref") or ""
                who = f"Speaker {anonymous.setdefault(ref, len(anonymous) + 1)}"
            stamp = f"[{_instant(turn['at'])}] " if turn.get("at") else ""
            lines.append(f"{stamp}{_nl(who)}: {_nl(turn['text'])}")
        return Canonical("\n".join(lines), turn_count=len(lines))
    return Canonical("", invalid_code="unknown_kind")


async def lock_stage(conn: AsyncConnection, ws: uuid.UUID, interaction_id: uuid.UUID, stage: str) -> Row[Any] | None:
    return (
        await conn.execute(
            text(
                "SELECT stage, status, generation, attempts, last_error_code FROM interaction_stages "
                "WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s FOR UPDATE"
            ),
            {"ws": ws, "i": interaction_id, "s": stage},
        )
    ).first()


class NormalizeHandler:
    stage = STAGE

    def __init__(self, engine: AsyncEngine, cipher: PayloadCipher, hasher: ContentHasher, orchestrator: Orchestrator | None = None):
        self._engine, self._cipher, self._hasher = engine, cipher, hasher
        self._orchestrator = orchestrator or Orchestrator()

    # ---- the job ----------------------------------------------------------------------------------------------------------------------------

    async def handle(self, job: ClaimedJob) -> None:
        ws, iid, gen = job.workspace_id, job.subject_id, job.generation
        ctx = WorkspaceContext.for_system_task(ws, reason="normalize stage")
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None:
                raise JobError(Category.CONFIG_TERMINAL, "stage_missing")
            if st.generation > gen:
                return  # a replay superseded this job
            if st.generation < gen:
                raise JobError(Category.CONFIG_TERMINAL, "generation_mismatch")
            if st.status == DEAD:
                raise JobError(Category.CONFIG_TERMINAL, st.last_error_code or "stage_dead")
            if st.status in TERMINAL_OK:
                return  # already done at this generation: the ack was lost, the result stands
            sealed = await read_content(conn, ws, iid, "raw")
            if sealed is None:
                raise JobError(Category.CONFIG_TERMINAL, "raw_content_missing")
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = 'running', attempts = :n, started_at = COALESCE(started_at, now()), finished_at = NULL "
                    "WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {"n": job.attempts, "ws": ws, "i": iid, "s": STAGE},
            )
            moved = await conn.execute(
                text(
                    "UPDATE interactions SET processing_status = 'processing', status_changed_at = now() "
                    "WHERE workspace_id = :ws AND id = :i AND processing_status = 'accepted'"
                ),
                {"ws": ws, "i": iid},
            )
            if moved.rowcount:
                metrics.interactions_status_total.labels(status="processing").inc()

        plaintext = self._cipher.decrypt(sealed, workspace_id=ws, interaction_id=iid, form="raw")
        doc = json.loads(plaintext)
        canon = await asyncio.to_thread(canonicalise, doc)
        tokens = await asyncio.to_thread(count_tokens, canon.text) if not canon.invalid_code else 0
        faults.current().hit(STAGE, "after_side_effect")

        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None or st.generation != gen or st.status in TERMINAL_OK or st.status == DEAD:
                return  # another run already finished this generation (lease lost and reclaimed): write nothing twice
            if canon.invalid_code:
                await conn.execute(
                    text(
                        "UPDATE interaction_stages SET status = 'invalid', finished_at = now(), last_error_category = 'config_terminal', last_error_code = :code, "
                        "result = CAST(:r AS jsonb) WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                    ),
                    {"code": canon.invalid_code, "r": "{}", "ws": ws, "i": iid, "s": STAGE},
                )
            else:
                data = canon.text.encode()
                sealed_norm = self._cipher.encrypt(data, workspace_id=ws, interaction_id=iid, form="normalized")
                await insert_content(conn, ws, iid, "normalized", sealed_norm, self._hasher.digest(ws, data), len(data))
                result = NormalizeResult(byte_size=len(data), token_estimate=tokens, turn_count=canon.turn_count)
                await conn.execute(
                    text(
                        "UPDATE interaction_stages SET status = 'succeeded', finished_at = now(), last_error_category = NULL, last_error_code = NULL, "
                        "result = CAST(:r AS jsonb) WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                    ),
                    {"r": result.model_dump_json(exclude_none=True), "ws": ws, "i": iid, "s": STAGE},
                )
            await self._orchestrator.advance(conn, ws, iid)

    # ---- failure bookkeeping (called by the dispatcher before the ack) ------------------------------------------------------------------------

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        ws, iid = job.workspace_id, job.subject_id
        ctx = WorkspaceContext.for_system_task(ws, reason="normalize stage failure")
        status = {"retry": FAILED, "dead": DEAD, "skipped": SKIPPED_POLICY}.get(decision.outcome)
        if status is None:  # budget_pause: waiting for budget is not a stage failure
            return
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None or st.generation != job.generation or st.status in TERMINAL_OK:
                return
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = :status, attempts = :n, last_error_category = :cat, last_error_code = :code, "
                    "finished_at = CASE WHEN :final THEN now() END WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {
                    "status": status,
                    "n": job.attempts,
                    "cat": decision.category.value if decision.category else None,
                    "code": decision.code,
                    "final": decision.outcome != "retry",
                    "ws": ws,
                    "i": iid,
                    "s": STAGE,
                },
            )
            if decision.outcome != "retry":
                await self._orchestrator.advance(conn, ws, iid)
        log.info("stage_failed", stage=STAGE, outcome=decision.outcome, error_code=decision.code, attempt=job.attempts)


__all__ = ["Canonical", "NormalizeHandler", "NormalizeResult", "canonicalise", "normalized_content_id"]
