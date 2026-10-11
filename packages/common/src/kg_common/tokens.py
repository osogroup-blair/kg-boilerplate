"""Token counting (ADR-020). Moved here from the Phase 0 spike (L13) so production code and the mock router share one implementation.

The router chooses the physical model, so the platform counts ITS OWN OUTPUT with a conservative configured tokenizer and records
the tokenizer id. Default budget counter = the larger of tiktoken o200k_base and cl100k_base, times (1 + margin).

Module-level helpers (`count_tokens`, `truncate_to_tokens`, `encoder`) use o200k_base alone: they estimate usage / chunk inputs, not budgets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import tiktoken

TOKENIZER_ID = "tiktoken:o200k_base"


@lru_cache(maxsize=1)
def encoder() -> Any | None:
    """o200k_base, or None when the BPE file cannot be fetched (first run offline): callers fall back to a 4 chars/token estimate."""
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    enc = encoder()
    return len(enc.encode(text, disallowed_special=())) if enc else max(1, len(text) // 4)


def truncate_to_tokens(text: str, n: int) -> str:
    enc = encoder()
    if enc is None:
        return text[: n * 4]
    return str(enc.decode(enc.encode(text, disallowed_special=())[:n]))


def split_by_tokens(text: str, limit: int) -> list[str]:
    """Split into pieces of at most `limit` tokens (character estimate when no encoder is available)."""
    enc = encoder()
    if enc is None:
        step = limit * 4
        return [text[i : i + step] for i in range(0, len(text), step)] or [""]
    ids = enc.encode(text, disallowed_special=())
    return [str(enc.decode(ids[i : i + limit])) for i in range(0, len(ids), limit)] or [""]


@dataclass(frozen=True)
class TokenCounter:
    encodings: tuple[str, ...] = ("o200k_base", "cl100k_base")
    margin: float = 0.10  # default; recalibrated by TK-02 against router-reported prompt_tokens

    @property
    def tokenizer_id(self) -> str:
        names = self.encodings[0] if len(self.encodings) == 1 else f"max({','.join(self.encodings)})"
        return f"tiktoken:{names}+margin{self.margin:g}"

    def raw(self, text: str) -> int:
        return max(len(tiktoken.get_encoding(e).encode(text, disallowed_special=())) for e in self.encodings)

    def count(self, text: str) -> int:
        return math.ceil(self.raw(text) * (1 + self.margin))
