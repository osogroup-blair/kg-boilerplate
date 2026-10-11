"""The purpose map: P2 adds `graph_projection`, which must stay embedding-only (ADR-016)."""

from __future__ import annotations

from kg_common.settings import Settings
from kg_modelgw.config import load_config


def test_graph_projection_is_an_embedding_only_purpose():
    s = Settings(_env_file=None, env="test")
    cfg = load_config(s.model_purposes_path, s.pricing_path)
    spec = cfg.purposes["graph_projection"]
    assert spec.alias == cfg.embedding.alias == "oso/embedding"
    assert spec.requires == [] and spec.fallback_alias is None and spec.task_type == "embedding"
    assert cfg.purposes["graph_extraction"].alias != spec.alias  # extraction (chat) and projection (embedding) never share an alias
