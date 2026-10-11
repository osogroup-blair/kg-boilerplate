from __future__ import annotations

from kg_common.settings import Settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class UnsafeDatabaseRole(RuntimeError):
    """The application connected as a role that can ignore row-level security (L1)."""


def create_app_engine(
    settings: Settings, *, url: str | None = None, pool_size: int | None = None, max_overflow: int | None = None
) -> AsyncEngine:
    """Engine for the API and worker: connects as kg_app. `reset_on_return="rollback"` plus transaction-local set_config (L2)."""
    return create_async_engine(
        url or settings.database_url_app.get_secret_value(),
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        pool_size=pool_size if pool_size is not None else settings.db_pool_size,
        max_overflow=max_overflow if max_overflow is not None else settings.db_max_overflow,
    )


async def assert_role_is_safe(engine: AsyncEngine) -> str:
    """Refuse to run as a superuser or BYPASSRLS role: RLS would be silently ignored, even with FORCE (PV-02). Returns the role name."""
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
        await conn.rollback()
    if row.rolsuper or row.rolbypassrls:
        raise UnsafeDatabaseRole(
            f"database role '{row.rolname}' can bypass row-level security (superuser={row.rolsuper}, bypassrls={row.rolbypassrls})"
        )
    return str(row.rolname)
