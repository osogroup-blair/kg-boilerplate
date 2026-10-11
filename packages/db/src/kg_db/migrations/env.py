from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, text

config = context.config


def _url() -> str:
    url = config.attributes.get("url")
    if not url:
        from kg_common.settings import get_settings

        url = get_settings().database_url_owner.get_secret_value()
    return str(url)


def run() -> None:
    engine = create_engine(_url(), pool_pre_ping=True)
    with engine.connect() as conn:
        who = conn.execute(text("SELECT current_user")).scalar_one()
        if who == "kg_app":
            raise SystemExit("refusing to run migrations as kg_app; use the kg_owner URL (DATABASE_URL_OWNER)")
        conn.commit()
        context.configure(connection=conn, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
