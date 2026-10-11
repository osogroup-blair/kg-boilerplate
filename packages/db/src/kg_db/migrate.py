"""Programmatic Alembic runner. Always connects as kg_owner; refuses kg_app."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(_config(url), revision)


def current(url: str) -> None:
    command.current(_config(url), verbose=False)
