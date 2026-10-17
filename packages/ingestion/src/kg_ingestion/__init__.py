"""kg_ingestion: the interaction ledger (ADR-024). Envelope validation, encryption at rest, idempotent acceptance, the pipeline skeleton
(normalize, graph_ingest and memory_propose, in that dependency order), replay, reconcile and reads."""

from kg_common.versions import PIPELINE_VERSION

__version__ = "0.1.0"
__all__ = ["PIPELINE_VERSION"]
