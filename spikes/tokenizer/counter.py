"""Budget accounting for CTX-01 (prototype of the P6 TokenCounter).

The router chooses the physical model, so the model's own tokenizer is unknowable. The platform therefore counts ITS OWN OUTPUT (the
context package) and inflates by a safety margin; the tokenizer id and margin are recorded in every trace.

Default = the LARGER of tiktoken o200k_base and cl100k_base, times (1 + margin). cl100k_base counts ~47% more tokens than o200k_base on
multilingual text (TK-01), so taking the max keeps the budget conservative for less efficient tokenizers; TK-02 calibrates the margin live.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import tiktoken


@dataclass(frozen=True)
class TokenCounter:
    encodings: tuple[str, ...] = ("o200k_base", "cl100k_base")
    margin: float = 0.10                         # default; recalibrated by TK-02 against router-reported prompt_tokens

    @property
    def tokenizer_id(self) -> str:
        names = self.encodings[0] if len(self.encodings) == 1 else f"max({','.join(self.encodings)})"
        return f"tiktoken:{names}+margin{self.margin:g}"

    def raw(self, text: str) -> int:
        return max(len(tiktoken.get_encoding(e).encode(text, disallowed_special=())) for e in self.encodings)

    def count(self, text: str) -> int:
        return math.ceil(self.raw(text) * (1 + self.margin))


def heuristic_chars(text: str, chars_per_token: float = 4.0) -> int:
    return math.ceil(len(text) / chars_per_token)
