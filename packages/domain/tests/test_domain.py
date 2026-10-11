import uuid

import pytest
from kg_domain import Principal, PrincipalKind, Role, WorkspaceContext

pytestmark = pytest.mark.unit


def _principal() -> Principal:
    return Principal(
        principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), kind=PrincipalKind.AGENT, handle="a", roles=frozenset({Role.AGENT})
    )


def test_workspace_context_only_from_principal():
    p = _principal()
    ctx = WorkspaceContext.of(p)
    assert ctx.workspace_id == p.workspace_id and ctx.group_id == f"ws-{p.workspace_id}"
    with pytest.raises(TypeError):
        WorkspaceContext(str(p.workspace_id))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        WorkspaceContext._make(str(p.workspace_id))  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        ctx.workspace_id = uuid.uuid4()  # type: ignore[misc]


def test_system_task_requires_reason():
    with pytest.raises(ValueError):
        WorkspaceContext.for_system_task(uuid.uuid4(), reason="")


def test_principal_is_frozen():
    p = _principal()
    with pytest.raises(Exception):
        p.handle = "x"  # type: ignore[misc]
