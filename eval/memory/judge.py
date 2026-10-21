"""Semantic match for the LIVE memory evaluation.

The gold set says what a good memory must contain as `must_contain` phrases, and the offline (mock) evaluation checks them as exact substrings. A real model words the same
fact differently ("considering entrepreneurship as a path" for "a possible software business"), and an exact substring then counts a correct memory as both a false
positive and a miss. The judge is asked, at temperature 0 and with a strict JSON schema, whether a memory conveys every required element in any wording. It is consulted ONLY
when the substring check fails (a substring hit is always accepted), and every verdict is kept in the report so a person can audit it."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

SCHEMA = {
    "type": "object",
    "properties": {"conveys_all": {"type": "boolean"}, "missing": {"type": "array", "items": {"type": "string"}}},
    "required": ["conveys_all", "missing"],
    "additionalProperties": False,
}
PROMPT = (
    "You are checking a memory that an AI system extracted from a conversation.\n"
    "REQUIRED ELEMENTS (each must be stated or clearly implied by the memory, in any wording):\n{required}\n\n"
    "MEMORY:\n{memory}\n\n"
    "Answer with JSON: conveys_all is true only if EVERY required element is conveyed; a paraphrase or synonym counts, an element that is missing, vague or "
    "contradicted does not. List the elements that are not conveyed in `missing`."
)


class Judge:
    def __init__(
        self, base_url: str, api_key: str, *, alias: str = "oso/code-high", client: httpx.Client | None = None, attempts: int = 3
    ) -> None:
        self.base_url, self.alias, self.attempts = base_url.rstrip("/"), alias, attempts
        self._headers = {"authorization": f"Bearer {api_key}"}
        self._client = client or httpx.Client(timeout=180)
        self.log: list[dict[str, Any]] = []

    def conveys(self, required: list[str], memory: str) -> bool:
        body = {
            "model": self.alias,
            "temperature": 0,
            "max_tokens": 1500,  # the judge model reasons before it answers: a small limit is spent on that and the reply comes back empty (finish_reason "length")
            "messages": [{"role": "user", "content": PROMPT.format(required="\n".join(f"- {r}" for r in required), memory=memory)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "MemoryJudgement", "strict": True, "schema": SCHEMA}},
        }
        verdict: dict[str, Any] | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                r = self._client.post(f"{self.base_url}/chat/completions", headers=self._headers, json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                verdict = json.loads(r.json()["choices"][0]["message"]["content"])
                break
            except (httpx.HTTPError, ValueError, KeyError):
                if attempt == self.attempts:
                    raise
                time.sleep(2 * attempt)
        assert verdict is not None
        self.log.append(
            {
                "required": list(required),
                "memory": memory[:200],
                "conveys_all": bool(verdict["conveys_all"]),
                "missing": list(verdict.get("missing") or []),
            }
        )
        return bool(verdict["conveys_all"])
