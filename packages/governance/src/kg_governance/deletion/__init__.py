"""Deletion orchestration (phase-8 plan §5.4, blueprint Flow E, SEC-05, AT-16, MEM-07, ADR-044/045): freeze, enumerate, purge, verify, receipt.

A deletion request freezes its target and the first derivatives in ONE transaction (status `deletion_pending`: every read already hides it), then a resumable job
chain plans the rest, purges store by store through the database's purge gate, verifies with id scans and residual-text scans, and issues a receipt that says what was
removed, what was kept and why, and when the last backup copy will have expired. This package reaches the graph and the memory store only through ports."""

from kg_governance.deletion.ports import GraphPurgePort, JobEnqueuer, ManifestCipher
from kg_governance.deletion.requests import DeletionService

__all__ = ["DeletionService", "GraphPurgePort", "JobEnqueuer", "ManifestCipher"]
