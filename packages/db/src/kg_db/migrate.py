"""Programmatic Alembic runner. Always connects as kg_owner; refuses kg_app."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _config(url: str, **attributes: object) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    cfg.attributes.update(attributes)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(_config(url), revision)


def downgrade(
    url: str,
    revision: str = "base",
    *,
    force_drop_extracted: bool = False,
    force_drop_memories: bool = False,
    force_forget_privileges: bool = False,
    force_drop_governance: bool = False,
    force_drop_release: bool = False,
) -> None:
    """`force_drop_extracted` lets 0004's downgrade delete extracted entities, facts and episodes, `force_drop_memories` lets 0005's delete curated memories, and
    `force_forget_privileges` lets 0007's drop grants that carry `publish`, and `force_drop_governance` lets 0008's keep rows made by corrections as ordinary rows, and `force_drop_release` lets 0009 drop reindex runs that are still building (each refuses otherwise)."""
    command.downgrade(
        _config(
            url,
            force_drop_extracted=force_drop_extracted,
            force_drop_memories=force_drop_memories,
            force_forget_privileges=force_forget_privileges,
            force_drop_governance=force_drop_governance,
            force_drop_release=force_drop_release,
        ),
        revision,
    )


def current(url: str) -> None:
    command.current(_config(url), verbose=False)
