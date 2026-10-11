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
