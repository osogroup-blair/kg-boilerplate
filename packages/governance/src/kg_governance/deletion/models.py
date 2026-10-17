"""Request and response models of the deletion API (phase-8 plan §6.2). A response carries ids, states, counts and codes: never a name, never a residual."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DeletionKind = Literal["subject", "interaction", "memory", "principal_data", "workspace", "retention"]
DeletionStatus = Literal[
    "received",
    "frozen",
    "planned",
    "purging",
    "verifying",
    "completed",
    "completed_with_exceptions",
    "blocked_legal_hold",
    "failed",
    "cancelled",
]
TERMINAL = ("completed", "completed_with_exceptions", "cancelled")
Store = Literal["postgres", "vector", "graph", "trace", "job", "idempotency"]


class DeletionRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["subject", "interaction", "memory", "principal_data", "workspace"]
    target_id: uuid.UUID | None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    legal_basis: str | None = Field(default=None, max_length=200)
    include_submissions: bool = False  # principal_data: also the interactions the principal submitted, and what only they support
    on_behalf_of: str | None = Field(
        default=None, max_length=255
    )  # trusted-source apps: the end subject's own identifier, kept only as an HMAC
    confirm_slug: str | None = Field(default=None, max_length=100)  # workspace: the slug, typed

    @model_validator(mode="after")
    def _shape(self) -> DeletionRequestIn:
        if self.kind == "workspace":
            if self.target_id is not None or not self.confirm_slug:
                raise ValueError("a workspace deletion names no target and needs confirm_slug")
        elif self.target_id is None:
            raise ValueError("this kind of deletion needs target_id")
        return self


class StoreCounts(BaseModel):
    deleted: int = 0
    redacted: int = 0
    retained: int = 0


class RetainedClass(BaseModel):
    cls: str = Field(alias="class")
    reason: str
    count: int
    model_config = ConfigDict(populate_by_name=True)


class Receipt(BaseModel):
    request_id: uuid.UUID
    kind: DeletionKind
    status: str
    completed_at: dt.datetime
    stores: dict[str, StoreCounts]
    by_table: dict[str, dict[str, int]] = {}
    graph: dict[str, int] = {}
    retained: list[RetainedClass] = []
    verification: dict[str, Any] = {}
    legal_hold: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    backup_copies_expire_by: dt.date
    journal_ref: str | None = None


class DeletionOut(BaseModel):
    request_id: uuid.UUID
    kind: DeletionKind
    status: DeletionStatus
    target_id: uuid.UUID | None = None
    requested_by: uuid.UUID | None = None
    reason_code: str
    include_submissions: bool = False
    received_at: dt.datetime
    frozen_at: dt.datetime | None = None
    planned_at: dt.datetime | None = None
    purge_started_at: dt.datetime | None = None
    verified_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    cancelled_at: dt.datetime | None = None
    cooling_off_until: dt.datetime | None = None
    error_code: str | None = None
    item_counts: dict[str, int] = {}
    open_exceptions: int = 0
    receipt: dict[str, Any] | None = None
    revision: int = 1
    status_url: str


class LegalHoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_kind: Literal["subject", "interaction", "workspace"]
    target_id: uuid.UUID | None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _shape(self) -> LegalHoldIn:
        if (self.scope_kind == "workspace") != (self.target_id is None):
            raise ValueError("a workspace hold names no target; any other hold needs one")
        return self


class LegalHoldOut(BaseModel):
    id: uuid.UUID
    scope_kind: str
    target_id: uuid.UUID | None
    reason_code: str
    note: str
    created_by: uuid.UUID
    created_at: dt.datetime
    released_by: uuid.UUID | None = None
    released_at: dt.datetime | None = None


class SuppressionOut(BaseModel):
    kind: str
    value_hmac_prefix: str  # the first 8 hex characters: enough to tell two apart, not enough to use
    request_id: uuid.UUID
    created_at: dt.datetime
    lifted_at: dt.datetime | None = None
    lifted_by: uuid.UUID | None = None
