"""kg_domain: public Pydantic contracts shared by every package (Principal, WorkspaceContext, envelopes, response models)."""

from kg_domain.enums import PrincipalKind, PrincipalStatus, Role, WorkspaceStatus
from kg_domain.envelope import ErrorBody, ErrorEnvelope, Page
from kg_domain.principal import AuthMethod, Principal, WorkspaceContext

__version__ = "0.1.0"
__all__ = [
    "AuthMethod",
    "ErrorBody",
    "ErrorEnvelope",
    "Page",
    "Principal",
    "PrincipalKind",
    "PrincipalStatus",
    "Role",
    "WorkspaceContext",
    "WorkspaceStatus",
]
