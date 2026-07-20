"""Schema-management tests for the Alembic-based migration flow.

Covers the three entry states create_db_and_tables() must handle: a fresh
database, a legacy database created under the old PRAGMA user_version scheme
(transparently adopted by Alembic), and a database stamped at a revision this
app does not know about (must refuse to migrate).
"""

from __future__ import annotations

import os
import tempfile

import pytest
import sqlalchemy
from alembic.util.exc import CommandError

import app.database as database
import app.models  # noqa: F401  – registers tables on SQLModel.metadata


def _alembic_version(engine) -> str | None:
    with engine.connect() as conn:
        if not sqlalchemy.inspect(engine).has_table("alembic_version"):
            return None
        return conn.execute(
            sqlalchemy.text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _invoice_columns(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(sqlalchemy.text(
            "PRAGMA table_info(invoice)"))}


def _index_names(engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(sqlalchemy.text(
            "SELECT name FROM sqlite_master WHERE type='index'"))]


def test_startup_stamps_alembic_head(client, db_engine):
    """App startup (lifespan → create_db_and_tables) leaves the DB at head."""
    assert _alembic_version(db_engine) == database._BASELINE_REVISION


def test_migrations_are_idempotent(client, db_engine):
    """A second startup on an already-migrated DB is a no-op, not an error."""
    database.create_db_and_tables()
    assert _alembic_version(db_engine) == database._BASELINE_REVISION


def test_fresh_database_is_built_by_alembic(db_engine):
    """A DB with no tables is built entirely from the Alembic migrations."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        engine = sqlalchemy.create_engine(
            f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}
        )
        database.engine = engine
        assert _alembic_version(engine) is None
        database.create_db_and_tables()
        assert _alembic_version(engine) == database._BASELINE_REVISION
        # Full schema present, including the credit-note columns and the VS index.
        assert {"doc_type", "related_invoice_id"} <= _invoice_columns(engine)
        assert "ix_invoice_user_vs" in _index_names(engine)
    finally:
        database.engine = db_engine   # restore for the fixture's teardown
        os.unlink(tmp.name)


def test_legacy_pragma_db_is_adopted(db_engine):
    """A pre-Alembic DB (PRAGMA user_version, no alembic_version) is adopted.

    Simulate a database from before credit-notes existed: drop the newer
    columns, roll user_version back, and remove the Alembic bookkeeping table.
    create_db_and_tables must finish the legacy PRAGMA migrations *and* hand the
    DB over to Alembic by stamping the baseline.
    """
    with db_engine.connect() as conn:
        conn.execute(sqlalchemy.text("DROP TABLE alembic_version"))
        conn.execute(sqlalchemy.text("ALTER TABLE invoice DROP COLUMN doc_type"))
        conn.execute(sqlalchemy.text("ALTER TABLE invoice DROP COLUMN related_invoice_id"))
        conn.execute(sqlalchemy.text("PRAGMA user_version = 1"))
        conn.commit()

    database.create_db_and_tables()

    assert _alembic_version(db_engine) == database._BASELINE_REVISION
    assert {"doc_type", "related_invoice_id"} <= _invoice_columns(db_engine)
    assert "ix_invoice_user_vs" in _index_names(db_engine)


def test_unknown_revision_refuses_to_start(db_engine):
    """A DB stamped at a revision this app doesn't ship must abort startup."""
    with db_engine.connect() as conn:
        conn.execute(sqlalchemy.text("UPDATE alembic_version SET version_num = 'deadbeef'"))
        conn.commit()
    with pytest.raises(CommandError):
        database.create_db_and_tables()
