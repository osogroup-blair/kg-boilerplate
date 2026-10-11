from __future__ import annotations

from enum import StrEnum


class PrincipalKind(StrEnum):
    USER = "user"
    APP = "app"
    AGENT = "agent"
    SERVICE = "service"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Role(StrEnum):
    WORKSPACE_ADMIN = "workspace_admin"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    APP_WRITER = "app_writer"
    AGENT = "agent"
    READER = "reader"
