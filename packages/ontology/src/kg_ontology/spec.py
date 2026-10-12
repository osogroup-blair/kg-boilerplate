"""OntologySpec: the canonical, validated form of an ontology (blueprint 02-domain/01, ADR-014).

Identifier rules live in the models, so no code path can skip them: type and relationship keys are interpolated into Cypher by Graphiti
(ADR-014, M3). The YAML in the blueprint and the designer's JSON both normalise to this model (`parse_document`).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from kg_ontology.report import Issue

IDENT_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
REL_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
ONTOLOGY_KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
SEMVER_PATTERN = r"^\d{1,4}\.\d{1,5}\.\d{1,6}$"

# Labels and edge names Graphiti uses for its own structure; a user type with the same name would corrupt the graph (ADR-014).
GRAPHITI_RESERVED_LABELS = frozenset({"Entity", "Episodic", "Community", "Saga"})
GRAPHITI_RESERVED_EDGES = frozenset({"MENTIONS", "HAS_EPISODE", "NEXT_EPISODE", "RELATES_TO", "HAS_MEMBER"})
# Property names the projector writes itself; an attribute with one of these names would be overwritten.
PLATFORM_RESERVED_ATTRIBUTES = frozenset(
    {"platform_entity_id", "platform_fact_id", "source_command_id", "authority_class", "ontology_version"}
)

AttrType = Literal["string", "integer", "number", "boolean", "date", "datetime"]
Classification = Literal["public", "internal", "confidential", "restricted", "local_only"]
CLASSIFICATIONS: tuple[str, ...] = ("public", "internal", "confidential", "restricted", "local_only")
Authority = Literal["authoritative_system", "admin_asserted", "user_asserted"]  # what a COMMAND may carry (derived from the caller's role)
# Everything a fact can carry. A model's output is never above `conversation_report` (a person's account of something said), and an agent's own
# generation is below it (ADR-027). Rank order is the conflict policy's authority ordering: authoritative_system > admin_asserted > user_asserted >
# conversation_report > agent_generated.
AnyAuthority = Literal["authoritative_system", "admin_asserted", "user_asserted", "conversation_report", "agent_generated"]
AUTHORITY_RANK: dict[str, int] = {
    "agent_generated": 1,
    "conversation_report": 2,
    "user_asserted": 3,
    "admin_asserted": 4,
    "authoritative_system": 5,
}
EXTRACTION_MAX_AUTHORITY = "conversation_report"  # a model output never changes a row whose authority is above this
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
CARDINALITY_STRICTNESS: dict[str, int] = {"many_to_many": 0, "one_to_many": 1, "many_to_one": 1, "one_to_one": 2}


def _entity_key(value: str) -> str:
    if value in GRAPHITI_RESERVED_LABELS:
        raise PydanticCustomError("reserved_label", "'{name}' is a label Graphiti reserves for its own nodes", {"name": value})
    return value


def _rel_key(value: str) -> str:
    if value in GRAPHITI_RESERVED_EDGES:
        raise PydanticCustomError("reserved_edge_name", "'{name}' is an edge name Graphiti reserves", {"name": value})
    return value


def _attr_key(value: str) -> str:
    if value in PLATFORM_RESERVED_ATTRIBUTES:
        raise PydanticCustomError("reserved_attribute_name", "'{name}' is a property the platform writes itself", {"name": value})
    return value


EntityKey = Annotated[
    str,
    StringConstraints(pattern=IDENT_PATTERN),
    AfterValidator(_entity_key),
]
RelKey = Annotated[str, StringConstraints(pattern=REL_PATTERN), AfterValidator(_rel_key)]
AttrKey = Annotated[str, StringConstraints(pattern=IDENT_PATTERN), AfterValidator(_attr_key)]
Identifier = Annotated[str, StringConstraints(pattern=IDENT_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttributeSpec(_Strict):
    type: AttrType = "string"
    required: bool = False
    default: str | int | float | bool | None = None
    editable: bool = True  # False => settable only when the entity is created
    sensitive: bool = False
    sensitivity: Classification | None = None
    description: str = Field(default="", max_length=500)
    pattern: str | None = Field(default=None, max_length=200)
    min: float | None = None
    max: float | None = None
    max_length: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def _check(self) -> AttributeSpec:
        if self.sensitivity in ("restricted", "local_only"):
            self.sensitive = True
        if self.sensitive and self.sensitivity is None:
            self.sensitivity = "restricted"
        if self.pattern is not None:
            if self.type != "string":
                raise PydanticCustomError("constraint_not_applicable", "'pattern' only applies to string attributes")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise PydanticCustomError("invalid_pattern", "pattern is not a valid regular expression: {why}", {"why": str(exc)}) from exc
        if self.max_length is not None and self.type != "string":
            raise PydanticCustomError("constraint_not_applicable", "'max_length' only applies to string attributes")
        if (self.min is not None or self.max is not None) and self.type not in ("integer", "number"):
            raise PydanticCustomError("constraint_not_applicable", "'min' and 'max' only apply to integer and number attributes")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise PydanticCustomError("invalid_range", "'min' is greater than 'max'")
        return self


class IdentitySpec(_Strict):
    key: Literal["platform_uuid", "external_id"] = "platform_uuid"
    external_id_systems: list[Identifier] = Field(
        default_factory=list
    )  # strong identifiers that match deterministically (KGR-01); names never do (KGR-02)
    aliases_from: list[str] = Field(default_factory=lambda: ["name"])


class RuleTest(_Strict):
    """A scenario attached to a rule (blueprint 'Rule … test cases'): publish runs it and fails on a mismatch."""

    name: str = Field(default="", max_length=200)
    given: dict[str, Any] = Field(default_factory=dict)
    command: dict[str, Any] = Field(default_factory=dict)
    expect: Literal["accept", "reject", "queue_review"]


class RuleRef(_Strict):
    kind: str = Field(max_length=64)
    id: str | None = Field(default=None, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["error", "warning"] | None = None
    action: Literal["reject", "queue_review", "warn"] | None = None
    tests: list[RuleTest] = Field(default_factory=list)


class EntityTypeSpec(_Strict):
    description: str = Field(default="", max_length=500)
    identity: IdentitySpec = Field(default_factory=IdentitySpec)
    allowed_classifications: list[Classification] = Field(default_factory=lambda: list(CLASSIFICATIONS))  # type: ignore[arg-type]
    attributes: dict[AttrKey, AttributeSpec] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)


class RelationshipTypeSpec(_Strict):
    description: str = Field(default="", max_length=500)
    source: Identifier
    target: Identifier
    cardinality: Cardinality = "many_to_many"  # informative; enforced only through rules
    temporal: bool = False
    fact_template: str | None = Field(default=None, max_length=500)  # deterministic text for the graph fact (no LLM)
    attributes: dict[AttrKey, AttributeSpec] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)


class OntologyHeader(_Strict):
    key: Annotated[str, StringConstraints(pattern=ONTOLOGY_KEY_PATTERN)]
    version: Annotated[str, StringConstraints(pattern=SEMVER_PATTERN)]
    mode: Literal["strict", "permissive"] = "strict"  # permissive: unknown attributes are kept and warned about


class ContextProfile(BaseModel):
    """Stored and shape-checked now; evaluated in P6. Unknown keys are kept so P6 can extend the profile without a migration."""

    model_config = ConfigDict(extra="allow")
    max_tokens: int | None = Field(default=None, ge=1)
    include: list[str] = Field(default_factory=list)
    evidence_required: bool = False


class OntologySpec(_Strict):
    ontology: OntologyHeader
    entity_types: dict[EntityKey, EntityTypeSpec] = Field(default_factory=dict)
    relationship_types: dict[RelKey, RelationshipTypeSpec] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)  # workspace-wide
    context_profiles: dict[str, ContextProfile] = Field(default_factory=dict)
    memory_policies: dict[str, dict[str, str | int | float | bool | None]] = Field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.ontology.version

    @property
    def key(self) -> str:
        return self.ontology.key


# ---- loading -------------------------------------------------------------------------------------------------------------------------


def _shorthand(rules: Any) -> Any:
    if not isinstance(rules, list):
        return rules
    return [{"kind": r} if isinstance(r, str) else r for r in rules]


def prepare_document(raw: Any) -> Any:
    """Expand the blueprint's shorthands without touching the input: `ontology.id` -> `key`, `identity.aliases` -> `aliases_from`, bare rule names."""
    doc = copy.deepcopy(raw)
    if not isinstance(doc, dict):
        return doc
    header = doc.get("ontology")
    if isinstance(header, dict) and "id" in header and "key" not in header:
        header["key"] = header.pop("id")
    if "rules" in doc:
        doc["rules"] = _shorthand(doc["rules"])
    for section in ("entity_types", "relationship_types"):
        types = doc.get(section)
        if not isinstance(types, dict):
            continue
        for spec in types.values():
            if not isinstance(spec, dict):
                continue
            if "rules" in spec:
                spec["rules"] = _shorthand(spec["rules"])
            identity = spec.get("identity")
            if isinstance(identity, dict) and "aliases" in identity and "aliases_from" not in identity:
                identity["aliases_from"] = identity.pop("aliases")
    return doc


def _loc_to_path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if part == "[key]":
            continue
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def issues_from_validation_error(exc: ValidationError) -> list[Issue]:
    seen: set[tuple[str, str]] = set()
    issues: list[Issue] = []
    for err in exc.errors(include_url=False, include_input=False):
        path = _loc_to_path(err["loc"])
        code = str(err["type"])
        if (code, path) in seen:
            continue
        seen.add((code, path))
        issues.append(Issue(code=code, path=path, message=str(err["msg"])))
    return issues


def load_text(text: str, fmt: Literal["yaml", "json"]) -> tuple[Any, list[Issue]]:
    """Parse the source text into a plain document. Syntax errors are reported (never raised) so an invalid draft can still be saved."""
    try:
        doc = yaml.safe_load(text) if fmt == "yaml" else json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        where = f" (line {line + 1})" if line is not None else ""
        return None, [Issue(code=f"{fmt}_syntax", path="", message=f"the document is not valid {fmt.upper()}{where}")]
    return doc, []


def parse_document(doc: Any) -> tuple[OntologySpec | None, list[Issue]]:
    """Validate a plain document. Returns (spec, []) or (None, issues with JSON paths)."""
    if not isinstance(doc, dict):
        return None, [Issue(code="document_not_mapping", path="", message="the ontology must be a mapping")]
    try:
        return OntologySpec.model_validate(prepare_document(doc)), []
    except ValidationError as exc:
        return None, issues_from_validation_error(exc)


def parse_text(text: str, fmt: Literal["yaml", "json"] = "yaml") -> tuple[OntologySpec | None, list[Issue]]:
    doc, issues = load_text(text, fmt)
    if issues:
        return None, issues
    return parse_document(doc)
