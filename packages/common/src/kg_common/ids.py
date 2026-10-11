"""Identifiers. UUIDv7 (time-ordered) is implemented locally: Python 3.12 has no uuid.uuid7."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_last_seq = 0


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit unix-ms timestamp, 12-bit counter (monotonic within a millisecond), 62 random bits."""
    global _last_ms, _last_seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _last_seq += 1
            if _last_seq > 0xFFF:  # counter exhausted inside one millisecond: borrow the next one
                ms += 1
                _last_seq = 0
        else:
            _last_seq = int.from_bytes(os.urandom(2), "big") & 0x1FF  # leave head-room so the counter cannot overflow quickly
        _last_ms = ms
        seq = _last_seq
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | seq << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())


def is_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    return value.int >> 80
