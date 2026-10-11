"""Token counting for mock usage figures. The implementation lives in kg_common.tokens (L13): the mock imports production code,
never the other way round."""
from __future__ import annotations

from kg_common.tokens import TOKENIZER_ID, count_tokens, encoder, truncate_to_tokens

_enc = encoder  # kept for callers that used the old private name

__all__ = ["TOKENIZER_ID", "count_tokens", "truncate_to_tokens", "encoder", "_enc"]
