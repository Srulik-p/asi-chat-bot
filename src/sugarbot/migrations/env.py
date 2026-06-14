"""Alembic env — delegates URL + metadata to db.py.

This keeps Postgres/SQLite switching, psycopg3 driver normalization, and
the Table definitions in a single place. Running `alembic upgrade head`
locally hits whatever DATABASE_URL/USERS_DB_PATH points at, same as the app.
"""

from __future__ import annotations

from alembic import context

from sugarbot import db

config = context.config
target_metadata = db._metadata

# Surface the resolved URL so `alembic current` / errors point at the real DB.
config.set_main_option("sqlalchemy.url", db._URL)


def run_migrations_offline() -> None:
    context.configure(
        url=db._URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with db._engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
