"""Request and response models of the correction API (phase-8 plan §5.1, §5.2, §6.2).

A correction names a REASON CODE from a closed set (it goes to the audit trail) and may carry reason text (it stays on the correction row, readable by reviewers and
admins, and is never copied to audit, logs or metrics)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kg_knowledge.models import EntityRefIn, ExternalId, Instant

CorrectionReason = Literal[
    "incorrect_extraction",
    "source_error",
    "outdated_information",
    "wrong_identity",
    "duplicate_report",
    "privacy_request",
    "policy_exception",
    "other",
]
FactCorrectionKind = Literal["reject", "supersede", "end", "restore"]
EntityCorrectionKind = Literal["correct_attribute", "rename", "split"]
MemoryCorrectionKind = Literal["supersede", "reject", "expire", "dispute"]
CorrectionKind = Literal[
    "fact.reject",
    "fact.supersede",
    "fact.end",
    "fact.restore",
    "entity.correct_attribute",
    "entity.rename",
    "entity.split",
    "memory.supersede",
    "memory.reject",
    "memory.expire",
    "memory.dispute",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Reasoned(_In):
    reason_code: CorrectionReason
    reason_text: str | None = Field(default=None, max_length=1000)


class FactCorrectionIn(_Reasoned):
    kind: FactCorrectionKind
    # supersede: the replacement. Anything left out is taken from the fact being superseded.
    source: EntityRefIn | None = None
    target: EntityRefIn | None = None
    attributes: dict[str, Any] | None = None
    valid_from: Instant | None = None
    valid_to: Instant | None = None  # supersede (the replacement's end) and end (when the fact stopped being true)
    # reject: facts this one ended by succession become current again
    reopen_predecessors: bool = True

    @model_validator(mode="after")
    def _shape(self) -> FactCorrectionIn:
        if self.kind == "end" and self.valid_to is None:
            raise ValueError("end needs valid_to")
        if self.kind in ("reject", "restore", "end") and (
            self.source is not None or self.target is not None or self.attributes is not None or self.valid_from is not None
        ):
            raise ValueError("only supersede takes a replacement")
        return self


class NewEntityIn(_In):
    display_name: str = Field(min_length=1, max_length=500)
    type: str | None = Field(default=None, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=50)


class SplitMoveIn(_In):
    mention_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    fact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    memory_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=500
    )  # memories ABOUT the split-off person: their subject (and subject scope) follows
    external_ids: list[ExternalId] = Field(default_factory=list, max_length=20)


class EntityCorrectionIn(_Reasoned):
    kind: EntityCorrectionKind
    attribute: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")  # correct_attribute
    value: Any = None  # correct_attribute; null removes the attribute
    name: str | None = Field(default=None, min_length=1, max_length=500)  # rename
    new_entity: NewEntityIn | None = None  # split
    move: SplitMoveIn | None = None  # split
    reextract: bool = False  # split: replay the extraction of the interactions whose mentions moved

    @model_validator(mode="after")
    def _shape(self) -> EntityCorrectionIn:
        if self.kind == "correct_attribute" and self.attribute is None:
            raise ValueError("correct_attribute needs attribute")
        if self.kind == "rename" and self.name is None:
            raise ValueError("rename needs name")
        if self.kind == "split" and (self.new_entity is None or self.move is None):
            raise ValueError("split needs new_entity and move")
        return self


class MemoryCorrectionIn(_Reasoned):
    kind: MemoryCorrectionKind
    content: str | None = Field(default=None, min_length=1, max_length=10_000)  # supersede
    summary: str | None = Field(default=None, min_length=1, max_length=280)

    @model_validator(mode="after")
    def _shape(self) -> MemoryCorrectionIn:
        if self.kind == "supersede" and self.content is None:
            raise ValueError("supersede needs content")
        if self.kind != "supersede" and self.content is not None:
            raise ValueError("only supersede takes content")
        return self


class DependentMemory(BaseModel):
    id: uuid.UUID
    type: str
    status: str


class CorrectionOut(BaseModel):
    correction_id: uuid.UUID
    command_id: uuid.UUID
    kind: CorrectionKind
    target_kind: Literal["entity", "fact", "memory"]
    target_id: uuid.UUID
    new_target_id: uuid.UUID | None = None  # the replacement fact, the split-off entity, the replacement memory
    revision: int
    projection_status: Literal["pending", "projected", "failed", "skipped_by_policy", "not_applicable"] = "pending"
    dependent_memory_ids: list[uuid.UUID] = []
    review_item_ids: list[uuid.UUID] = []
    reopened_fact_ids: list[uuid.UUID] = []
    authoritative_override: bool = False
    grant_holders: list[uuid.UUID] = []  # split: principals that hold a grant on the original entity (the new one has none)
    moved: dict[str, int] = {}
    status_url: str


class PreviewOut(BaseModel):
    """What a correction would touch, read-only. Counts and ids the caller may see; contexts are counted for admins only."""

    kind: CorrectionKind
    target_kind: Literal["entity", "fact", "memory"]
    target_id: uuid.UUID
    authoritative: bool = False
    dependent_memories: list[DependentMemory] = []
    dependent_memory_counts: dict[str, int] = {}
    sibling_fact_ids: list[uuid.UUID] = []  # facts supported by the same episodes
    predecessor_fact_ids: list[uuid.UUID] = []  # facts this one ended by succession (a reject can reopen them)
    open_review_ids: list[uuid.UUID] = []
    recent_context_count: int | None = None
    grant_holders: list[uuid.UUID] = []
    mention_count: int | None = None
    graph_operations: list[str] = []


class CorrectionRecord(BaseModel):
    id: uuid.UUID
    command_id: uuid.UUID
    target_kind: Literal["entity", "fact", "memory"]
    target_id: uuid.UUID
    kind: str
    reason_code: str
    reason_text: str | None = None  # reviewers and admins only
    new_target_id: uuid.UUID | None = None
    created_by: uuid.UUID
    created_at: dt.datetime
