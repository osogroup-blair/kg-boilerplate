"""Ports the ontology package needs from outside. The composition roots (api, worker, cli) pass the implementations in."""

from __future__ import annotations

from typing import Protocol

from kg_ontology.report import Issue
from kg_ontology.spec import OntologySpec


class GraphCompilerPort(Protocol):
    """Checks that the ontology compiles to Graphiti entity/edge types (reserved-name collisions, unsafe identifiers). Implemented by kg_graph.compile."""

    def check(self, spec: OntologySpec) -> tuple[list[Issue], list[Issue]]:
        """Returns (errors, warnings), each anchored at the offending field's JSON path."""
        ...
