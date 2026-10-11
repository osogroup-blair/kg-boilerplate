"""kg_ontology: the ontology spec, its validator, the declarative rule catalog and the pure evaluators. No database or graph imports."""

from kg_ontology.canonical import canonical_dict, canonical_json, content_hash
from kg_ontology.diff import Change, DiffResult, ImpactCount, diff_specs
from kg_ontology.ports import GraphCompilerPort
from kg_ontology.report import Issue, RuleTestResult, ValidationReport
from kg_ontology.spec import OntologySpec, parse_document, parse_text
from kg_ontology.validate import validate_document, validate_spec, validate_text

__version__ = "0.1.0"
__all__ = [
    "Change",
    "DiffResult",
    "GraphCompilerPort",
    "ImpactCount",
    "Issue",
    "OntologySpec",
    "RuleTestResult",
    "ValidationReport",
    "canonical_dict",
    "canonical_json",
    "content_hash",
    "diff_specs",
    "parse_document",
    "parse_text",
    "validate_document",
    "validate_spec",
    "validate_text",
]
