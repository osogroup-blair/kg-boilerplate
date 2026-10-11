"""One deny-list for logs and span attributes. Keys are matched case-insensitively at any depth; other strings are truncated."""

from __future__ import annotations

from typing import Any

DENY_KEYS = frozenset(
    {
        "content",
        "messages",
        "input",
        "text",
        "payload",
        "prompt",
        "body",
        "embedding",
        "authorization",
        "api_key",
        "password",
        "token",
        "secret",
        "key",
        "cookie",
        "access_token",
        "jwt",
        "ciphertext",
        "nonce",
        "content_hmac",
        "plaintext",
    }
)
MAX_STR = 256
REDACTED = "[redacted]"


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and k.lower() in DENY_KEYS else redact(v, depth=depth + 1)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, depth=depth + 1) for v in value]
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + "...[truncated]"
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    return value


def redact_attributes(attrs: dict[str, Any] | None) -> dict[str, Any]:
    out = redact(dict(attrs or {}))
    return {k: v for k, v in out.items() if v != REDACTED}
