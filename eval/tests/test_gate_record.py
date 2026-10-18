"""The CI rule asks for a live-gate record only when a model-affecting pin changes: a FastAPI patch bump does not, a Graphiti or router change does."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_gate_record", Path(__file__).resolve().parents[2] / "scripts" / "check_gate_record.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BASE = """
services: {neo4j: {server: "5.26.31"}, postgres: {server: "17.11"}}
python_packages: {graphiti-core: "0.30.2", openai: "3.29.0", tiktoken: "0.14.0", fastapi: "0.143.0"}
router: {contract: "v1"}
"""


def test_a_web_framework_bump_is_not_a_model_change():
    assert mod.lock_slice(BASE) == mod.lock_slice(BASE.replace("0.143.0", "0.143.3").replace("17.11", "17.12"))


def test_graphiti_the_client_the_tokenizer_the_graph_database_and_the_router_are():
    for a, b in (
        ("0.30.2", "0.31.1"),
        ("3.29.0", "3.35.1"),
        ('tiktoken: "0.14.0"', 'tiktoken: "0.15.0"'),
        ("5.26.31", "5.27.0"),
        ('contract: "v1"', 'contract: "v2"'),
    ):
        old = BASE
        new = BASE.replace(a, b)
        assert new != old and mod.lock_slice(old) != mod.lock_slice(new), a
