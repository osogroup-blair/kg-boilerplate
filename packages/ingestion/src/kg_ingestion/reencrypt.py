"""Payload key rotation: re-encrypts stored contents under another key id, in batches inside tenant sessions. Resumable (it only selects rows not yet
under the target key) and audited (`payload.reencrypted`, key id and count, never content)."""

from __future__ import annotations

from collections.abc import Callable

from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_ingestion.crypto import PayloadCipher, Sealed


async def reencrypt_workspace(
    engine: AsyncEngine,
    ctx: WorkspaceContext,
    cipher: PayloadCipher,
    audit_for: Callable[[AsyncConnection], AuditWriter],
    *,
    to_kid: str,
    batch_size: int = 100,
) -> int:
    if to_kid not in cipher.kids:
        raise ValueError(f"unknown target key id '{to_kid}'")
    total = 0
    while True:
        async with tenant_session(engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT interaction_id, form, kid, nonce, ciphertext FROM interaction_contents WHERE workspace_id = :ws AND kid <> :kid "
                        "ORDER BY interaction_id, form LIMIT :n FOR UPDATE SKIP LOCKED"
                    ),
                    {"ws": ctx.workspace_id, "kid": to_kid, "n": batch_size},
                )
            ).all()
            if not rows:
                return total
            for r in rows:
                plaintext = cipher.decrypt(
                    Sealed(r.kid, bytes(r.nonce), bytes(r.ciphertext)),
                    workspace_id=ctx.workspace_id,
                    interaction_id=r.interaction_id,
                    form=r.form,
                )
                sealed = cipher.encrypt(plaintext, workspace_id=ctx.workspace_id, interaction_id=r.interaction_id, form=r.form, kid=to_kid)
                await conn.execute(
                    text(
                        "UPDATE interaction_contents SET kid = :kid, nonce = :nonce, ciphertext = :ct "
                        "WHERE workspace_id = :ws AND interaction_id = :i AND form = :form"
                    ),
                    {
                        "kid": sealed.kid,
                        "nonce": sealed.nonce,
                        "ct": sealed.ciphertext,
                        "ws": ctx.workspace_id,
                        "i": r.interaction_id,
                        "form": r.form,
                    },
                )
            await audit_for(conn).record(
                "payload.reencrypted", resource_type="interaction_content", metadata=AuditMetadata(count=len(rows), payload_kid=to_kid)
            )
            total += len(rows)
