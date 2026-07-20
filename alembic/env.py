"""Alembic migration environment.

Bound to the *application's* engine and metadata rather than a URL configured
in alembic.ini:

* ``target_metadata`` is ``SQLModel.metadata`` (importing ``app.models``
  registers every table on it), so ``alembic revision --autogenerate`` diffs
  against the live ORM models.
* The connection comes from ``app.database.engine``. That engine is built from
  the ``DATABASE_URL`` env var (defaulting to the project-root SQLite file), and
  the test-suite swaps it at runtime — reading it here means the CLI, the
  startup path, and the tests all migrate the same database.

``render_as_batch`` is enabled for SQLite so future column alters work despite
SQLite's limited ``ALTER TABLE`` (it rebuilds the table); it is a no-op on
server databases.
"""

from __future__ import annotations

from alembic import context
from sqlmodel import SQLModel

import app.database as database
import app.models  # noqa: F401  — registers all tables on SQLModel.metadata

config = context.config

target_metadata = SQLModel.metadata


def _is_sqlite(bind) -> bool:
    return bind.dialect.name == "sqlite"


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (`alembic upgrade --sql`)."""
    url = str(database.engine.url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection from the app engine."""
    connectable = database.engine
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(connection),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
