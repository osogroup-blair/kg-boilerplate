"""The ingestion services as one object, built once per process by the composition root (api, worker, cli)."""

from __future__ import annotations

from dataclasses import dataclass

from kg_common.settings import Settings

from kg_ingestion.accept import AcceptService
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_ingestion.operations import OperationsService
from kg_ingestion.reads import InteractionReads
from kg_ingestion.reconcile import Reconciler


@dataclass
class IngestionServices:
    cipher: PayloadCipher
    hasher: ContentHasher
    accept: AcceptService
    reads: InteractionReads
    operations: OperationsService
    reconciler: Reconciler


def build_ingestion(settings: Settings) -> IngestionServices:
    cipher = PayloadCipher.from_settings(settings)  # raises KeyringError at startup on a bad ring or a missing active kid
    hasher = ContentHasher.from_settings(settings)
    return IngestionServices(
        cipher=cipher,
        hasher=hasher,
        accept=AcceptService(cipher, hasher, max_content_bytes=settings.interaction_max_content_bytes),
        reads=InteractionReads(cipher),
        operations=OperationsService(),
        reconciler=Reconciler(),
    )
