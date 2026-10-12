"""Fixture replay for the mock router.

Each file in fixtures/*.json is a list of entries:
  {"key": "<sha256>",                      "response": <json or string>}      # recorded by the live kit (LV-10)
  {"when": {"alias": "oso/general", "schema_name": "ExtractedEntities", "contains": ["Sarah", "Acme"], "not_contains": ["NewCo"]}, "response": ...}
`contains` / `not_contains` are checked case-insensitively against the concatenated message text. First match wins; recorded keys are tried first.
Files under fixtures/scenarios/ (P4) are loaded first, then fixtures/*.json (Phase 0 spike rules and recorded keys).
Content must be synthetic (Sarah scenario) only.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fixture_key(alias: str, messages: list[dict[str, str]], schema_name: Optional[str] = None) -> str:
    payload = {"alias": alias, "schema": schema_name or "", "messages": [{"role": m["role"], "content": _norm(m["content"])} for m in messages]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FixtureStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.keyed: dict[str, Any] = {}
        self.rules: list[dict] = []
        self.dynamic: list[dict] = []  # rules added at run time through /__mock/fixtures (tests); consulted before the files
        self.reload()

    def reload(self) -> None:
        self.keyed.clear()
        self.rules.clear()
        if not self.directory.exists():
            return
        # scenario files first: a P4 scenario must win over the Phase 0 spike rules in fixtures/sarah.json (kept for the spike suite)
        for f in [*sorted(self.directory.glob("scenarios/*.json")), *sorted(self.directory.glob("*.json"))]:
            for entry in json.loads(f.read_text()):
                if "key" in entry:
                    self.keyed[entry["key"]] = entry["response"]
                elif "when" in entry:
                    self.rules.append(entry)

    def lookup(self, key: str, alias: str, messages: list[dict[str, str]], schema_name: Optional[str]) -> Any:
        if key in self.keyed:
            return self.keyed[key]
        text = _norm(" ".join(m["content"] for m in messages)).lower()
        for rule in [*self.dynamic, *self.rules]:
            w = rule["when"]
            if w.get("alias") not in (None, alias):
                continue
            if w.get("schema_name") not in (None, schema_name):
                continue
            if all(s.lower() in text for s in w.get("contains", [])) and not any(s.lower() in text for s in w.get("not_contains", [])):
                return rule["response"]
        return None
