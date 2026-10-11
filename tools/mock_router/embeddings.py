"""Deterministic 384-dim unit vectors by feature hashing, so lexically similar texts are close (useful for retrieval tests).

E5 prefixes ("query: ", "passage: ") are stripped before hashing, as a real E5 model treats them as markers, not content.
Not semantically meaningful beyond word overlap; GR-10/PV-03 results measure plumbing and scope isolation, not model quality.
"""
from __future__ import annotations

import hashlib
import math
import re

_WORD = re.compile(r"[a-z0-9]+")
_PREFIX = re.compile(r"^(query|passage):\s*")


def embed_text(text: str, dim: int = 384) -> list[float]:
    words = _WORD.findall(_PREFIX.sub("", text.lower()))
    vec = [0.0] * dim
    grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
    if not grams:
        grams = ["<empty>"]
    for g in grams:
        h = hashlib.blake2b(g.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
