"""API keys (DEC-03). Format: kgk_<8-char public prefix>_<secret>. Only SHA-256(pepper || key) is stored; the plaintext is shown once."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

KEY_SCHEME = "kgk"
_PREFIX_LEN = 8


UNLABELED = "unlabeled"  # keys minted before P9 carry no pepper label: they are tried against the current pepper, then the previous one


@dataclass(frozen=True)
class MintedKey:
    plaintext: str
    prefix: str
    key_hash: str
    pepper_id: str = UNLABELED


def pepper_fingerprint(pepper: str) -> str:
    """A label for a pepper that reveals nothing about it (a salted SHA-256, 12 hex characters): stored with every key hash so the authenticator knows which pepper to try."""
    return hashlib.sha256(b"kg-pepper-id|" + pepper.encode()).hexdigest()[:12]


def hash_key(pepper: str, key: str) -> str:
    return hashlib.sha256(pepper.encode() + key.encode()).hexdigest()


def mint_key(pepper: str) -> MintedKey:
    prefix = secrets.token_urlsafe(8)[:_PREFIX_LEN].replace("_", "x").replace("-", "y")
    plaintext = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    return MintedKey(plaintext, prefix, hash_key(pepper, plaintext), pepper_fingerprint(pepper))


def parse_prefix(presented: str) -> str | None:
    """The public prefix, or None if the string is not shaped like a key. The secret part may itself contain underscores."""
    parts = presented.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_SCHEME or len(parts[1]) != _PREFIX_LEN or not parts[2]:
        return None
    return parts[1]


def looks_like_key(presented: str) -> bool:
    return presented.startswith(KEY_SCHEME + "_")


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
