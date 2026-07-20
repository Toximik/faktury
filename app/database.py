"""
database.py – database engine, session factory, and schema management.

The engine is built from the ``DATABASE_URL`` environment variable. It defaults
to a SQLite file (``faktury.db``) in the project root, one level above the
``app/`` package, so the dev/self-hosted database survives code deploys and venv
re-creations. Pointing ``DATABASE_URL`` at PostgreSQL or MySQL (e.g.
``postgresql+psycopg://user:pass@host/db``) is all that is needed to run on a
server database — the SQLite-only connect arg and the connection-pool tuning
below are applied conditionally on the URL scheme.

Schema changes are managed by **Alembic** (see ``alembic/``); there is no more
hand-rolled ``PRAGMA user_version`` bookkeeping in the normal path. On startup
``create_db_and_tables()`` runs ``alembic upgrade head``. Databases created
before Alembic was adopted (tracked via SQLite's ``PRAGMA user_version``) are
transparently adopted the first time they are opened — see
``_adopt_legacy_pragma_database``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Generator

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlmodel import Session, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Resolve the project root regardless of the working directory at runtime.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SQLITE_PATH = _PROJECT_ROOT / "faktury.db"
_DEFAULT_DATABASE_URL = f"sqlite:///{_DEFAULT_SQLITE_PATH}"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or _DEFAULT_DATABASE_URL

_IS_SQLITE = DATABASE_URL.startswith("sqlite")


def _build_engine(url: str):
    """Create an engine, applying only the options valid for its dialect."""
    kwargs: dict = {"echo": False}  # set echo=True temporarily for SQL debugging
    if url.startswith("sqlite"):
        # check_same_thread=False is required for SQLite under FastAPI's thread
        # pool; it is meaningless (and rejected) on other drivers.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Server databases: verify a pooled connection is alive before use and
        # recycle it well before typical server-side idle timeouts.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return create_engine(url, **kwargs)


engine = _build_engine(DATABASE_URL)


# ---------------------------------------------------------------------------
# Alembic wiring
# ---------------------------------------------------------------------------

_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"

# The revision that represents the schema as it stood under the old
# PRAGMA user_version scheme (baseline + credit-note columns). Legacy databases
# are stamped here when adopted; future revisions chain off it.
_BASELINE_REVISION = "0001_baseline"


def _alembic_config() -> Config:
    """Alembic config pointed at this project's ``alembic/`` directory.

    env.py binds to ``app.database.engine`` directly, so the connection always
    follows the module-level ``engine`` (which the test-suite swaps at runtime);
    the URL set here is only for offline/`--sql` use.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


# ---------------------------------------------------------------------------
# Schema creation & migrations
# ---------------------------------------------------------------------------

def create_db_and_tables() -> None:
    """Bring the database schema up to the latest Alembic revision.

    Safe to call on every startup (Alembic no-ops when already current):

    * **Fresh database** (no application tables) → ``upgrade head`` builds the
      whole schema from the migrations.
    * **Legacy database** (tables exist but no ``alembic_version`` — i.e. one
      created under the old ``PRAGMA user_version`` scheme, or a test DB built
      from ``SQLModel.metadata``) → finish any pending legacy PRAGMA migrations,
      stamp the Alembic baseline, then ``upgrade head`` for anything newer.
    * **Alembic-managed database** → plain ``upgrade head``.
    """
    cfg = _alembic_config()

    if _is_fresh_database():
        command.upgrade(cfg, "head")
    else:
        if not _is_alembic_managed():
            _adopt_legacy_pragma_database(cfg)
        command.upgrade(cfg, "head")

    _ensure_master_is_admin()


def _is_fresh_database() -> bool:
    """True when the database has none of the application's tables yet."""
    return not inspect(engine).has_table("user")


def _is_alembic_managed() -> bool:
    """True once Alembic is actually tracking this database.

    Requires not just the ``alembic_version`` table but a stamped revision row
    in it: a bare, row-less table (e.g. left behind by an autogenerate run or an
    interrupted first migration) means the DB has *not* been adopted yet and
    must still go through ``_adopt_legacy_pragma_database``.
    """
    if not inspect(engine).has_table("alembic_version"):
        return False
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM alembic_version")
        ).scalar_one() > 0


def _adopt_legacy_pragma_database(cfg: Config) -> None:
    """Hand a pre-Alembic database over to Alembic.

    Databases created before Alembic tracked their version via SQLite's
    ``PRAGMA user_version``. Finish any migrations still pending under that
    scheme so the schema matches the baseline exactly, then stamp the baseline
    revision. Only SQLite ever used the PRAGMA scheme, so the finishing step is
    a SQLite-only concern; a non-SQLite database in this branch (tables present,
    never Alembic-managed) is simply stamped.
    """
    if _IS_SQLITE:
        _finish_legacy_pragma_migrations()
    command.stamp(cfg, _BASELINE_REVISION)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request.

    Usage::

        @app.get("/example")
        def example(session: Session = Depends(get_session)):
            ...
    """
    with Session(engine) as session:
        yield session


# ===========================================================================
# Legacy PRAGMA user_version migrations (adoption-only).
#
# Retained solely to finish migrating a database that predates Alembic before
# stamping it with the baseline revision. Do NOT add new steps here — new schema
# changes are Alembic revisions under alembic/versions/.
# ===========================================================================

def _legacy_stamp_version(conn: Connection, version: int) -> None:
    conn.execute(text(f"PRAGMA user_version = {version}"))
    conn.commit()


def _migration_0001_baseline(conn: Connection) -> None:
    """Baseline for databases created before schema versioning existed.

    Tolerant by necessity: a pre-versioning database may already have any subset
    of these columns, so each ALTER is attempted and a duplicate-column error is
    ignored.
    """
    columns = [
        ("supplierprofile", "logo_path",     "VARCHAR(500)", "NULL"),
        ("supplierprofile", "stamp_path",    "VARCHAR(500)", "NULL"),
        ("supplierprofile", "brand_color",   "VARCHAR(7)",   "'#1F2937'"),
        ("supplierprofile", "osvc_activity_type", "VARCHAR(10)", "'main'"),
        ("user",           "is_admin",      "BOOLEAN",      "0"),
        ("user",           "is_active",     "BOOLEAN",      "1"),
        ("user",           "token_version", "INTEGER",      "1"),
        ("contact",        "payment_due_days", "INTEGER",   "NULL"),
        ("invoice",        "payment_method", "VARCHAR(10)", "'transfer'"),
        ("invoice",        "customer_snapshot", "VARCHAR",  "'{}'"),
        ("taxsetting",     "income_tax_rate",        "FLOAT", "0.15"),
        ("taxsetting",     "social_assessment_rate", "FLOAT", "0.55"),
        ("taxsetting",     "social_insurance_rate",  "FLOAT", "0.292"),
        ("taxsetting",     "health_assessment_rate", "FLOAT", "0.50"),
        ("taxsetting",     "health_insurance_rate",  "FLOAT", "0.135"),
    ]
    for table, col, sql_type, default in columns:
        try:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {col} {sql_type} DEFAULT {default}"
            ))
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists

    # The Variable-Symbol uniqueness index (now declared on the Invoice model)
    # was historically created here; ensure a legacy DB gains it before adoption.
    try:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_invoice_user_vs ON invoice (user_id, variable_symbol)"
        ))
        conn.commit()
    except Exception:
        conn.rollback()


def _migration_0002_credit_notes(conn: Connection) -> None:
    """Dobropis (credit note) support: document type + back-reference."""
    conn.execute(text(
        "ALTER TABLE invoice ADD COLUMN doc_type VARCHAR(12) DEFAULT 'invoice'"
    ))
    conn.execute(text(
        "ALTER TABLE invoice ADD COLUMN related_invoice_id INTEGER DEFAULT NULL"
    ))


# Ordered, append-only list frozen at the point Alembic took over. The last
# number is the schema version the baseline revision corresponds to.
_LEGACY_MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [
    (1, _migration_0001_baseline),
    (2, _migration_0002_credit_notes),
]

_LEGACY_SCHEMA_VERSION = _LEGACY_MIGRATIONS[-1][0]


def _finish_legacy_pragma_migrations() -> None:
    """Apply any legacy PRAGMA migrations still pending on a SQLite database."""
    with engine.connect() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar_one()
        for version, step in _LEGACY_MIGRATIONS:
            if version <= current:
                continue
            step(conn)
            _legacy_stamp_version(conn, version)


# ---------------------------------------------------------------------------
# Data fixups (not schema migrations, so they run on every startup)
# ---------------------------------------------------------------------------

def _ensure_master_is_admin() -> None:
    """The original master user always has admin rights."""
    with engine.connect() as conn:
        conn.execute(text("UPDATE \"user\" SET is_admin = 1 WHERE username = 'master'"))
        conn.commit()
