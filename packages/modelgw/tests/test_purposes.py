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


def test_every_structured_purpose_pins_its_temperature_to_zero():
    """Structured extraction must be repeatable. Unpinned, the router's default (0.7) applies, and Graphiti's own default is 1.0: the same input gave different
    answers on each run (live memory evaluation, 2026-10-21: the unchanged policy scored precision 0.14 and 0.67)."""
    s = Settings(_env_file=None, env="test")
    cfg = load_config(s.model_purposes_path, s.pricing_path)
    structured = {k: v for k, v in cfg.purposes.items() if "structured_output" in v.requires}
    assert structured and all(v.temperature == 0.0 for v in structured.values()), {k: v.temperature for k, v in structured.items()}
    assert cfg.purposes["context_compression"].temperature is None  # plain-text purposes keep the router's default


def test_temperature_is_validated_when_the_file_is_read():
    import pytest
    from kg_modelgw.config import PurposeSpec
    from pydantic import ValidationError

    assert PurposeSpec(alias="oso/code", temperature=0).temperature == 0.0
    with pytest.raises(ValidationError):
        PurposeSpec(alias="oso/code", temperature=2.5)
    with pytest.raises(ValidationError):
        PurposeSpec(alias="oso/code", temperature=-0.1)
