"""The conflict policy is pure and lives in kg_ontology (kg_knowledge's review decisions must use the very same code and may not import this package);
this module is the name the phase plan gives it."""

from kg_ontology.conflicts import (
    EndEffect,
    FactView,
    Incoming,
    Invalidation,
    PolicyInputs,
    Resolution,
    evidence_status,
    rank,
    resolve,
    succession_evidence,
)

__all__ = [
    "EndEffect",
    "FactView",
    "Incoming",
    "Invalidation",
    "PolicyInputs",
    "Resolution",
    "evidence_status",
    "rank",
    "resolve",
    "succession_evidence",
]
