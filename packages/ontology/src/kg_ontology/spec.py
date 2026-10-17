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
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    ValidationError,
    model_serializer,
    model_validator,
)
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


EntityAccess = Literal["workspace", "subject_scoped"]
ENTITY_ACCESS: tuple[str, ...] = ("workspace", "subject_scoped")


class EntityTypeSpec(_Strict):
    description: str = Field(default="", max_length=500)
    identity: IdentitySpec = Field(default_factory=IdentitySpec)
    allowed_classifications: list[Classification] = Field(default_factory=lambda: list(CLASSIFICATIONS))  # type: ignore[arg-type]
    # P7 (ADR-037): `subject_scoped` entities, and every fact touching one, are visible only to principals granted that subject. The default is the P2-P6 behaviour, and it
    # is not serialised, so a document published before P7 hashes exactly as it did.
    access: EntityAccess = "workspace"
    attributes: dict[AttrKey, AttributeSpec] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_default_access(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("access") == "workspace":
            data.pop("access", None)
        return data


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


CONTEXT_WEIGHT_KEYS = ("relevance", "authority", "importance", "evidence", "proximity", "recency", "diversity")
CONTEXT_SECTIONS = ("current_facts", "relevant_facts", "episodic", "semantic", "procedural", "working", "uncertainty", "history")
CONTEXT_COMPRESSIONS = ("structured", "extractive", "summarized")
SignalKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class SignalSource(_Strict):
    """One way an expected signal can be satisfied (P6, §5.6): a relationship the subject holds, an attribute it carries, or a memory assertion about it."""

    relationship: RelKey | None = None
    direction: Literal["out", "in", "both"] = "out"
    attribute: Identifier | None = None
    memory_predicate: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None

    @model_validator(mode="after")
    def _one(self) -> SignalSource:
        if sum(x is not None for x in (self.relationship, self.attribute, self.memory_predicate)) != 1:
            raise PydanticCustomError(
                "signal_source_shape", "a signal source names exactly one of relationship, attribute, memory_predicate"
            )
        return self


class ExpectedSignal(_Strict):
    """Deterministic unknown detection: when no authorised, verified item satisfies any `satisfied_by`, the context says `No verified <label> for <subject>`."""

    key: SignalKey
    label: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _/-]*$")
    for_types: list[Identifier] = Field(min_length=1, max_length=10)
    satisfied_by: list[SignalSource] = Field(min_length=1, max_length=6)


class ContextGraphSettings(_Strict):
    enabled: bool = True
    hops: int = Field(default=1, ge=0, le=2)
    limit: int = Field(default=20, ge=1, le=25)


def _default_memory_types() -> list[Literal["working", "episodic", "semantic", "procedural"]]:
    return ["episodic", "semantic", "procedural"]


class ContextMemorySettings(_Strict):
    types: list[Literal["working", "episodic", "semantic", "procedural"]] = Field(default_factory=_default_memory_types)
    limit_per_type: int = Field(default=8, ge=1, le=50)


class ContextProfile(BaseModel):
    """A versioned context profile (P6, blueprint 02-domain/05): caps, sections, retrieval weights, compression modes, expected signals.

    Every P6 field is optional and None until set, and a None field is not serialised, so a document published before P6 hashes exactly as it did. Unknown keys are
    still kept (`extra="allow"`). A request or an agent override can only NARROW what a profile allows (`kg_context.profiles`)."""

    model_config = ConfigDict(extra="allow")
    max_tokens: int | None = Field(default=None, ge=1)
    include: list[str] = Field(default_factory=list)
    evidence_required: bool = False
    # ---- P6 ----
    max_items: int | None = Field(default=None, ge=1, le=200)
    evidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    compression: list[Literal["structured", "extractive", "summarized"]] | None = Field(default=None, min_length=1)
    graph: ContextGraphSettings | None = None
    memory: ContextMemorySettings | None = None
    weights: dict[str, float] | None = None
    section_budget: dict[str, float] | None = None
    history: Literal["never", "when_relevant", "always"] | None = None
    current_facts: Literal["always", "relevant"] | None = (
        None  # always: a subject's critical current facts are packed whatever the task says
    )
    required_current_k: int | None = Field(default=None, ge=0, le=10)
    required_procedural_k: int | None = Field(default=None, ge=0, le=5)
    item_chars: int | None = Field(default=None, ge=40, le=1000)
    intent_terms: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9 -]{0,39}$")]] | None = Field(
        default=None, max_length=12
    )  # words that say what a task of this kind needs; appended to the MEMORY query
    expected_signals: list[ExpectedSignal] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ContextProfile:
        if self.weights is not None:
            for key, value in self.weights.items():
                if key not in CONTEXT_WEIGHT_KEYS or not 0.0 <= value <= 5.0:
                    raise PydanticCustomError(
                        "invalid_weight", "weights are {names}, each between 0 and 5", {"names": list(CONTEXT_WEIGHT_KEYS)}
                    )
        if self.section_budget is not None:
            for key, value in self.section_budget.items():
                if not 0.0 <= value <= 1.0:
                    raise PydanticCustomError("invalid_section_budget", "section_budget shares are between 0 and 1")
                _ = key
        return self  # `include` is deliberately not checked: a version published before P6 may name sections this release does not know (they are ignored)

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in (
            "max_items",
            "evidence_threshold",
            "compression",
            "graph",
            "memory",
            "weights",
            "section_budget",
            "history",
            "current_facts",
            "required_current_k",
            "required_procedural_k",
            "item_chars",
            "intent_terms",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        if not data.get("expected_signals"):
            data.pop("expected_signals", None)
        return data


MemoryTypeName = Literal["working", "episodic", "semantic", "procedural"]
MEMORY_POLICY_KEYS = ("default", "working", "episodic", "semantic", "procedural")
_PROCEDURAL_ALIAS = {"review_required": True, "required": True, "allowed": False, "auto_activate": False}


class MemoryPolicy(_Strict):
    """Typed, versioned memory policy (P5, ADR-029/030). Every field is optional: an unset field falls through `memory_policies.<type>` -> `default` ->
    the platform defaults (`kg_memory.policy`). The P2 key set is accepted (`inferred_shared_procedural`, `archive_after_days`), so versions published
    before P5 keep parsing; their stored hashes are never re-derived."""

    extraction: Literal["enabled", "disabled"] | None = None
    extraction_min_importance: float | None = Field(default=None, ge=0.0, le=1.0)
    max_memories_per_interaction: int | None = Field(default=None, ge=0, le=20)
    auto_activate: list[Literal["episodic", "semantic", "procedural"]] | None = None
    procedural_requires_review: bool | None = None
    agent_shared_requires_review: bool | None = None
    working_ttl_hours: float | None = Field(default=None, gt=0.0, le=24 * 30)
    max_content_chars: int | None = Field(default=None, ge=50, le=10_000)
    default_extracted_scope: Literal["subject", "workspace"] | None = None
    excluded_classifications: list[Classification] | None = None
    archive_after_days: int | None = Field(default=None, ge=1)  # reserved for P8 retention
    # P8 (ADR-043): consolidation. Unset fields are not serialised, so a document published before P8 hashes exactly as it did.
    consolidation: Literal["disabled", "reinforce", "synthesize"] | None = None
    consolidation_min_group: int | None = Field(default=None, ge=2, le=50)
    consolidation_auto_supersede: bool | None = (
        None  # reinforce only: folded duplicates are superseded at once. Synthesis never supersedes without a reviewer.
    )
    near_duplicate_jaccard: float | None = Field(default=None, ge=0.5, le=1.0)
    near_duplicate_cosine: float | None = Field(default=None, ge=0.5, le=1.0)
    episodic_window_days: int | None = Field(default=None, ge=1, le=3650)

    @model_serializer(mode="wrap")
    def _omit_unset_consolidation(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in (
            "consolidation",
            "consolidation_min_group",
            "consolidation_auto_supersede",
            "near_duplicate_jaccard",
            "near_duplicate_cosine",
            "episodic_window_days",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="before")
    @classmethod
    def _p2_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "inferred_shared_procedural" not in data:
            return data
        data = dict(data)
        value = data.pop("inferred_shared_procedural")
        if value not in _PROCEDURAL_ALIAS:
            raise PydanticCustomError(
                "invalid_policy_value", "inferred_shared_procedural must be one of {options}", {"options": sorted(_PROCEDURAL_ALIAS)}
            )
        data.setdefault("procedural_requires_review", _PROCEDURAL_ALIAS[value])
        return data

    @model_validator(mode="after")
    def _check(self) -> MemoryPolicy:
        if self.excluded_classifications is not None and "local_only" not in self.excluded_classifications:
            raise PydanticCustomError("local_only_not_excludable", "local_only content is never sent to a model, so it must stay excluded")
        if self.auto_activate and "procedural" in self.auto_activate and self.procedural_requires_review is not False:
            raise PydanticCustomError(
                "procedural_needs_review", "procedural memory can only auto-activate when procedural_requires_review is false"
            )
        return self


class OntologySpec(_Strict):
    ontology: OntologyHeader
    entity_types: dict[EntityKey, EntityTypeSpec] = Field(default_factory=dict)
    relationship_types: dict[RelKey, RelationshipTypeSpec] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)  # workspace-wide
    context_profiles: dict[str, ContextProfile] = Field(default_factory=dict)
    memory_policies: dict[Literal["default", "working", "episodic", "semantic", "procedural"], MemoryPolicy] = Field(default_factory=dict)

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
