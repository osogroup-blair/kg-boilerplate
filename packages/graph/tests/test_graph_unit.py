import uuid

import pytest
from kg_domain.principal import WorkspaceContext
from kg_graph.group import GroupId

pytestmark = pytest.mark.unit


def _ctx() -> WorkspaceContext:
    return WorkspaceContext.for_system_task(uuid.uuid4(), reason="test")


def test_group_id_only_from_workspace_context():
    ctx = _ctx()
    gid = GroupId.for_workspace(ctx)
    assert str(gid) == f"ws-{ctx.workspace_id}"
    for bad in ("ws-abc", "", None, str(ctx.workspace_id)):
        with pytest.raises(TypeError):
            GroupId.for_workspace(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GroupId("ws-x")  # type: ignore[call-arg]
    with pytest.raises(AttributeError):
        gid.value = "ws-other"  # type: ignore[misc]


def test_graphiti_takes_the_extraction_aliases_and_temperature_from_the_purposes_file():
    from kg_common.settings import Settings
    from kg_graph.factory import llm_config
    from kg_modelgw.config import load_config

    s = Settings(_env_file=None, env="test")
    cfg = load_config(s.model_purposes_path, s.pricing_path)
    llm = llm_config(cfg, "http://router/v1")
    assert (llm.model, llm.small_model) == (cfg.purposes["graph_extraction"].alias, cfg.purposes["graph_extraction_small"].alias)
    assert llm.temperature == 0.0  # Graphiti's own default is 1.0
    cfg.purposes["graph_extraction"].temperature = None
    assert llm_config(cfg, "http://router/v1").temperature == 1  # unset: Graphiti's default, unchanged behaviour
