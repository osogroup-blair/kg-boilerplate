"""Opaque keyset cursors: base64url of (created_at, id). Clients treat them as strings."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime

from kg_common.errors import ValidationFailed

MAX_LIMIT = 100
DEFAULT_LIMIT = 50


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, row_id = json.loads(raw)
        return datetime.fromisoformat(created), uuid.UUID(row_id)
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Invalid cursor", details={"fields": ["cursor"]}) from exc


def clamp_limit(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
