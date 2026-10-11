"""The event envelope for POST /v1/interactions (phase-3 plan §5.1). Validation reports field PATHS only; values are never echoed (they are content)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, field_validator

Kind = Literal["message", "conversation", "observation", "document", "json"]
Classification = Literal["public", "internal", "confidential", "restricted", "local_only"]
CLASSIFICATION_ORDER = ("public", "internal", "confidential", "restricted", "local_only")
DEFAULT_CLASSIFICATION = "internal"  # the workspace default in P3 (no per-workspace setting yet); a request may only raise it
SOURCE_SYSTEM = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}$")
SUPPORTED_DOCUMENT_TYPES = frozenset({"text/plain", "text/markdown"})
MAX_TURNS = 500
MAX_JSON_DEPTH = 32
MAX_SUBJECTS = 20
CLOCK_SKEW = dt.timedelta(minutes=5)


def _instant(value: Any) -> Any:
    """ISO 8601 date or datetime; a naive value is taken as UTC (P2 convention)."""
    if value is None or isinstance(value, dt.datetime) and value.tzinfo:
        return value
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("not a valid date or datetime") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    raise ValueError("not a valid date or datetime")


Instant = Annotated[dt.datetime, BeforeValidator(_instant)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityRef(_In):
    entity_id: uuid.UUID


class ExternalRef(_In):
    """A strong external identifier, resolved at acceptance when possible and otherwise kept UNRESOLVED for P4 identity resolution."""

    type: str = Field(min_length=1, max_length=64)
    system: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255)


SubjectRef = EntityRef | ExternalRef


class Turn(_In):
    speaker_ref: str | None = Field(default=None, max_length=128)
    speaker_label: str | None = Field(default=None, max_length=128)
    text: str = Field(min_length=1, max_length=262_144)
    at: Instant | None = None


class Consent(_In):
    external_processing: Literal["allowed", "denied"] = "allowed"


def _depth(value: Any, level: int = 1) -> int:
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level])
    return level


class InteractionIn(_In):
    """Validation is per FIELD (not a whole-model check) so every error names its path; `details.fields` never carries a value."""

    source_system: str = Field(pattern=SOURCE_SYSTEM.pattern)
    source_ref: str = Field(min_length=1, max_length=256)
    kind: Kind
    payload_ref: str | None = Field(default=None, max_length=512)  # reserved seam; the service answers 422 pointer_not_supported
    content_type: str | None = Field(default=None, max_length=127, validate_default=True)
    content: Any = Field(default=None, validate_default=True)
    occurred_at_unknown: bool = False
    occurred_at: Instant | None = Field(default=None, validate_default=True)
    subject_refs: list[SubjectRef] = Field(default_factory=list, max_length=MAX_SUBJECTS)
    actor_ref: SubjectRef | None = None
    sensitivity: Classification | None = None
    consent: Consent | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("content_type")
    @classmethod
    def _content_type(cls, v: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("payload_ref") is not None:
            return v
        if v is not None and not CONTENT_TYPE.match(v):
            raise ValueError("not a media type")
        if info.data.get("kind") == "document" and v is None:
            raise ValueError("required for a document")
        return v

    @field_validator("content")
    @classmethod
    def _content(cls, v: Any, info: ValidationInfo) -> Any:
        kind = info.data.get("kind")
        if kind is None or info.data.get("payload_ref") is not None:
            return v
        if kind in ("message", "observation", "document"):
            if not isinstance(v, str) or not v.strip():
                raise ValueError("must be a non-empty string")
        elif kind == "conversation":
            if not isinstance(v, list) or not v or len(v) > MAX_TURNS:
                raise ValueError(f"must be a list of 1 to {MAX_TURNS} turns")
            return [Turn.model_validate(t).model_dump(mode="json", exclude_none=True) for t in v]
        elif kind == "json":
            if not isinstance(v, dict):
                raise ValueError("must be a JSON object")
            if _depth(v) > MAX_JSON_DEPTH:
                raise ValueError(f"nested deeper than {MAX_JSON_DEPTH} levels")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _occurred(cls, v: dt.datetime | None, info: ValidationInfo) -> dt.datetime | None:
        if (v is None) == (not info.data.get("occurred_at_unknown", False)):
            raise ValueError("send exactly one of occurred_at or occurred_at_unknown=true")
        return v

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 16 or any(len(k) > 64 or len(val) > 256 for k, val in v.items()):
            raise ValueError("at most 16 keys, values up to 256 characters")
        return v

    def effective_classification(self) -> str:
        """max(requested, workspace default): a request can raise the classification, never lower it below the default."""
        requested = self.sensitivity or DEFAULT_CLASSIFICATION
        return max(requested, DEFAULT_CLASSIFICATION, key=CLASSIFICATION_ORDER.index)

    @property
    def external_processing_allowed(self) -> bool:
        return self.consent is None or self.consent.external_processing == "allowed"

    def request_document(self) -> dict[str, Any]:
        """The canonical request (what the idempotency hash covers)."""
        return self.model_dump(mode="json")

    def raw_document(self) -> dict[str, Any]:
        """What is stored encrypted as the `raw` form: everything content-bearing, including unresolved identifiers and metadata."""
        return {
            "v": 1,
            "kind": self.kind,
            "content": self.content,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "subject_refs": [r.model_dump(mode="json") for r in self.subject_refs],
            "actor_ref": self.actor_ref.model_dump(mode="json") if self.actor_ref else None,
        }
